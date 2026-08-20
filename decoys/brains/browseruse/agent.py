"""
BrowserUse Brain - AI-powered browser automation agent.

Supports prompt configuration for content/behavior control.
"""
import json
import os
import asyncio
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from common.logging.agent_logger import AgentLogger


def log(msg: str):
    """Print with timestamp."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")

from browser_use import Agent, ChatOllama
from browser_use.browser.session import BrowserSession

from brains.browseruse.config import CHROMIUM_ARGS

from common.config.model_config import get_model, get_ollama_seed, get_num_ctx
from brains.browseruse.prompts import BUPrompts, DEFAULT_PROMPTS

# LLM timeout in seconds - 5 minutes for CPU models
LLM_TIMEOUT = 300

# Mapping from browser_use action types to unified step names.
# browser_use LLM responses contain JSON with an "action" list where each
# element is a dict like {"navigate": {"url": "..."}} or {"click": {"index": 5}}.
#
# This must cover browser-use 0.12.7's FULL action registry (Tools.registry,
# 24 actions). The 0.12.x section below is the complete set as of 2026-05-27 —
# enumerated directly from the registry after the drift-detection WARNING
# caught an unmapped `read_file` on a deployed SUP (the original map was
# derived from one VM's observed actions and missed ~half). The pre-0.12 names
# are retained as aliases for pin-rollback safety. If browser-use adds/renames
# actions again, the drift WARNING (see logged_chat) fires so it isn't silent.
_BU_ACTION_MAP = {
    # --- browser-use 0.12.7 registry (full) ---
    # navigation / tabs
    "navigate": ("navigate", "browser"),
    "go_back": ("navigate", "browser"),
    "switch": ("navigate", "browser"),            # switch tab
    "close": ("navigate", "browser"),             # close tab
    # interaction
    "click": ("click", "browser"),
    "select_dropdown": ("click", "browser"),
    "dropdown_options": ("click", "browser"),
    "input": ("type_text", "browser"),
    "send_keys": ("type_text", "browser"),
    # page read / observe
    "scroll": ("scroll", "browser"),
    "extract": ("scroll", "browser"),             # read page content
    "find_elements": ("scroll", "browser"),       # scan the DOM
    "find_text": ("scroll", "browser"),           # scan page for text
    "screenshot": ("scroll", "browser"),          # observe page
    "evaluate": ("scroll", "browser"),            # JS eval against the page
    # search
    "search": ("search", "browser"),
    "search_page": ("search", "browser"),
    # file ops
    "read_file": ("download_file", "browser"),
    "upload_file": ("download_file", "browser"),
    "write_file": ("save_document", "browser"),
    "save_as_pdf": ("save_document", "browser"),
    "replace_file": ("edit_content", "browser"),
    # misc
    "wait": ("wait", "browser"),
    "play_assigned_video": ("play_assigned_video", "video"),
    "create_assigned_document": ("create_assigned_document", "office"),
    # "done" intentionally omitted — workflow completion, not a step
    # --- pre-0.12 aliases (not in 0.12.7 registry; kept for pin-rollback) ---
    "go_to_url": ("navigate", "browser"),
    "open_tab": ("navigate", "browser"),
    "switch_tab": ("navigate", "browser"),
    "click_element": ("click", "browser"),
    "input_text": ("type_text", "browser"),
    "scroll_down": ("scroll", "browser"),
    "scroll_up": ("scroll", "browser"),
    "scroll_to_text": ("scroll", "browser"),
    "search_google": ("search", "browser"),
    "extract_content": ("scroll", "browser"),
    "extract_page_content": ("scroll", "browser"),
    "save_file": ("save_document", "browser"),
}


# Drift detection. When an action name recorded by browser_use is absent from
# _BU_ACTION_MAP, browser-use has renamed its vocabulary and step logging has
# gone silent for that action — exactly what bit us on the 0.12.x upgrade
# (~99% of steps dropped, no error). We count consecutive UNMAPPED actions and
# print one [WARNING] at the threshold so ./audit's Warn column surfaces it.
_BU_DRIFT_THRESHOLD = 10
_bu_drift_state = {"streak": 0, "warned": False}


def _bu_note_drift(logger, name: str) -> None:
    """Record an unmapped browser_use action; emit one [WARNING] at threshold."""
    _bu_drift_state["streak"] += 1
    if (logger is not None and not _bu_drift_state["warned"]
            and _bu_drift_state["streak"] >= _BU_DRIFT_THRESHOLD):
        _bu_drift_state["warned"] = True
        msg = (f"[WARNING] [parser-drift] browser-use action '{name}' and "
               f"{_bu_drift_state['streak']} consecutive others are absent from "
               f"_BU_ACTION_MAP — browser action steps are NOT being logged. "
               f"browser-use likely renamed its action vocabulary; update "
               f"_BU_ACTION_MAP in brains/browseruse/agent.py.")
        print(msg, flush=True)
        logger.warning(msg, details={"unmapped_action": name,
                                     "streak": _bu_drift_state["streak"]})


def _log_bu_steps(logger, history) -> None:
    """Emit step events from a browser_use AgentHistoryList (the agent.run()
    return value).

    This is the authoritative source: each AgentHistory carries the actions
    browser_use actually parsed AND their ActionResults (so we get real
    success/error), plus per-step timing. Walking the structured history avoids
    re-parsing raw LLM text — which the model wraps in `---\\n{...}` separators
    that strict json.loads rejects (the 2026-05-25 step-drop). Captures every
    action in a step, not just the first.
    """
    if logger is None:
        return
    steps = getattr(history, "history", None)
    if not steps:
        return
    for h in steps:
        model_output = getattr(h, "model_output", None)
        if model_output is None:
            continue
        actions = getattr(model_output, "action", None) or []
        results = getattr(h, "result", None) or []
        # Per-step duration; only attribute it when the step has a single
        # action so multi-action steps don't each claim the full step time.
        dur_ms = None
        meta = getattr(h, "metadata", None)
        if meta is not None and len(actions) == 1:
            try:
                dur_ms = int(meta.duration_seconds * 1000)
            except (TypeError, ValueError, AttributeError):
                dur_ms = None
        for idx, action in enumerate(actions):
            try:
                dumped = action.model_dump(exclude_none=True)
            except Exception:
                continue
            name = next((k for k in dumped if k != "interacted_element"), None)
            if not name or name == "done":
                continue
            mapped = _BU_ACTION_MAP.get(name)
            if mapped is None:
                _bu_note_drift(logger, name)
                continue
            _bu_drift_state["streak"] = 0
            step_name, category = mapped
            params = dumped.get(name) or {}
            if isinstance(params, dict):
                msg = params.get("url") or params.get("text") or params.get("query") or str(params)
            else:
                msg = str(params)
            logger.step_start(step_name, category=category, message=str(msg)[:200])
            res = results[idx] if idx < len(results) else None
            err = getattr(res, "error", None) if res is not None else None
            if err:
                logger.step_error(step_name, message=str(err)[:200],
                                  category=category, duration_ms=dur_ms)
            else:
                logger.step_success(step_name, category=category, duration_ms=dur_ms)


def create_logged_chat_ollama(model: str, logger: Optional["AgentLogger"] = None, timeout: int = LLM_TIMEOUT):
    """
    Create a browser_use.ChatOllama instance with logging wrapped around ainvoke.

    browser_use uses its own ChatOllama class (not LangChain) which directly calls
    ollama.AsyncClient. We wrap the Ollama client's chat method to capture token
    counts before browser_use discards them.

    Each LLM response is also parsed for browser_use action types (go_to_url,
    click_element, input_text, etc.) and logged as mechanical steps using the
    unified step vocabulary (navigate, click, type_text, scroll, etc.).

    Args:
        model: Ollama model name
        logger: Optional AgentLogger for logging LLM calls
        timeout: LLM request timeout in seconds (default 300s for CPU models)
    """
    llm = ChatOllama(model=model, timeout=timeout)

    # BrowserUse sends full DOMs as context; Ollama's default num_ctx (4096
    # on CPU, auto-tuned on GPU) silently truncates these and the model
    # produces garbage. Pick a tier-aware num_ctx so V100s get headroom and
    # CPU agents don't blow their RAM budget.
    BU_NUM_CTX = get_num_ctx()

    if logger is None:
        # Even without logging, inject num_ctx + optional seed
        ollama_seed = get_ollama_seed()
        original_get_client_nolog = llm.get_client
        def get_configured_client():
            client = original_get_client_nolog()
            original_chat_nolog = client.chat
            async def configured_chat(*args, **kwargs):
                # browser_use sometimes passes options=None explicitly,
                # so setdefault won't help — must check for falsy.
                options = kwargs.get('options') or {}
                options['num_ctx'] = BU_NUM_CTX
                if ollama_seed is not None:
                    options['seed'] = ollama_seed
                    options['temperature'] = 0
                kwargs['options'] = options
                return await original_chat_nolog(*args, **kwargs)
            client.chat = configured_chat
            return client
        llm.get_client = get_configured_client
        return llm

    # Store the original get_client method
    original_get_client = llm.get_client
    ollama_seed = get_ollama_seed()

    def get_logged_client():
        """Return an Ollama client with logging wrapped around chat()."""
        client = original_get_client()
        original_chat = client.chat

        async def logged_chat(*args, **kwargs):
            """Wrapper that logs requests/responses with token counts."""
            # browser_use sometimes passes options=None explicitly, so
            # setdefault won't help — must check for falsy.
            options = kwargs.get('options') or {}
            options['num_ctx'] = BU_NUM_CTX
            # Inject Ollama seed for deterministic generation (if configured)
            if ollama_seed is not None:
                options['seed'] = ollama_seed
                options['temperature'] = 0
            kwargs['options'] = options

            # Log the request
            messages = kwargs.get('messages', args[1] if len(args) > 1 else [])
            action = "generate"
            if messages and len(messages) > 0:
                last_msg = messages[-1]
                content = last_msg.get('content', '') if isinstance(last_msg, dict) else str(last_msg)
                action = content[:100] if len(content) > 100 else content

            logger.llm_request(
                action=action,
                model=model,
                input_data={"message_count": len(messages), "model": model}
            )

            # Call the original method
            start_time = time.time()
            try:
                response = await original_chat(*args, **kwargs)
                duration_ms = int((time.time() - start_time) * 1000)

                # Extract output and token counts from Ollama response
                output = ""
                if hasattr(response, 'message') and hasattr(response.message, 'content'):
                    output = str(response.message.content)[:500] if response.message.content else ""

                # Ollama provides token counts in the response
                tokens = None
                input_tokens = getattr(response, 'prompt_eval_count', None)
                output_tokens = getattr(response, 'eval_count', None)
                if input_tokens is not None or output_tokens is not None:
                    tokens = {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total": (input_tokens or 0) + (output_tokens or 0)
                    }

                logger.llm_response(
                    output=output,
                    duration_ms=duration_ms,
                    model=model,
                    tokens=tokens
                )

                # NOTE: per-action step events are emitted from the structured
                # AgentHistoryList after agent.run() returns (see _log_bu_steps),
                # not parsed from this raw LLM text — that gives real
                # success/error outcomes and avoids the markdown-wrapped-JSON
                # fragility. This wrapper only records the LLM call itself.
                return response
            except asyncio.CancelledError:
                # CPU-slow LLM calls get cancelled (browser_use step timeout /
                # shutdown) BEFORE returning. CancelledError subclasses
                # BaseException, so the `except Exception` below misses it and the
                # call vanishes silently (llm_request logged, no response, no
                # error — the req>>resp gap on CPU SUPs). Log it, then re-raise:
                # cancellation must always propagate.
                duration_ms = int((time.time() - start_time) * 1000)
                logger.llm_error(error=f"cancelled/timeout after {duration_ms}ms",
                                 action=f"llm_call_{model}", fatal=False)
                raise
            except Exception as e:
                logger.llm_error(error=str(e), action=f"llm_call_{model}", fatal=True)
                raise

        client.chat = logged_chat
        return client

    # Replace get_client with our logged version
    llm.get_client = get_logged_client

    return llm


class BrowserUseAgent:
    """
    BrowserUse agent with three-prompt support.

    Configurations:
    - B1.llama: DEFAULT_PROMPTS + llama3.1:8b
    - B2.gemma: DEFAULT_PROMPTS + gemma3:1b
    - B3.deepseek: DEFAULT_PROMPTS + deepseek-r1:8b
    - B?.model+: PHASE_PROMPTS + any model (POST-PHASE)
    """

    def __init__(
        self,
        prompts: BUPrompts = DEFAULT_PROMPTS,
        model: str = None,
        headless: bool = False,
        max_steps: int = 10,
        logger: Optional["AgentLogger"] = None,
    ):
        self.prompts = prompts
        self.model_name = get_model(model)
        self.headless = headless
        self.max_steps = max_steps
        self.logger = logger
        self._llm = None
        self._browser_session = None

    def _get_llm(self):
        """Lazy-load the LLM with logging wrapper."""
        if self._llm is None:
            # Use browser_use's native ChatOllama with our logging wrapper
            # This properly integrates with browser_use's custom LLM architecture
            self._llm = create_logged_chat_ollama(self.model_name, self.logger)
        return self._llm

    def _get_browser_session(self):
        """Create browser session with container-safe configuration."""
        return BrowserSession(
            headless=self.headless,
            channel="chromium",
            args=CHROMIUM_ARGS,
        )

    async def run_async(self, task: str) -> Optional[str]:
        """
        Run a task asynchronously with configured prompts.

        Args:
            task: The specific task to perform (e.g., "Search for Python tutorials")

        Returns:
            Result from the agent, or None on error
        """
        # Build full prompt from task + content guidelines
        full_prompt = BUPrompts(
            task=task,
            content=self.prompts.content,
        ).build_full_prompt()

        log(f"Starting BrowserUse agent with model: {self.model_name}")
        log(f"Task: {task}")

        # Use the task itself as workflow name for better queryability
        workflow_name = task[:100] if len(task) > 100 else task

        if self.logger:
            self.logger.workflow_start(workflow_name, params={
                "agent_type": "browseruse",
                "model": self.model_name
            })

        try:
            browser_session = self._get_browser_session()
            agent = Agent(
                task=full_prompt,
                llm=self._get_llm(),
                browser_session=browser_session,
                # Token budget optimization — see browser_use Agent docs.
                # Defaults send screenshots + 40K-char DOM dumps + unbounded
                # history, which crushes CPU LLMs and wastes V100 cycles.
                use_vision=False,                  # gemma family is text-only
                use_judge=False,                   # skip LLM-based step evaluation
                max_clickable_elements_length=8000,  # cap DOM dump (~2K tokens)
                max_history_items=5,               # bounded conversation memory
                include_attributes=[
                    "id", "class", "name", "type", "value",
                    "placeholder", "aria-label", "role", "href",
                    "title", "alt",
                ],
                llm_timeout=300,                   # CPU LLMs can take 2-3 min/call
            )
            result = await agent.run(max_steps=self.max_steps)
            success = bool(result and result.is_done())
            log("Task completed successfully!" if success else "Task finished but agent did not complete goal")

            _log_bu_steps(self.logger, result)
            if self.logger:
                self.logger.workflow_end(workflow_name, success=success,
                                        result=str(result)[:500] if result else None)
            return result
        except Exception as e:
            log(f"Error running agent: {e}")
            import traceback
            traceback.print_exc()

            if self.logger:
                self.logger.workflow_end(workflow_name, success=False, error=str(e))
            return None

    def run(self, task: str) -> Optional[str]:
        """
        Run a task synchronously.

        Args:
            task: The specific task to perform

        Returns:
            Result from the agent, or None on error
        """
        return asyncio.run(self.run_async(task))


def run(task: str, model: str = None, prompts: BUPrompts = DEFAULT_PROMPTS,
        headless: bool = False, max_steps: int = 10) -> Optional[str]:
    """Convenience function to run BrowserUse agent."""
    agent = BrowserUseAgent(
        prompts=prompts,
        model=model,
        headless=headless,
        max_steps=max_steps,
    )
    return agent.run(task)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='BrowserUse Agent')
    parser.add_argument('task', nargs='?', default="Visit google.com and search for 'OpenAI news'")
    parser.add_argument('--model', type=str, default=None, help='Model key: llama, gemma, deepseek')
    parser.add_argument('--phase', action='store_true', help='Use PHASE-improved prompts')
    parser.add_argument('--max-steps', type=int, default=10)
    parser.add_argument('--headless', action='store_true', default=False)
    args = parser.parse_args()

    from brains.browseruse.prompts import PHASE_PROMPTS

    prompts = PHASE_PROMPTS if args.phase else DEFAULT_PROMPTS

    run(
        task=args.task,
        model=args.model,
        prompts=prompts,
        headless=args.headless,
        max_steps=args.max_steps,
    )
