"""Shared deploy-step helpers used by every per-type spinup.

Three things every spinup does post-provision:
  1. SSH connectivity test — parallel probe across the new VMs
  2. PHASE registration — write one phase-run-v1 deployment record
  3. SSH config snippet install — covered by core/ssh_config.py
     (caller-side because the VM list shape differs per type)
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import output
from .config import DeploymentConfig
from .phase_run_registry import (
    PhaseRunRegistryError,
    create_deployment,
)


def ssh_connectivity_test(
    hosts: list[dict],
    *,
    key_path: Path | None = None,
    user: str = "ubuntu",
    max_retries: int = 30,
    timeout: int = 10,
    delay: int = 5,
    workers: int = 20,
) -> int:
    """Parallel SSH probe with real-time per-VM output. Returns count reachable.

    Each host dict needs `name` and `ip` keys. Default key is
    ~/.ssh/id_ed25519 (DECOY/GHOSTS); RAMPART/Linux endpoints can
    override via key_path. SSH_AUTH_SOCK is unset in subprocess env to
    avoid OpenStack VM auth-timeouts (see CLAUDE.md).
    """
    if key_path is None:
        key_path = Path.home() / ".ssh" / "id_ed25519"

    ok_count = 0

    def _probe_one(host: dict) -> bool:
        name = host["name"]
        ip = host["ip"]
        for attempt in range(1, max_retries + 1):
            ts = time.strftime("%H:%M:%S")
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        "-i", str(key_path),
                        "-o", "IdentitiesOnly=yes",
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", f"ConnectTimeout={timeout}",
                        "-o", "ConnectionAttempts=1",
                        "-o", "BatchMode=yes",
                        "-o", "LogLevel=ERROR",
                        f"{user}@{ip}", "echo ok",
                    ],
                    capture_output=True, timeout=timeout + 5,
                    env={**os.environ, "SSH_AUTH_SOCK": ""},
                )
                if result.returncode == 0:
                    output.info(f"  [{ts}]    OK  {name} ({ip})")
                    return True
            except subprocess.TimeoutExpired:
                pass
            output.info(f"  [{ts}]    ..  {name} ({ip})  attempt {attempt}/{max_retries}")
            time.sleep(delay)

        ts = time.strftime("%H:%M:%S")
        output.info(f"  [{ts}]    FAIL  {name} ({ip})  unreachable after {max_retries} attempts")
        return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_probe_one, h): h for h in hosts}
        for future in concurrent.futures.as_completed(futures):
            if future.result():
                ok_count += 1

    return ok_count


def register_phase_run(
    config: DeploymentConfig,
    system: str,
    started_at: datetime,
    vms: list[dict],
) -> bool:
    """Create the one exact PHASE deployment record for a completed spinup."""
    try:
        run_id, path = create_deployment(
            experiment_id=config.deployment_name,
            system=system,
            purpose=config.purpose,
            target=config.target,
            started_at=started_at,
            capture_interface=config.capture_interface,
            vms=vms,
        )
    except PhaseRunRegistryError as exc:
        output.error(f"  ERROR: PHASE deployment registration failed: {exc}")
        return False
    output.dim(f"  Registered PHASE run {run_id}: {path}")
    return True


def neighborhood_vms(run_dir: Path) -> list[dict]:
    """Return DECOY sidecar VMs with explicit null SUP configuration."""
    inv = run_dir / "neighborhood-inventory.ini"
    if not inv.exists():
        return []
    vms = []
    import re
    for line in inv.read_text().splitlines():
        m = re.match(r"^(\S+)\s+ansible_host=(\S+)", line)
        if m:
            vms.append({"name": m.group(1), "ip": m.group(2), "sup_config": None})
    return vms


def share_sidecar_vms(run_dir: Path) -> list[dict]:
    """Return the exact fleet-local share VM with null SUP configuration."""
    inv = run_dir / "inventory.ini"
    if not inv.exists():
        return []
    vms = []
    import re
    for line in inv.read_text().splitlines():
        match = re.match(
            r"^(\S+)\s+ansible_host=(\S+).*\bshare_sidecar=true\b", line
        )
        if match:
            vms.append({
                "name": match.group(1),
                "ip": match.group(2),
                "sup_config": None,
            })
    return vms


def infrastructure_vms_from_ssh_config(snippet_path: Path) -> list[dict]:
    """Read actual infrastructure VM names and IPs from a RUSE SSH snippet."""
    if not snippet_path.is_file():
        return []
    vms = []
    current_name = None
    for raw_line in snippet_path.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith("Host "):
            name = line.removeprefix("Host ").strip()
            current_name = None if "*" in name else name
        elif current_name is not None and line.startswith("HostName "):
            vms.append({
                "name": current_name,
                "ip": line.removeprefix("HostName ").strip(),
                "sup_config": None,
            })
            current_name = None
    return vms
