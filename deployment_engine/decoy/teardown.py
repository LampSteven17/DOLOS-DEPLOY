"""DECOY SUP teardown — single deployment.

DECOY uses an Ansible playbook (teardown.yaml) which deletes VMs, waits
for completion, and removes inventory in one shot. The playbook does its
own polling, so finalize_teardown does a one-shot count rather than a
poll loop.
"""

from __future__ import annotations

from pathlib import Path

from ..core import output
from ..core.ansible_runner import AnsibleRunner, default_event_handler
from ..core.teardown_steps import finalize_teardown, find_hosts_ini
from ..core.vm_naming import make_run_dep_id, make_vm_prefix


def run_decoy_teardown(
    config_dir: Path, config_name: str, run_id: str, deploy_dir: Path,
) -> int:
    """Teardown a DECOY SUP deployment via teardown.yaml + shared epilogue."""
    run_dir = config_dir / "runs" / run_id
    if not run_dir.is_dir():
        output.error(f"ERROR: No run directory found for: {config_name}/{run_id}")
        return 1

    output.banner(f"TEARDOWN: {config_name}/{run_id}")

    hosts_ini = find_hosts_ini(config_dir, deploy_dir)
    if not hosts_ini:
        output.info("ERROR: No hosts.ini found")
        return 1

    dep_id = make_run_dep_id(config_name, run_id)
    vm_prefix = make_vm_prefix(dep_id)

    runner = AnsibleRunner(deploy_dir / "logs")

    result = runner.run_playbook(
        "decoy/teardown.yaml",
        hosts_ini,
        extra_vars={
            "deployment_dir": str(config_dir),
            "deployment_id": dep_id,
            "run_dir": str(run_dir),
            # Pass vm_prefix explicitly. Playbook used to default to
            # `r-{deployment_id}-` (legacy), which silently matched zero
            # VMs since DECOYs are `d-`. See 8c35214.
            "vm_prefix": vm_prefix,
        },
        on_event=default_event_handler,
    )

    if result.rc != 0:
        output.error("ERROR: VM teardown failed; registry and local state were preserved")
        return result.rc

    ok = finalize_teardown(
        config_name, config_dir, run_id, run_dir,
        vm_prefix=vm_prefix,
        feedback_marker="decoy-feedback-",
        poll_for_zero=False,  # playbook already waited
    )
    return 0 if ok else 1
