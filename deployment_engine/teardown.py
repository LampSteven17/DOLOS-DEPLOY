"""Teardown router for exact Phase 3 deployment runs."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .core import output
from .core.ansible_runner import AnsibleRunner, default_event_handler
from .core.config import DeploymentConfig
from .core.openstack import OpenStack
from .core.phase_run_registry import (
    PhaseRunRegistryError,
    close_deployment,
    validate_experiment_id,
    validate_run_id,
)
from .core.ssh_config import remove_all_managed_blocks
from .core.teardown_steps import find_hosts_ini
from .list import has_exact_run_vm


def run_teardown(target: str, deploy_dir: Path) -> int:
    """Teardown one run addressed by deployment name and UTC run ID.

    Dispatches by deploy type to the per-subsystem teardown.
    """
    separator = target[-19:-18] if len(target) >= 19 else ""
    config_name = target[:-19] if separator == "-" else ""
    run_id = target[-18:] if separator == "-" else ""
    try:
        if not separator or not config_name:
            raise PhaseRunRegistryError("missing deployment name or run ID")
        validate_experiment_id(config_name)
        validate_run_id(run_id)
    except PhaseRunRegistryError:
        output.error(
            f"ERROR: Invalid teardown target: {target} "
            "(expected: name-YYYY-MM-DD_HHMMSSZ)"
        )
        return 1

    config_dir = deploy_dir / config_name
    config_file = config_dir / "config.yaml"

    if not config_file.exists():
        output.error(f"ERROR: No config.yaml found for: {config_name}")
        return 1

    run_dir = config_dir / "runs" / run_id
    if not run_dir.is_dir():
        output.error(f"ERROR: No run directory found for: {config_name}/{run_id}")
        return 1

    config = DeploymentConfig.load(config_file)

    if config.is_rampart():
        from .rampart.teardown import run_rampart_teardown
        return run_rampart_teardown(config_dir, config_name, run_id, config, deploy_dir)

    if config.is_ghosts():
        from .ghosts.teardown import run_ghosts_teardown
        return run_ghosts_teardown(config_dir, config_name, run_id, deploy_dir)

    from .decoy.teardown import run_decoy_teardown
    return run_decoy_teardown(config_dir, config_name, run_id, deploy_dir)


def run_teardown_filtered(
    deploy_dir: Path,
    types: dict[str, bool],
    purpose: str | None = None,
    failed_only: bool = False,
) -> int:
    """Teardown all active deployments matching the given filters.

    types: {"decoy": bool, "rampart": bool, "ghosts": bool} — only
    deployments matching any selected type get torn down. If all False,
    matches nothing (caller should prevent that).

    purpose: when set, only target deployments whose explicit configuration
    purpose matches this value.

    failed_only: only target runs stamped FAILED in deploy_status.json
    (see core/run_status.py). Runs with no stamp (UNKNOWN) or an OK stamp
    are left alone — a missing stamp is never treated as failure, so this
    won't delete an in-flight or pre-instrumentation run.
    """
    from .core.run_status import read_run_status, FAILED

    matches: list[tuple[str, str, Path]] = []  # (config_name, run_id, config_dir)
    server_statuses = OpenStack().server_status_map()

    for config_dir in sorted(deploy_dir.iterdir()):
        if not config_dir.is_dir():
            continue
        config_file = config_dir / "config.yaml"
        if not config_file.exists():
            continue

        try:
            config = DeploymentConfig.load(config_file)
        except Exception as e:
            output.error(f"  WARNING: skipping {config_dir.name}/config.yaml: {e}")
            continue

        # Type filter
        if config.is_rampart():
            if not types.get("rampart"):
                continue
        elif config.is_ghosts():
            if not types.get("ghosts"):
                continue
        else:
            if not types.get("decoy"):
                continue

        # Purpose is explicit configuration metadata. Deployment and directory
        # names never determine whether a run is a control or feedback run.
        if purpose is not None and config.purpose != purpose:
            continue

        # Normal filtered teardown follows the same active-run definition as
        # ./list: at least one OpenStack VM under this run's exact prefix.
        # --failed remains the explicit path for locally failed zero-VM runs.
        runs_dir = config_dir / "runs"
        if not runs_dir.is_dir():
            continue
        for run_dir in sorted(runs_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            try:
                validate_run_id(run_dir.name)
            except PhaseRunRegistryError:
                continue
            if failed_only:
                if read_run_status(run_dir) != FAILED:
                    continue
            elif not has_exact_run_vm(
                config_dir.name, run_dir.name, config, server_statuses
            ):
                continue
            matches.append((config_dir.name, run_dir.name, config_dir))

    if not matches:
        scope = "failed deployments" if failed_only else "deployments"
        output.info(f"No {scope} match the filter.")
        return 0

    label = "FAILED-ONLY TEARDOWN" if failed_only else "FILTERED TEARDOWN"
    output.banner(f"{label} — {len(matches)} deployments")
    for cn, rid, _ in matches:
        output.info(f"  {cn}/{rid}")
    output.info("")

    # Single-step confirm — the listed deployments above already make the
    # scope explicit, and the filter (e.g. --ghosts) is narrower than --all.
    # The two-step "DELETE ALL" prompt is reserved for run_teardown_all.
    if not output.confirm(f"Confirm teardown of {len(matches)} deployments?"):
        output.info("Teardown cancelled.")
        return 0

    # Parallel fan-out via subprocess. Each child runs its own teardown
    # CLI invocation in isolation — own OpenStack auth, own session log,
    # own ansible runs. Concurrency-safe because:
    #   - ~/.ssh/config edits are fcntl-locked (core/ssh_config.py)
    #   - each PHASE run closes under its own run-directory lock
    #   - per-deploy state lives in distinct config_dir/runs/{rid}/ trees
    #   - OpenStack handles concurrent server/volume DELETEs natively
    # Sequential serial run was 8 × ~3min = ~25min; parallel run is bounded
    # by the slowest single teardown (~3min).
    import subprocess
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    repo_root = Path(__file__).resolve().parent.parent
    teardown_script = repo_root / "teardown"
    logs_dir = deploy_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    parallel_ts = _time.strftime("%Y%m%d-%H%M%S")

    def _one(idx: int, cn: str, rid: str) -> tuple[int, str, str, int, Path, float]:
        target = f"{cn}-{rid}"
        log_path = logs_dir / f"teardown-parallel-{parallel_ts}-{cn}-{rid}.log"
        t0 = _time.monotonic()
        with open(log_path, "w") as log_f:
            log_f.write(f"# Parallel teardown child for {target}\n# Started {_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            log_f.flush()
            # CI=1 keeps the child non-interactive (it has nothing to confirm —
            # the parent already collected the y/N for the whole batch).
            env = {**os.environ, "CI": "1"}
            proc = subprocess.run(
                [str(teardown_script), target],
                cwd=str(repo_root),
                stdout=log_f, stderr=subprocess.STDOUT,
                env=env,
            )
        elapsed = _time.monotonic() - t0
        return idx, cn, rid, proc.returncode, log_path, elapsed

    output.info(f"Running {len(matches)} teardowns in parallel...")
    output.info("(per-deployment output captured to logs/teardown-parallel-*.log)")
    output.info("")

    failures = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=len(matches)) as ex:
        futures = [
            ex.submit(_one, i, cn, rid)
            for i, (cn, rid, _) in enumerate(matches, 1)
        ]
        for fut in as_completed(futures):
            idx, cn, rid, rc, log_path, elapsed = fut.result()
            completed += 1
            status = "OK  " if rc == 0 else f"FAIL"
            ts = _time.strftime("%H:%M:%S")
            output.info(
                f"  [{ts}] [{completed}/{len(matches)}] {status}  {cn}-{rid}  "
                f"({int(elapsed//60)}m{int(elapsed%60):02d}s)  →  {log_path.name}"
            )
            if rc != 0:
                failures += 1

    output.info("")
    if failures:
        output.error(f"DONE: {len(matches) - failures}/{len(matches)} succeeded, {failures} failed")
        return 1
    output.info(f"DONE: all {len(matches)} torn down")
    return 0


def run_teardown_all(deploy_dir: Path) -> int:
    """Delete ALL DECOY (d-*), RAMPART (r-*), and GHOSTS (g-*) servers + volumes.

    Uses teardown-all.yaml which sweeps by regex — no per-type dispatch.
    Local run directories are cleaned up afterward by walking the deploy
    dir and removing any inventory.ini that points at the now-gone VMs.
    """
    output.banner("TEARDOWN ALL")
    output.info("This will DELETE ALL DECOY (d-*), RAMPART (r-*), and GHOSTS (g-*) servers and volumes!")
    output.info("")

    if not output.confirm_destructive("Confirm teardown-all?"):
        output.info("Teardown cancelled.")
        return 0

    hosts_ini = find_hosts_ini(None, deploy_dir)
    if not hosts_ini:
        output.error("ERROR: No hosts.ini found")
        return 1

    runner = AnsibleRunner(deploy_dir / "logs")
    output.info("")
    output.section("[Teardown]")

    result = runner.run_playbook(
        "shared/teardown-all.yaml",
        hosts_ini,
        extra_vars={"deployment_dir": str(deploy_dir)},
        on_event=default_event_handler,
    )

    if result.rc != 0:
        output.error("ERROR: teardown-all failed; local state was preserved")
        return result.rc

    # Close only current Phase 3 records. Historical and invalid run
    # directories remain untouched and are never interpreted as deployments.
    close_failures = 0
    for config_dir in deploy_dir.iterdir():
        if not config_dir.is_dir():
            continue
        runs_dir = config_dir / "runs"
        if not runs_dir.is_dir():
            continue
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            try:
                validate_run_id(run_dir.name)
            except PhaseRunRegistryError:
                continue
            try:
                close_deployment(
                    config_dir.name,
                    run_dir.name,
                    ended_at=datetime.now(timezone.utc),
                )
            except PhaseRunRegistryError as exc:
                close_failures += 1
                output.error(
                    f"  ERROR: PHASE deployment close failed for "
                    f"{config_dir.name}/{run_dir.name}: {exc}"
                )
                continue

    if close_failures:
        output.error("ERROR: one or more deployment records could not be closed")
        return 1

    removed = remove_all_managed_blocks()
    if removed:
        output.info(f"  Removed {len(removed)} SSH config blocks")
    return 0
