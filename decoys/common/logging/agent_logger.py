"""
RUSE Agent Logger

Unified logging framework for all agent types with timestamped event tracking
for experiment analysis. Outputs JSON-Lines format for easy parsing.

Usage:
    from common.logging.agent_logger import AgentLogger

    logger = AgentLogger(agent_type="MCHP-SMOL", log_dir="/path/to/logs")
    logger.session_start()
    logger.workflow_start("google_search")
    logger.llm_request(action="generate_query", input_data={"context": "..."})
    logger.llm_response(output="search query", duration_ms=1200)
    logger.workflow_end("google_search", success=True, duration_ms=45000)
    logger.session_end()
"""

import atexit
import json
import os
import random
import signal
import uuid
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum


class EventType(str, Enum):
    """Types of events that can be logged."""
    # Session events
    SESSION_START = "session_start"
    SESSION_SUCCESS = "session_success"  # Explicit success marker (call before session_end)
    SESSION_FAIL = "session_fail"        # Explicit failure marker (call before session_end)
    SESSION_END = "session_end"

    # Workflow events
    WORKFLOW_START = "workflow_start"
    WORKFLOW_END = "workflow_end"
    WORKFLOW_PLAN_TERMINAL = "workflow_plan_terminal"

    # Step events (replaces BROWSER_ACTION/GUI_ACTION)
    STEP_START = "step_start"
    STEP_SUCCESS = "step_success"
    STEP_ERROR = "step_error"

    # LLM events
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    LLM_ERROR = "llm_error"

    # Other events
    DECISION = "decision"
    TIMING_DELAY = "timing_delay"

    # Diagnostic events
    WARNING = "warning"
    INFO = "info"

    # Telemetry events
    NETWORK_SAMPLE = "network_sample"  # per-minute OS-level outbound conn volume


class StepCategory(str, Enum):
    """Categories for step-level actions."""
    BROWSER = "browser"           # Web browsing, search, navigation
    VIDEO = "video"               # YouTube, video players
    OFFICE = "office"             # Word processors, spreadsheets, paint
    SHELL = "shell"               # Command execution, terminals
    PROGRAMMING = "programming"   # Code editing, building software
    EMAIL = "email"               # Email clients
    AUTHENTICATION = "authentication"  # Login, SSO, Shibboleth
    OTHER = "other"               # Uncategorized actions


@dataclass
class LogEvent:
    """Structured log event."""
    timestamp: str
    session_id: str
    agent_type: str
    event_type: str
    workflow: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        """Convert event to JSON string."""
        data = asdict(self)
        # Remove None values for cleaner output
        data = {k: v for k, v in data.items() if v is not None}
        return json.dumps(data)

    @classmethod
    def from_json(cls, json_str: str) -> 'LogEvent':
        """Create event from JSON string."""
        data = json.loads(json_str)
        return cls(**data)


