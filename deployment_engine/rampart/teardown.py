"""RAMPART enterprise teardown — single deployment.

RAMPART teardown is direct OpenStack iteration (no Ansible playbook for
deletion). Pre-step kills emulate.pid; mid-step deletes the per-deploy
DNS zone. After VM deletion, finalize_teardown closes the exact registry
record and removes its managed SSH block while retaining run history.

Performance: every `openstack` CLI call costs ~17s (python startup +
auth). Step 2 batches all 23 VM deletes into a single `--wait` call
(was 23 × 17s ≈ 6 min serial), and step 3's zone delete runs in
parallel with the VM-wait, since they're independent OpenStack ops.
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

from ..core import output
from ..core.config import DeploymentConfig
from ..core.openstack import OpenStack
from ..core.teardown_steps import finalize_teardown
from ..core.vm_naming import make_ent_vm_prefix, make_run_dep_id


def run_rampart_teardown(
    config_dir: Path, config_name: str, run_id: str,
    config: DeploymentConfig, deploy_dir: Path,
) -> int:
    """Teardown a RAMPART enterprise deployment."""
    run_dir = config_dir / "runs" / run_id

    output.banner(f"TEARDOWN: {config_name}/{run_id} (rampart)")

    dep_id = make_run_dep_id(config_name, run_id)
    ent_vm_prefix = make_ent_vm_prefix(dep_id)
    os_client = OpenStack()

    # Step 1: Kill emulation
    output.info("[1/3] Stopping emulation...")
    pid_file = run_dir / "emulate.pid"
    if pid_file.exists():
        _kill_pid_file(pid_file)
    else:
        output.info("  No emulate.pid found -- skipping")

    # Identify VM cohort up front so the zone-delete thread can run while
    # the batch VM delete --wait blocks.
    output.info("[2/3] Deleting VMs...")
    servers = os_client.server_list_with_ids(prefix=ent_vm_prefix)
    if servers:
        output.info(f"  Deleting {len(servers)} VMs (prefix {ent_vm_prefix})...")
        for s in servers:
            output.dim(f"    queued {s['name']}")
    else:
        output.info("  No RAMPART VMs found")

    # Kick off zone delete on a side thread; it's an independent OpenStack
    # API and runs concurrently with the VM-delete wait below.
    zone_result: dict = {"deleted": False, "msg": ""}

    def _delete_zone():
        zone_marker = run_dir / "dns_zone.txt"
        if zone_marker.exists():
            zone_name = zone_marker.read_text().strip()
            z = os_client.zone_find(zone_name)
            if z:
                os_client.zone_delete(z["id"])
                zone_result.update(deleted=True, msg=f"Deleted DNS zone: {zone_name}")
                return
            zone_result.update(deleted=True, msg=f"Zone not found: {zone_name} (already deleted?)")
            return
        # Fallback for pre-zone-isolation deployments: match zones containing
        # this deployment's hash. Extract from the `r-{hash}-` prefix.
        ent_hash = ent_vm_prefix[2:-1]
        for z in os_client.zone_list():
            zname = z.get("name", "")
            if ent_hash in zname:
                os_client.zone_delete(z["id"])
                zone_result.update(deleted=True, msg=f"Deleted DNS zone: {zname}")
                return

    zone_thread = threading.Thread(target=_delete_zone, name="rampart-zone-delete")
    zone_thread.start()

    # Batch VM delete with --wait. One CLI invocation handles all servers
    # and blocks until OpenStack reports each gone. finalize_teardown's
    # poll_for_zero check below then completes on the first iteration.
    delete_ok = True
    if servers:
        ok_delete = os_client.server_delete_many(
            [s["id"] for s in servers], wait=True,
        )
        if ok_delete:
            output.info(f"  Deleted {len(servers)} VMs")
        else:
            delete_ok = False
            output.error("  WARNING: server_delete_many reported non-zero rc")

    output.info("[3/3] Cleaning up DNS zone...")
    zone_thread.join()
    if zone_result["msg"]:
        output.info(f"  {zone_result['msg']}")
    if not zone_result["deleted"]:
        output.info("  No DNS zones found for this deployment")

    if not delete_ok:
        output.error("ERROR: VM teardown failed; registry and local state were preserved")
        return 1

    # Shared epilogue: close exact registry record, then remove its SSH block.
    # poll_for_zero=True is still cheap here — `--wait` already drained
    # the cohort so wait_until_zero returns on its first iteration.
    ok = finalize_teardown(
        config_name, config_dir, run_id, run_dir,
        vm_prefix=ent_vm_prefix,
        feedback_marker="rampart-feedback-",
        poll_for_zero=True,
    )
    if ok:
        output.info("")
        output.info("DONE: all RAMPART VMs deleted")
    return 0 if ok else 1


def _kill_pid_file(pid_file: Path) -> None:
    """Kill a process from a PID file. RAMPART-only — emulate.pid pattern."""
    try:
        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            output.dim(f"  Killed emulation PID {pid}")
        except ProcessLookupError:
            output.dim(f"  Emulation PID {pid} already stopped")
    except (ValueError, FileNotFoundError):
        output.dim("  Could not read emulate.pid")
