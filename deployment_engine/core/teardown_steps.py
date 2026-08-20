"""Shared teardown helpers used by all three subsystem teardowns.

Lives here so per-type teardown modules (decoy/teardown.py,
rampart/teardown.py, ghosts/teardown.py) can import from one place
without importing from each other or from the router (teardown.py).
"""

from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path

from . import output
from .openstack import OpenStack
from .phase_run_registry import (
    PhaseRunRegistryError,
    close_deployment,
)


def find_hosts_ini(config_dir: Path | None, deploy_dir: Path) -> Path | None:
    """Find hosts.ini, checking config dir first, then deploy_dir root."""
    if config_dir and (config_dir / "hosts.ini").exists():
        return config_dir / "hosts.ini"
    if (deploy_dir / "hosts.ini").exists():
        return deploy_dir / "hosts.ini"
    for d in deploy_dir.iterdir():
        if d.is_dir() and (d / "hosts.ini").exists():
            return d / "hosts.ini"
    return None


def make_dep_id(deployment_name: str, run_id: str) -> str:
    """Build the deployment ID used in VM names: {name_no_prefix}{run_id}.

    Strips the deploy_type prefix (decoy-/rampart-/ghosts-/legacy-enterprise-)
    so all three subsystems produce the same compact identifier shape.
    """
    dep = deployment_name
    for prefix in ("decoy-", "ghosts-", "rampart-", "enterprise-"):
        if dep.startswith(prefix):
            dep = dep[len(prefix):]
    dep = dep.replace("-", "")
    return f"{dep}{run_id}"


def cleanup_orphaned_volumes(os_client: OpenStack) -> int:
    """Delete orphaned boot volumes (nameless, 200GB, available).

    All three subsystems provision 200GB boot disks; one shared filter.

    Returns the number deleted. Batches IDs into one CLI call — each
    `openstack` invocation is ~17s of python+auth overhead, so deleting
    23 orphans serially used to add ~6 min on top of VM teardown.
    """
    orphans = os_client.find_orphaned_volumes(size=200)
    if not orphans:
        return 0
    ids = [v.get("ID", v.get("id", "")) for v in orphans]
    ids = [i for i in ids if i]
    if not ids:
        return 0
    if os_client.volume_delete_many(ids):
        output.info(f"  Cleaned up {len(ids)} orphaned boot volumes")
        return len(ids)
    return 0


def safe_rmtree(path: Path) -> None:
    """Recursively remove a directory; swallow OSError."""
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def wait_until_zero(
    os_client: OpenStack, vm_prefix: str,
    *, attempts: int = 20, delay_s: int = 5,
) -> int:
    """Poll OpenStack until no VMs match `vm_prefix`. Returns final count.

    Used by RAMPART/GHOSTS teardowns where VM deletion is async (no Ansible
    playbook to do the wait). DECOY's teardown.yaml waits internally via
    its own retry loop, so this isn't called there.
    """
    import time
    for attempt in range(1, attempts + 1):
        os_client.invalidate_cache()
        remaining = os_client.count_vms_with_prefix(vm_prefix)
        if remaining == 0:
            return 0
        output.info(f"  Waiting for {remaining} VMs to delete... ({attempt}/{attempts})")
        time.sleep(delay_s)
    os_client.invalidate_cache()
    return os_client.count_vms_with_prefix(vm_prefix)


def finalize_teardown(
    config_name: str, config_dir: Path, run_id: str, run_dir: Path,
    vm_prefix: str,
    *,
    feedback_marker: str | None = None,
    poll_for_zero: bool = False,
) -> bool:
    """Shared epilogue for every teardown.

    Steps:
      1. remove_ssh_config block
      2. verify VMs gone (poll_for_zero=True polls; False does one-shot count
         — DECOY's playbook already waited internally)
      3. cleanup_orphaned_volumes
      4. close the exact phase-run-v1 deployment record
      5. safe_rmtree run_dir
      6. if config name starts with feedback_marker, drop the empty config dir

    Returns True on success, False if VMs are still alive (caller should
    return non-zero).
    """
    from .ssh_config import remove_ssh_config
    remove_ssh_config(f"{config_name}/{run_id}")

    os_client = OpenStack()
    if poll_for_zero:
        remaining = wait_until_zero(os_client, vm_prefix)
    else:
        remaining = os_client.count_vms_with_prefix(vm_prefix)

    if remaining > 0:
        output.info("")
        output.info(f"WARNING: {remaining} VMs still exist on OpenStack (prefix: {vm_prefix})")
        output.info("Local state preserved. Re-run teardown or use --all.")
        return False

    output.info(f"  Verified: 0 VMs remaining on OpenStack (prefix: {vm_prefix})")
    cleanup_orphaned_volumes(os_client)
    try:
        close_deployment(
            config_name,
            run_id,
            ended_at=datetime.datetime.now(datetime.timezone.utc),
        )
        output.info(f"  Closed PHASE deployment record: {config_name}/{run_id}")
    except PhaseRunRegistryError as exc:
        output.error(f"  ERROR: PHASE deployment close failed: {exc}")
        output.info("  Local state preserved. Re-run teardown after fixing the record.")
        return False

    if run_dir.is_dir():
        safe_rmtree(run_dir)
        output.info(f"  Removed local run directory: {run_dir.name}")

    if feedback_marker and config_name.startswith(feedback_marker):
        runs_dir = config_dir / "runs"
        remaining_runs = (
            [d for d in runs_dir.iterdir() if d.is_dir()]
            if runs_dir.is_dir() else []
        )
        if not remaining_runs:
            safe_rmtree(config_dir)
            output.info(f"  Removed empty feedback config directory: {config_dir.name}")

    return True