class AgentLogger:
    """
    Unified logging framework for RUSE agents.

    Outputs JSON-Lines format with one event per line for easy parsing
    and post-hoc analysis of agent behavior.
    """

    def __init__(
        self,
        agent_type: str,
        log_dir: Optional[str] = None,
        session_id: Optional[str] = None
    ):
        """
        Initialize the logger.

        Args:
            agent_type: Type of agent (e.g., "MCHP-SMOL", "BU-PHASE")
            log_dir: Directory for log files. Defaults to deployed_sups/{agent_type}/logs/
            session_id: Optional session ID. Auto-generated if not provided.
        """
        self.agent_type = agent_type
        # Deterministic session_id when seed is set (via SUP_OLLAMA_SEED env
        # var, exported by sup/__main__.py after seed resolution). Uses a
        # separate Random() instance so global RNG state is not consumed.
        # Same seed across replays → same session_id → diff tooling can align.
        if session_id is None:
            seed_env = os.environ.get("SUP_OLLAMA_SEED")
            if seed_env:
                try:
                    sid_rng = random.Random(int(seed_env))
                    session_id = f"{sid_rng.getrandbits(32):08x}"
                except (TypeError, ValueError):
                    session_id = str(uuid.uuid4())[:8]
            else:
                session_id = str(uuid.uuid4())[:8]
        self.session_id = session_id
        self.current_workflow: Optional[str] = None
        self._workflow_start_time: Optional[float] = None
        self._session_start_time: Optional[float] = None

        # Step tracking
        self._step_start_times: Dict[str, float] = {}
        self._step_categories: Dict[str, str] = {}
        self._current_step: Optional[str] = None

        # Session outcome tracking
        self._session_outcome: Optional[str] = None  # "success" or "fail"

        # Shutdown guard: ensures session_end is written exactly once
        self._session_ended = False
        self._shutdown_registered = False

        # Set up log directory
        # SUP_CONFIG_KEY overrides agent_type for log path (e.g., B0R.llama vs B0.llama)
        log_identity = os.environ.get("SUP_CONFIG_KEY", agent_type)

        if log_dir:
            self.log_dir = Path(log_dir)
        else:
            # Check for RUSE_LOG_DIR environment variable first (set by deployment)
            env_log_dir = os.environ.get("RUSE_LOG_DIR")
            if env_log_dir:
                self.log_dir = Path(env_log_dir)
            else:
                # Default: Use /opt/ruse if it exists (deployed), otherwise relative
                deployed_base = Path("/opt/ruse/deployed_sups")
                if deployed_base.exists():
                    self.log_dir = deployed_base / log_identity / "logs"
                else:
                    # Development fallback: relative to project root
                    base_dir = Path(__file__).parent.parent.parent.parent / "deployed_sups" / log_identity / "logs"
                    self.log_dir = base_dir

        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Create log file with timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = self.log_dir / f"session_{timestamp}_{self.session_id}.jsonl"

        # Create/update symlink to latest log
        latest_link = self.log_dir / "latest.jsonl"
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()
        latest_link.symlink_to(self.log_file.name)

        self._file_handle = open(self.log_file, 'a')

    def _get_timestamp(self) -> str:
        """Get current ISO timestamp with microseconds."""
        return datetime.now().isoformat()

    def _write_event(self, event: LogEvent) -> None:
        """Write event to log file."""
        self._file_handle.write(event.to_json() + "\n")
        self._file_handle.flush()

    def _log(
        self,
        event_type: EventType,
        workflow: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> LogEvent:
        """Create and write a log event."""
        event = LogEvent(
            timestamp=self._get_timestamp(),
            session_id=self.session_id,
            agent_type=self.agent_type,
            event_type=event_type.value,
            workflow=workflow or self.current_workflow,
            details=details
        )
        self._write_event(event)
        return event

    # =========================================================================
    # Session Events
    # =========================================================================

    def session_start(self, config: Optional[Dict[str, Any]] = None) -> LogEvent:
        """Log session start event."""
        self._session_start_time = time.time()
        self._session_ended = False
        details = {"config": config} if config else {}
        details["log_file"] = str(self.log_file)
        event = self._log(EventType.SESSION_START, details=details)
        self._register_shutdown_handlers()
        return event

    def session_end(self, summary: Optional[Dict[str, Any]] = None) -> LogEvent:
        """Log session end event. Guarded against double-call."""
        if self._session_ended:
            return None
        self._session_ended = True
        details = summary or {}
        if self._session_start_time:
            details["total_duration_ms"] = int((time.time() - self._session_start_time) * 1000)
        if self._session_outcome:
            details["outcome"] = self._session_outcome
        return self._log(EventType.SESSION_END, details=details)

    def session_success(self, message: str = "Session completed successfully",
                        details: Optional[Dict[str, Any]] = None) -> LogEvent:
        """
        Log explicit session success. Call BEFORE session_end().

        Args:
            message: Success message
            details: Additional details to log
        """
        self._session_outcome = "success"
        event_details = {
            "message": message,
            "status": "success"
        }
        if self._session_start_time:
            event_details["duration_ms"] = int((time.time() - self._session_start_time) * 1000)
        if details:
            event_details.update(details)
        return self._log(EventType.SESSION_SUCCESS, details=event_details)

    def session_fail(self, message: str, error: Optional[str] = None,
                     exception: Optional[Exception] = None,
                     details: Optional[Dict[str, Any]] = None) -> LogEvent:
        """
        Log explicit session failure. Call BEFORE session_end().

        Args:
            message: Failure message
            error: Error string (alternative to exception)
            exception: Original exception if available
            details: Additional details to log
        """
        self._session_outcome = "fail"
        event_details = {
            "message": message,
            "status": "fail"
        }
        if error:
            event_details["error"] = error
        if exception:
            event_details["exception_type"] = type(exception).__name__
            event_details["exception_str"] = str(exception)
        if self._session_start_time:
            event_details["duration_ms"] = int((time.time() - self._session_start_time) * 1000)
        if details:
            event_details.update(details)
        return self._log(EventType.SESSION_FAIL, details=event_details)

    # =========================================================================
    # Workflow Events
    # =========================================================================

    def workflow_start(self, workflow_name: str, params: Optional[Dict[str, Any]] = None) -> LogEvent:
        """Log workflow start event."""
        self.current_workflow = workflow_name
        self._workflow_start_time = time.time()
        details = {"params": params} if params else {}
        return self._log(EventType.WORKFLOW_START, workflow=workflow_name, details=details)

    def workflow_end(
        self,
        workflow_name: str,
        success: bool,
        duration_ms: Optional[int] = None,
        result: Optional[Any] = None,
        error: Optional[str] = None
    ) -> LogEvent:
        """Log workflow end event."""
        details = {"success": success}

        if duration_ms is not None:
            details["duration_ms"] = duration_ms
        elif self._workflow_start_time:
            details["duration_ms"] = int((time.time() - self._workflow_start_time) * 1000)

        if result is not None:
            # Truncate result if too long
            result_str = str(result)
            details["result"] = result_str[:500] if len(result_str) > 500 else result_str

        if error:
            details["error"] = error

        event = self._log(EventType.WORKFLOW_END, workflow=workflow_name, details=details)
        self.current_workflow = None
        self._workflow_start_time = None
        return event

    # =========================================================================
    # LLM Events
    # =========================================================================

    def llm_request(
        self,
        action: str,
        input_data: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None
    ) -> LogEvent:
        """Log LLM request event."""
        details = {"action": action}
        if input_data:
            # Truncate long inputs
            for key, value in input_data.items():
                if isinstance(value, str) and len(value) > 500:
                    input_data[key] = value[:500] + "..."
            details["input"] = input_data
        if model:
            details["model"] = model
        return self._log(EventType.LLM_REQUEST, details=details)

    def llm_response(
        self,
        output: str,
        duration_ms: int,
        model: Optional[str] = None,
        tokens: Optional[Dict[str, int]] = None
    ) -> LogEvent:
        """Log LLM response event."""
        details = {
            "output": output[:500] if len(output) > 500 else output,
            "duration_ms": duration_ms
        }
        if model:
            details["model"] = model
        if tokens:
            details["tokens"] = tokens
        return self._log(EventType.LLM_RESPONSE, details=details)

    def llm_error(self, error: str, action: str, fatal: bool = True) -> LogEvent:
        """
        Log LLM error event.

        Args:
            error: Error message
            action: The action that was being attempted
            fatal: If True, indicates experiment is invalid
        """
        details = {
            "error": error,
            "action": action,
            "fatal": fatal,
            "experiment_valid": not fatal
        }
        return self._log(EventType.LLM_ERROR, details=details)

    # =========================================================================
    # Decision Events
    # =========================================================================

    def decision(
        self,
        choice: str,
        options: Optional[List[str]] = None,
        selected: Optional[str] = None,
        context: Optional[str] = None,
        method: str = "llm"  # "llm" or "random"
    ) -> LogEvent:
        """Log a decision event."""
        details = {
            "choice_type": choice,
            "method": method
        }
        if options:
            # Limit options list for logging
            details["options_count"] = len(options)
            details["options_sample"] = options[:5] if len(options) > 5 else options
        if selected:
            details["selected"] = selected
        if context:
            details["context"] = context[:200] if len(context) > 200 else context
        return self._log(EventType.DECISION, details=details)

    # =========================================================================
    # Step Events (replaces BROWSER_ACTION/GUI_ACTION)
    # =========================================================================

    def step_start(self, step_name: str, category: str = "other",
                   message: Optional[str] = None,
                   details: Optional[Dict[str, Any]] = None) -> LogEvent:
        """
        Log step start with category. Tracks start time for duration.

        Args:
            step_name: Name of the step (e.g., "navigate", "click", "login")
            category: Step category (browser, video, office, shell, etc.)
            message: Optional description message
            details: Additional details to log
        """
        self._step_start_times[step_name] = time.time()
        self._step_categories[step_name] = category
        self._current_step = step_name

        event_details = {
            "step_name": step_name,
            "category": category,
            "status": "start"
        }
        if message:
            event_details["message"] = message[:200] if len(message) > 200 else message
        if details:
            event_details.update(details)

        return self._log(EventType.STEP_START, details=event_details)

    def step_success(self, step_name: str, category: Optional[str] = None,
                     message: Optional[str] = None,
                     duration_ms: Optional[int] = None,
                     details: Optional[Dict[str, Any]] = None) -> LogEvent:
        """
        Log step success. Category optional (uses stored value from step_start).

        Args:
            step_name: Name of the step
            category: Step category (optional, uses stored value if not provided)
            message: Optional success message
            duration_ms: Duration in milliseconds (auto-calculated if not provided)
            details: Additional details to log
        """
        # Use stored category if not provided
        if category is None:
            category = self._step_categories.get(step_name, "other")

        # Calculate duration if not provided
        if duration_ms is None and step_name in self._step_start_times:
            duration_ms = int((time.time() - self._step_start_times[step_name]) * 1000)

        event_details = {
            "step_name": step_name,
            "category": category,
            "status": "success"
        }
        if duration_ms is not None:
            event_details["duration_ms"] = duration_ms
        if message:
            event_details["message"] = message[:200] if len(message) > 200 else message
        if details:
            event_details.update(details)

        # Clean up tracking
        self._step_start_times.pop(step_name, None)
        self._step_categories.pop(step_name, None)
        if self._current_step == step_name:
            self._current_step = None

        return self._log(EventType.STEP_SUCCESS, details=event_details)

    def step_error(self, step_name: str, message: str,
                   category: Optional[str] = None,
                   exception: Optional[Exception] = None,
                   duration_ms: Optional[int] = None,
                   details: Optional[Dict[str, Any]] = None) -> LogEvent:
        """
        Log step error. Category optional (uses stored value from step_start).

        Args:
            step_name: Name of the step
            message: Error message
            category: Step category (optional, uses stored value if not provided)
            exception: Original exception if available
            duration_ms: Duration in milliseconds (auto-calculated if not provided)
            details: Additional details to log
        """
        # Use stored category if not provided
        if category is None:
            category = self._step_categories.get(step_name, "other")

        # Calculate duration if not provided
        if duration_ms is None and step_name in self._step_start_times:
            duration_ms = int((time.time() - self._step_start_times[step_name]) * 1000)

        event_details = {
            "step_name": step_name,
            "category": category,
            "status": "error",
            "message": message
        }
        if exception:
            event_details["exception_type"] = type(exception).__name__
            event_details["exception_str"] = str(exception)
        if duration_ms is not None:
            event_details["duration_ms"] = duration_ms
        if details:
            event_details.update(details)

        # Clean up tracking
        self._step_start_times.pop(step_name, None)
        self._step_categories.pop(step_name, None)
        if self._current_step == step_name:
            self._current_step = None

        return self._log(EventType.STEP_ERROR, details=event_details)

    @contextmanager
    def step(self, step_name: str, category: str = "other",
             message: Optional[str] = None):
        """
        Context manager for automatic step success/error logging with category.

        Usage:
            with logger.step("login", category="authentication", message="Logging in"):
                perform_login()
            # Automatically logs step_success or step_error

        Args:
            step_name: Name of the step
            category: Step category (browser, video, office, shell, etc.)
            message: Optional description message
        """
        self.step_start(step_name, category=category, message=message)
        try:
            yield
            self.step_success(step_name)
        except Exception as e:
            self.step_error(step_name, str(e), exception=e)
            raise

    # =========================================================================
    # Deprecated Methods (for backwards compatibility)
    # =========================================================================

    def browser_action(
        self,
        action: str,
        target: Optional[str] = None,
        success: bool = True,
        duration_ms: Optional[int] = None
    ) -> LogEvent:
        """
        DEPRECATED: Use step() or step_start/step_success/step_error instead.

        Maps to: step(action, category="browser", message=target)
        """
        if success:
            return self.step_success(action, category="browser", message=target,
                                     duration_ms=duration_ms)
        else:
            return self.step_error(action, message=f"Failed: {target or 'unknown'}",
                                   category="browser", duration_ms=duration_ms)

    def gui_action(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        success: bool = True,
        target: Optional[str] = None  # Backwards compatibility with old API
    ) -> LogEvent:
        """
        DEPRECATED: Use step() or step_start/step_success/step_error instead.

        Maps to: step(action, category="office", details=params)

        Args:
            action: Action name
            params: Optional parameters dict
            success: Whether action succeeded
            target: Backwards-compatible alias for message (old API used target=)
        """
        # Handle backwards-compatible target parameter
        msg = target
        if params and not msg:
            msg = params.get("target")
        if success:
            return self.step_success(action, category="office", message=msg,
                                     details=params)
        else:
            return self.step_error(action, message=f"Failed: {msg or 'unknown'}",
                                   category="office", details=params)

    def error(
        self,
        message: str,
        fatal: bool = False,
        exception: Optional[Exception] = None
    ) -> LogEvent:
        """
        DEPRECATED: Use step_error() within a step, or session_fail() at session level.

        This method logs a warning with the error details for backwards compatibility.
        """
        details = {
            "message": message,
            "fatal": fatal,
            "deprecated": True,
            "migration_hint": "Use step_error() within a step, or session_fail() at session level"
        }
        if exception:
            details["exception_type"] = type(exception).__name__
            details["exception_str"] = str(exception)

        event = self._log(EventType.WARNING, details=details)

        if fatal:
            raise RuntimeError(f"FATAL ERROR [{self.agent_type}]: {message}")

        return event

    # =========================================================================
    # Other Events
    # =========================================================================

    def timing_delay(
        self,
        seconds: float,
        reason: str = "scheduled"
    ) -> LogEvent:
        """Log timing delay event."""
        details = {
            "delay_seconds": round(seconds, 2),
            "delay_ms": int(seconds * 1000),
            "reason": reason
        }
        return self._log(EventType.TIMING_DELAY, details=details)

    def network_sample(
        self,
        active_opens: Optional[int] = None,
        distinct_hosts: Optional[int] = None,
        window_s: Optional[float] = None,
        d4_synthetic: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> LogEvent:
        """Log a per-minute OS-level outbound-connection sample.

        active_opens   — real outbound TCP connections opened this window (incl.
                         short-lived sub-resource conns; minor loopback noise).
        distinct_hosts — distinct non-loopback remote peer IPs (clean external floor).
        d4_synthetic   — the legacy D4-only background-service conn count, for
                         continuity with the [bg-counter] / audit BG column.
        Workflow/step counts are NOT a traffic proxy; this is the representative
        volume signal (BU ~18/min, MCHP ~1, Smol ~0.27 in 2026-06-01 ground-truth).
        """
        sample: Dict[str, Any] = {}
        if active_opens is not None:
            sample["active_opens"] = active_opens
        if distinct_hosts is not None:
            sample["distinct_hosts"] = distinct_hosts
        if window_s is not None:
            sample["window_s"] = round(window_s, 2)
        if d4_synthetic is not None:
            sample["d4_synthetic"] = d4_synthetic
        if details:
            sample.update(details)
        return self._log(EventType.NETWORK_SAMPLE, details=sample)

    def workflow_plan_terminal(self, details: Dict[str, Any]) -> LogEvent:
        """Write one canonical planned-occurrence terminal event."""
        return self._log(
            EventType.WORKFLOW_PLAN_TERMINAL,
            workflow=details.get("workflow"),
            details=details,
        )

    def warning(self, message: str, details: Optional[Dict[str, Any]] = None) -> LogEvent:
        """Log warning event."""
        warn_details = {"message": message}
        if details:
            warn_details.update(details)
        return self._log(EventType.WARNING, details=warn_details)

    def info(self, message: str, details: Optional[Dict[str, Any]] = None) -> LogEvent:
        """Log info event."""
        info_details = {"message": message}
        if details:
            info_details.update(details)
        return self._log(EventType.INFO, details=info_details)

    # =========================================================================
    # Shutdown Handling
    # =========================================================================

    def _register_shutdown_handlers(self) -> None:
        """Register atexit and signal handlers to ensure session_end on termination."""
        if self._shutdown_registered:
            return
        self._shutdown_registered = True

        # Save original signal handlers so we can restore them
        self._orig_sigterm = signal.getsignal(signal.SIGTERM)
        self._orig_sigint = signal.getsignal(signal.SIGINT)

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        atexit.register(self._atexit_handler)

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle SIGTERM/SIGINT: write session_end then re-raise."""
        sig_name = signal.Signals(signum).name
        if not self._session_ended:
            if self._session_outcome is None:
                self.session_fail(
                    message=f"Session terminated by {sig_name}",
                    error=sig_name,
                )
            self.session_end()
        self.close()

        # Restore original handler and re-raise so the process exits
        # with the correct signal exit code
        if signum == signal.SIGTERM:
            signal.signal(signal.SIGTERM, self._orig_sigterm or signal.SIG_DFL)
        elif signum == signal.SIGINT:
            signal.signal(signal.SIGINT, self._orig_sigint or signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _atexit_handler(self) -> None:
        """atexit fallback: write session_end if not already done."""
        if not self._session_ended and self._session_start_time:
            if self._session_outcome is None:
                self.session_fail(
                    message="Session terminated unexpectedly (atexit)",
                    error="unexpected_exit",
                )
            self.session_end()
        self.close()

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def close(self) -> None:
        """Close the log file handle."""
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.close()

    def __enter__(self) -> 'AgentLogger':
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        if exc_type is not None and self._session_outcome is None:
            self.session_fail(
                message=f"Session ended with exception: {exc_val}",
                exception=exc_val
            )
        self.session_end()
        self.close()

    def get_log_path(self) -> Path:
        """Get the path to the current log file."""
        return self.log_file


def read_log_file(log_path: str) -> List[LogEvent]:
    """
    Read and parse a JSON-Lines log file.

    Args:
        log_path: Path to the .jsonl log file

    Returns:
        List of LogEvent objects
    """
    events = []
    with open(log_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(LogEvent.from_json(line))
    return events


def summarize_session(log_path: str) -> Dict[str, Any]:
    """
    Generate summary statistics for a session log.

    Args:
        log_path: Path to the .jsonl log file

    Returns:
        Dictionary with session statistics including:
        - session_outcome: "success" | "fail" | None
        - steps_executed: count of STEP_START events
        - steps_successful: count of STEP_SUCCESS events
        - steps_failed: count of STEP_ERROR events
        - steps_by_category: dict of category -> {executed, successful, failed}
    """
    events = read_log_file(log_path)

    summary = {
        "total_events": len(events),
        # Session outcome
        "session_outcome": None,
        # Workflow stats
        "workflows_executed": 0,
        "workflows_successful": 0,
        "workflows_failed": 0,
        # Step stats
        "steps_executed": 0,
        "steps_successful": 0,
        "steps_failed": 0,
        "steps_by_category": {},
        # LLM stats
        "llm_requests": 0,
        "llm_errors": 0,
        "total_llm_duration_ms": 0,
        # Diagnostic stats
        "warnings": 0
    }

    for event in events:
        # Session outcome
        if event.event_type == EventType.SESSION_SUCCESS.value:
            summary["session_outcome"] = "success"
        elif event.event_type == EventType.SESSION_FAIL.value:
            summary["session_outcome"] = "fail"
        # Workflow stats
        elif event.event_type == EventType.WORKFLOW_END.value:
            summary["workflows_executed"] += 1
            if event.details and event.details.get("success"):
                summary["workflows_successful"] += 1
            else:
                summary["workflows_failed"] += 1
        # Step stats
        elif event.event_type == EventType.STEP_START.value:
            summary["steps_executed"] += 1
            category = event.details.get("category", "other") if event.details else "other"
            if category not in summary["steps_by_category"]:
                summary["steps_by_category"][category] = {"executed": 0, "successful": 0, "failed": 0}
            summary["steps_by_category"][category]["executed"] += 1
        elif event.event_type == EventType.STEP_SUCCESS.value:
            summary["steps_successful"] += 1
            category = event.details.get("category", "other") if event.details else "other"
            if category not in summary["steps_by_category"]:
                summary["steps_by_category"][category] = {"executed": 0, "successful": 0, "failed": 0}
            summary["steps_by_category"][category]["successful"] += 1
        elif event.event_type == EventType.STEP_ERROR.value:
            summary["steps_failed"] += 1
            category = event.details.get("category", "other") if event.details else "other"
            if category not in summary["steps_by_category"]:
                summary["steps_by_category"][category] = {"executed": 0, "successful": 0, "failed": 0}
            summary["steps_by_category"][category]["failed"] += 1
        # LLM stats
        elif event.event_type == EventType.LLM_REQUEST.value:
            summary["llm_requests"] += 1
        elif event.event_type == EventType.LLM_RESPONSE.value:
            if event.details and "duration_ms" in event.details:
                summary["total_llm_duration_ms"] += event.details["duration_ms"]
        elif event.event_type == EventType.LLM_ERROR.value:
            summary["llm_errors"] += 1
        # Diagnostic stats
        elif event.event_type == EventType.WARNING.value:
            summary["warnings"] += 1

    return summary
