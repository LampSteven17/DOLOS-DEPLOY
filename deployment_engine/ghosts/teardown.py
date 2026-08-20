"""GHOSTS NPC teardown — single deployment.

Direct OpenStack VM deletion (no Ansible playbook). After deletion,
finalize_teardown closes the exact registry record and retains run history.
"""

from __future__ import annotations

from pathlib import Path

from ..core import output
from ..core.openstack import OpenStack
from ..core.teardown_steps import finalize_teardown
from ..core.vm_naming import make_ghosts_vm_prefix, make_run_dep_id


def run_ghosts_teardown(
    config_dir: Path, config_name: str, run_id: str, deploy_dir: Path,
) -> int:
    """Teardown a GHOSTS deployment."""
    run_dir = config_dir / "runs" / run_id

    output.banner(f"TEARDOWN: {config_name}/{run_id} (ghosts)")

    dep_id = make_run_dep_id(config_name, run_id)
    g_prefix = make_ghosts_vm_prefix(dep_id)
    os_client = OpenStack()

    # Step 1: Delete VMs.
    # Each `openstack` CLI call costs ~17s (python startup + auth), so
    # the historical serial loop scaled linearly with N. Batch with
    # --wait collapses N round trips into one ~17s invocation that
    # blocks until OpenStack reports them gone.
    output.info("[1/1] Deleting GHOSTS VMs...")
    servers = os_client.server_list_with_ids(prefix=g_prefix)
    if servers:
        output.info(f"  Deleting {len(servers)} VMs...")
        for s in servers:
            output.dim(f"    queued {s['name']}")
        ok_delete = os_client.server_delete_many(
            [s["id"] for s in servers], wait=True,
        )
        if ok_delete:
            output.info(f"  Deleted {len(servers)} VMs")
        else:
            output.error("  WARNING: server_delete_many reported non-zero rc")
            output.error("ERROR: VM teardown failed; registry and local state were preserved")
            return 1
    else:
        output.info("  No GHOSTS VMs found")

    # Shared epilogue: close exact registry record, then remove its SSH block.
    # poll_for_zero=True — direct OpenStack delete is async.
    ok = finalize_teardown(
        config_name, config_dir, run_id, run_dir,
        vm_prefix=g_prefix,
        feedback_marker="ghosts-feedback-",
        poll_for_zero=True,
    )
    if ok:
        output.info("")
        output.info("DONE: all GHOSTS VMs deleted")
    return 0 if ok else 1
