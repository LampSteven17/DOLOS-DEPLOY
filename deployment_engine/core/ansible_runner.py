"""Ansible playbook runner with streaming log parser."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import output


@dataclass
class AnsibleEvent:
    """A parsed event from Ansible output."""
    kind: str  # "task", "host_ok", "host_fail", "retry", "skip"
    host: str | None = None
    task: str | None = None
    detail: str | None = None
    elapsed: float = 0.0


@dataclass
class AnsibleResult:
    """Result of an Ansible playbook run."""
    rc: int
    elapsed: float
    log_path: Path


# ── Task classification ──────────────────────────────────────────────
# Whitelist: only these task names produce visible step headers.
# Everything else (set_fact, include_vars, etc.) is silently logged.
_STEP_TASKS = {
    # provision-vms.yaml
    "Create OpenStack VMs":       "Creating VMs on OpenStack",
    "Wait for all VMs to become ACTIVE": "Waiting for VMs to reach ACTIVE state",
    "Show VM IPs":                "VM IP addresses",
    "Test SSH connectivity":      "Testing SSH to each VM",
    "Generate inventory file":    "Writing inventory",
    "Write SSH config snippet":   "Writing SSH config",
    # install-sups.yaml
    "Wait for SSH":               "Connecting via SSH",
    "Wait for cloud-init":        "Waiting for cloud-init to finish",
    "Update apt cache":           "Updating apt",
    "Install prerequisites":      "Installing git/curl/wget",
    "Clone RUSE repo":            "Cloning RUSE repo",
    "Make INSTALL_SUP.sh executable": "Preparing installer",
    "Stage 1: system deps + drivers": "Running INSTALL_SUP.sh stage 1 (deps + GPU drivers)",
    "Reboot for NVIDIA drivers (exit code 100)": "Rebooting VM for NVIDIA drivers",
    "Stage 2: ollama + python + services": "Running INSTALL_SUP.sh stage 2 (ollama + services)",
    "Check SUP service status":   "Verifying SUP service is running",
    # distribute-behavior-configs.yaml — only show the actual copy
    "Copy configs to SUP":        "Copying behavioral configs",
    # teardown.yaml / teardown-all.yaml
    "Get list of SUP servers":    "Listing servers to delete",
    "Get list of ALL servers": "Listing all DECOY/RAMPART/GHOSTS servers",
    "Get attached volume IDs":    "Finding attached volumes",
    "Get volume IDs attached to SUP servers": "Finding attached volumes",
    "Delete SUP servers":         "Deleting servers",
    "Delete ALL servers":         "Deleting servers",
    "Wait for servers to be deleted": "Waiting for server deletion",
    "Delete orphaned SUP volumes": "Deleting volumes",
    "Delete ALL volumes":         "Deleting volumes",
    "Wait for volumes to be deleted": "Waiting for volume deletion",
    "Delete orphaned boot volumes": "Deleting orphaned 200GB volumes",
    "Find orphaned boot volumes (nameless, 200GB, available)": "Scanning for orphaned volumes",
    "Count remaining servers":    "Verifying cleanup",
    "Assert all VMs deleted":     "Asserting cleanup complete",
    # install-ghosts-api.yaml
    "Install Docker and Docker Compose": "Installing Docker",
    "Clone GHOSTS repository":    "Cloning GHOSTS repository",
    "Start GHOSTS API stack":     "Starting GHOSTS API (docker compose up)",
    "Wait for GHOSTS API health": "Waiting for GHOSTS API to be ready",
    "Report API status":          "GHOSTS API status",
    # install-rampart-emulation.yaml
    "Write emulation passfile":         "Writing credentials",
    "Create emulation systemd service": "Creating emulation service",
    "Start emulation service":          "Starting emulation",
    "Check emulation status":           "Checking emulation status",
    "Write emulation passfile (SSH fallback)": "Writing credentials (Windows)",
    "Create emulation startup script":  "Creating startup script (Windows)",
    "Create emulation scheduled task":  "Creating scheduled task (Windows)",
    "Start emulation task":             "Starting emulation (Windows)",
    # install-ghosts-clients.yaml
    "Install .NET 10 SDK":        "Installing .NET 10 SDK",
    "Build GHOSTS universal client": "Building GHOSTS client",
    "Configure GHOSTS client (application.json)": "Configuring GHOSTS client",
    "Configure GHOSTS client timeline": "Configuring timeline",
    "Create GHOSTS client systemd service": "Creating systemd service",
    "Start GHOSTS client service": "Starting GHOSTS client",
    "Check GHOSTS client status": "Checking client status",
}

# VM name prefixes for host extraction
_VM_PREFIXES = ("d-", "r-", "g-")


class AnsibleRunner:
    """Runs Ansible playbooks with streaming output.

    Playbooks live next to the engine package (deployment_engine/playbooks/).
    Logs are caller-specified (typically deployments/logs/). Two distinct
    roots — code vs state.
    """

    # deployment_engine/playbooks/ — playbooks live alongside the code.
    _PLAYBOOKS_DIR = Path(__file__).resolve().parent.parent / "playbooks"

    def __init__(self, logs_dir: Path, playbooks_dir: Path | None = None):
        self.playbooks_dir = playbooks_dir or self._PLAYBOOKS_DIR
        self.logs_dir = logs_dir
        self._process: subprocess.Popen | None = None

    def run_playbook(
        self,
        playbook: str,
        inventory: Path,
        extra_vars: dict[str, str] | None = None,
        on_event: Callable[[AnsibleEvent], None] | None = None,
    ) -> AnsibleResult:
        """Run an Ansible playbook, stream parsed output, return result."""
        playbook_path = self.playbooks_dir / playbook
        # Flatten the playbook path for log filename: shared/teardown-all.yaml
        # → ansible-shared--teardown-all-{ts}.log (single flat file in logs_dir,
        # no nested directory). Without this the / in the playbook name made
        # the log path point at a non-existent subdir.
        log_stem = playbook.replace(".yaml", "").replace("/", "--")
        # PID suffix avoids collision when parallel teardown spawns N child
        # processes that each invoke this runner with the same wall-clock ts.
        import os as _os
        log_path = self.logs_dir / f"ansible-{log_stem}-{_timestamp()}-{_os.getpid()}.log"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "ansible-playbook",
            "-i", str(inventory),
        ]

        if extra_vars:
            for key, val in extra_vars.items():
                cmd.extend(["-e", f"{key}={val}"])

        cmd.append(str(playbook_path))

        env = os.environ.copy()
        env["ANSIBLE_FORCE_COLOR"] = "0"
        env["ANSIBLE_NOCOLOR"] = "1"
        env["ANSIBLE_STDOUT_CALLBACK"] = "default"
        env["PYTHONUNBUFFERED"] = "1"
        # Disable SSH agent — it offers too many keys causing auth timeouts
        env["SSH_AUTH_SOCK"] = ""

        start_time = time.time()

        with open(log_path, "w") as log_file:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                # Use binary mode + manual decode to avoid Python's line buffering
                bufsize=0,
            )

            # Stream stdout in the main thread to keep the log_file lifecycle
            # tied to the streaming loop. The previous threaded version raced
            # with parser_thread.join(timeout=5) — if the parser hadn't drained
            # the pipe in 5s the file would close under it and crash the thread
            # with "I/O operation on closed file", killing the batch loop.
            assert self._process.stdout is not None
            parser = _LineParser(start_time)
            try:
                for raw_line in self._process.stdout:
                    line = raw_line.decode("utf-8", errors="replace")
                    log_file.write(line)
                    log_file.flush()
                    if on_event:
                        event = parser.parse(line.rstrip("\n"))
                        if event:
                            on_event(event)
            except Exception as e:
                # Never let a parsing/log-write error abort the deploy.
                output.info(f"  WARNING: stream parser error (non-fatal): {e}")

            self._process.wait()

        elapsed = time.time() - start_time
        rc = self._process.returncode
        self._process = None

        output.info("")
        if rc == 0:
            output.info(f"  Done ({output.format_duration(elapsed)})")
        else:
            output.info(f"  FAILED (exit {rc}, {output.format_duration(elapsed)})")
            output.info(f"  Full log: {log_path}")

        return AnsibleResult(rc=rc, elapsed=elapsed, log_path=log_path)

    def kill(self) -> None:
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

# ── Stateful line parser ─────────────────────────────────────────────

class _LineParser:
    """Stateful parser that tracks the current task to filter output."""

    def __init__(self, start_time: float):
        self.start_time = start_time
        self.current_task_visible = False  # is the current TASK whitelisted?
        self.current_task_label: str | None = None
        self.current_task_deferred = False

    def parse(self, line: str) -> AnsibleEvent | None:
        if not line or line.isspace():
            return None

        stripped = line.rstrip("* ").rstrip()
        elapsed = time.time() - self.start_time

        # PLAY / PLAY RECAP — skip
        if stripped.startswith("PLAY [") or stripped.startswith("PLAY RECAP"):
            return None

        # Timestamp lines — skip
        if re.match(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s", stripped):
            return None

        # [WARNING] / skipping: — skip
        if stripped.startswith("[WARNING]") or stripped.startswith("skipping:"):
            return None

        # TASK [name] — show only whitelisted tasks, track state
        if stripped.startswith("TASK ["):
            task_name = re.sub(r'\]\s*\**\s*$', '', stripped[6:]).strip()
            step_label = _match_step(task_name)
            self.current_task_visible = step_label is not None
            self.current_task_label = step_label
            self.current_task_deferred = (
                task_name == "Reboot for NVIDIA drivers (exit code 100)"
            )
            if step_label:
                if self.current_task_deferred:
                    return None
                return AnsibleEvent(kind="task", task=step_label, elapsed=elapsed)
            return None

        # changed: [host] — only show if current task is visible
        if stripped.startswith("changed:"):
            if not self.current_task_visible:
                return None
            host = _extract_host(stripped)
            return AnsibleEvent(
                kind="host_ok",
                host=host,
                task=self.current_task_label if self.current_task_deferred else None,
                elapsed=elapsed,
            )

        # Standalone "msg" lines from debug tasks (IP display)
        msg_line_match = re.match(r'^\s*"msg"\s*:\s*"(.+)"', stripped)
        if msg_line_match:
            msg_val = msg_line_match.group(1)
            if "=>" in msg_val:
                return AnsibleEvent(kind="host_ok", host=msg_val, elapsed=elapsed)
            return None

        # ok: — skip (changed: is the real signal, msg lines handle debug output)
        if stripped.startswith("ok:"):
            return None

        # fatal: / UNREACHABLE — always show
        if stripped.startswith("fatal:") or "UNREACHABLE" in stripped:
            host = _extract_host(stripped)
            msg = _extract_error_msg(stripped)
            return AnsibleEvent(kind="host_fail", host=host, detail=msg, elapsed=elapsed)

        # ASYNC OK
        if stripped.startswith("ASYNC OK on "):
            host = stripped[len("ASYNC OK on "):].split(":")[0].strip()
            return AnsibleEvent(kind="host_ok", host=host, detail="async", elapsed=elapsed)

        # ASYNC POLL / ASYNC FAILED — skip
        if stripped.startswith("ASYNC POLL") or stripped.startswith("ASYNC FAILED"):
            return None

        # FAILED - RETRYING:
        if "FAILED - RETRYING:" in stripped:
            host = _extract_host(stripped)
            retry_match = re.search(r"Retries left:\s*(\d+)", stripped)
            retry_info = f"retries left: {retry_match.group(1)}" if retry_match else ""
            return AnsibleEvent(kind="retry", host=host, detail=retry_info, elapsed=elapsed)

        # RUSE_RETRY:
        if "RUSE_RETRY:" in stripped:
            host = _extract_host(stripped)
            msg = stripped.split("RUSE_RETRY:")[-1].strip()[:60]
            return AnsibleEvent(kind="retry", host=host, detail=msg, elapsed=elapsed)

        return None


def _match_step(task_name: str) -> str | None:
    """Match a task name to a user-visible step label."""
    if task_name in _STEP_TASKS:
        return _STEP_TASKS[task_name]
    for key, label in _STEP_TASKS.items():
        if task_name.startswith(key):
            return label
    return None


def _extract_host(line: str) -> str:
    """Extract hostname from Ansible output like 'changed: [hostname]'.

    If there's an (item=...) label, append it to the host for context.
    e.g. 'changed: [r-vm-0] => (item=timing_profile.json)' → 'r-vm-0: timing_profile.json'
    e.g. 'changed: [localhost] => (item=r-vm-0 (10.0.0.1))' → 'r-vm-0 (10.0.0.1)'
    """
    match = re.search(r"\[([^\]]+)\]", line)
    if not match:
        return "?"
    host = match.group(1)
    host = host.split(" -> ")[0].strip()

    # Extract (item=...) if present
    item_match = re.search(r"\(item=(.+)\)", line)
    if item_match:
        item = item_match.group(1).strip()
        # Strip trailing "Retries left: ..." from retry lines
        item = re.sub(r"\)\s*Retries left:.*", "", item).strip()
        # Fix unmatched parens
        if item.count("(") > item.count(")"):
            item += ")"

        if not any(host.startswith(p) for p in _VM_PREFIXES):
            # localhost with item = the item IS the host (e.g. VM name)
            return item
        else:
            # VM host with item = show both (e.g. "r-vm-0: timing_profile.json")
            return f"{host}: {item}"

    return host


def _extract_error_msg(line: str) -> str:
    """Extract a readable error message from an Ansible fatal line."""
    msg_match = re.search(r'"msg"\s*:\s*"([^"]{1,120})"', line)
    if msg_match:
        return msg_match.group(1)
    stderr_match = re.search(r'"stderr"\s*:\s*"([^"]{1,120})"', line)
    if stderr_match:
        return stderr_match.group(1)
    host = _extract_host(line)
    return f"{host}: failed"


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# ── Default event handler ────────────────────────────────────────────

def default_event_handler(event: AnsibleEvent) -> None:
    """Plain-text display handler with wall-clock timestamps.

    Format:
      [14:23:50]  Creating VMs on OpenStack
      [14:24:05]    OK  r-controls032226-C0-0
      [14:24:18]    OK  r-controls032226-M0-0
      [14:27:30]  Waiting for VMs to reach ACTIVE state
      [14:27:42]    OK  r-controls032226-C0-0
      [14:28:10]    ..  r-controls032226-M0-0  retries left: 57
      [14:28:40]    OK  r-controls032226-M0-0
      [14:29:15]    FAIL  r-controls032226-S0-llama-0  VM in error state
    """
    ts = time.strftime("%H:%M:%S")

    if event.kind == "task":
        output.info(f"  [{ts}]  {event.task}")
    elif event.kind == "host_ok":
        if event.task:
            output.info(f"  [{ts}]  {event.task}")
        extra = f" ({event.detail})" if event.detail else ""
        output.info(f"  [{ts}]    OK  {event.host}{extra}")
    elif event.kind == "host_fail":
        output.info(f"  [{ts}]    FAIL  {event.host}  {event.detail or 'failed'}")
    elif event.kind == "retry":
        extra = f"  {event.detail}" if event.detail else ""
        output.info(f"  [{ts}]    ..  {event.host}{extra}")
