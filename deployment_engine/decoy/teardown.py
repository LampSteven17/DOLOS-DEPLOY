"""Deadline-bounded teardown for one exact Decoy deployment run."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..core import output
from ..core.openstack import OpenStack, OpenStackCommandError
from ..core.teardown_steps import finalize_verified_teardown
from ..core.vm_naming import make_run_dep_id, make_vm_prefix


TEARDOWN_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 5.0


class TeardownDeadlineExpired(RuntimeError):
    """The one deployment-wide teardown deadline expired."""


class TeardownOperationFailed(RuntimeError):
    """A scoped OpenStack delete request failed."""


def run_decoy_teardown(
    config_dir: Path, config_name: str, run_id: str, deploy_dir: Path,
) -> int:
    """Delete one exact Decoy VM/boot-volume cohort within five minutes."""
    del deploy_dir  # Decoy teardown no longer uses an Ansible inventory.
    deadline = time.monotonic() + TEARDOWN_TIMEOUT_S
    run_dir = config_dir / "runs" / run_id
    if not run_dir.is_dir():
        output.error(f"ERROR: No run directory found for: {config_name}/{run_id}")
        return 1

    output.banner(f"TEARDOWN: {config_name}/{run_id}")
    vm_prefix = make_vm_prefix(make_run_dep_id(config_name, run_id))
    os_client = OpenStack()
    last_servers: list[dict] = []
    captured_volumes: set[str] = set()
    last_volumes: dict[str, str] = {}
    ordinary_requested_ids: set[str] = set()
    forced_ids: set[str] = set()

    try:
        output.info("[1/2] Deleting exact Decoy VM cohort...")
        last_servers = _query_servers(os_client, vm_prefix, deadline)
        _capture_volumes(last_servers, captured_volumes)
        if last_servers:
            _delete_exact_servers(
                os_client,
                last_servers,
                ordinary_requested_ids,
                forced_ids,
                deadline,
            )
        else:
            output.info("  No Decoy VMs found")

        while last_servers:
            last_servers = _query_servers(os_client, vm_prefix, deadline)
            _capture_volumes(last_servers, captured_volumes)
            if not last_servers:
                break

            # A same-run VM appearing during the poll is still exact-scope.
            # Capture its volume before issuing its first delete request.
            _delete_exact_servers(
                os_client,
                last_servers,
                ordinary_requested_ids,
                forced_ids,
                deadline,
            )

            output.info(
                f"  Waiting for {len(last_servers)} exact VMs to disappear..."
            )
            _sleep_within_deadline(deadline)

        output.info(f"  Verified: 0 VMs remaining (prefix: {vm_prefix})")

        output.info("[2/2] Deleting captured boot volumes...")
        last_volumes = os_client.volume_statuses(
            captured_volumes, timeout_s=_remaining(deadline)
        )
        volume_delete_requested = False
        while last_volumes:
            error_volumes = {
                volume_id: status
                for volume_id, status in last_volumes.items()
                if status.upper() == "ERROR"
            }
            if error_volumes:
                raise TeardownOperationFailed(
                    "captured boot volumes entered ERROR: "
                    + ", ".join(sorted(error_volumes))
                )
            if (
                not volume_delete_requested
                and all(status.upper() == "AVAILABLE" for status in last_volumes.values())
            ):
                volume_ids = sorted(last_volumes)
                if not os_client.volume_delete_many(
                    volume_ids, timeout_s=_remaining(deadline)
                ):
                    raise TeardownOperationFailed(
                        "batch volume delete failed for captured IDs: "
                        + ", ".join(volume_ids)
                    )
                volume_delete_requested = True
                output.info(
                    f"  Delete requested for {len(volume_ids)} boot volumes"
                )
            elif not volume_delete_requested:
                output.info(
                    f"  Waiting for {len(last_volumes)} boot volumes to detach..."
                )
            _sleep_within_deadline(deadline)
            last_volumes = os_client.volume_statuses(
                captured_volumes, timeout_s=_remaining(deadline)
            )
            if last_volumes:
                output.info(
                    f"  Waiting for {len(last_volumes)} captured volumes to disappear..."
                )

        output.info(f"  Verified: 0 of {len(captured_volumes)} captured volumes remain")
        _remaining(deadline)
    except (
        OpenStackCommandError,
        TeardownDeadlineExpired,
        TeardownOperationFailed,
        subprocess.TimeoutExpired,
    ) as exc:
        reason = (
            "five-minute teardown deadline expired"
            if isinstance(exc, subprocess.TimeoutExpired)
            else str(exc)
        )
        last_servers, last_volumes = _refresh_diagnostics(
            os_client,
            vm_prefix,
            captured_volumes,
            deadline,
            last_servers,
            last_volumes,
        )
        faults = _collect_remaining_faults(os_client, last_servers, deadline)
        _report_incomplete(
            reason, last_servers, faults, captured_volumes, last_volumes
        )
        return 1

    # Registry closure and SSH removal are deliberately outside the resource
    # loop and occur only after both exact cohorts have verified empty.
    return 0 if finalize_verified_teardown(config_name, run_id) else 1


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TeardownDeadlineExpired("five-minute teardown deadline expired")
    return remaining


def _query_servers(
    os_client: OpenStack, vm_prefix: str, deadline: float
) -> list[dict]:
    return os_client.server_cohort(
        vm_prefix, timeout_s=_remaining(deadline)
    )


def _capture_volumes(servers: list[dict], captured: set[str]) -> None:
    for server in servers:
        captured.update(server.get("volume_ids", []))


def _delete_exact_servers(
    os_client: OpenStack,
    servers: list[dict],
    ordinary_requested_ids: set[str],
    forced_ids: set[str],
    deadline: float,
) -> None:
    error_ids = [
        server["id"]
        for server in servers
        if server["status"].upper() == "ERROR" and server["id"] not in forced_ids
    ]
    if error_ids:
        if not os_client.server_force_delete_many(
            error_ids, timeout_s=_remaining(deadline)
        ):
            raise TeardownOperationFailed(
                "force-delete failed for exact ERROR VM IDs: "
                + ", ".join(error_ids)
            )
        forced_ids.update(error_ids)
        output.info(f"  Force-delete requested for {len(error_ids)} ERROR VMs")

    ordinary_ids = [
        server["id"]
        for server in servers
        if server["status"].upper() != "ERROR"
        and server["id"] not in ordinary_requested_ids
        and server["id"] not in forced_ids
    ]
    if ordinary_ids:
        if not os_client.server_delete_many(
            ordinary_ids, wait=False, timeout_s=_remaining(deadline)
        ):
            raise TeardownOperationFailed(
                "batch VM delete failed for exact IDs: " + ", ".join(ordinary_ids)
            )
        ordinary_requested_ids.update(ordinary_ids)
        output.info(f"  Delete requested for {len(ordinary_ids)} exact VMs")


def _sleep_within_deadline(deadline: float) -> None:
    time.sleep(min(POLL_INTERVAL_S, _remaining(deadline)))


def _refresh_diagnostics(
    os_client: OpenStack,
    vm_prefix: str,
    captured_volumes: set[str],
    deadline: float,
    servers: list[dict],
    volumes: dict[str, str],
) -> tuple[list[dict], dict[str, str]]:
    """Best-effort final cohort snapshot without resetting the deadline."""
    try:
        servers = _query_servers(os_client, vm_prefix, deadline)
        _capture_volumes(servers, captured_volumes)
    except (OpenStackCommandError, TeardownDeadlineExpired, subprocess.TimeoutExpired):
        pass
    try:
        volumes = os_client.volume_statuses(
            captured_volumes, timeout_s=_remaining(deadline)
        )
    except (OpenStackCommandError, TeardownDeadlineExpired, subprocess.TimeoutExpired):
        pass
    return servers, volumes


def _collect_remaining_faults(
    os_client: OpenStack, servers: list[dict], deadline: float
) -> dict[str, str | None]:
    """Inspect failure-only faults within the original teardown deadline."""
    faults: dict[str, str | None] = {}
    for server in servers:
        try:
            timeout_s = _remaining(deadline)
        except TeardownDeadlineExpired:
            break
        try:
            faults[server["id"]] = os_client.server_fault(
                server["id"], timeout_s=timeout_s
            )
        except (OSError, subprocess.SubprocessError) as exc:
            faults[server["id"]] = f"fault query failed: {exc}"
    return faults


def _report_incomplete(
    reason: str,
    servers: list[dict],
    faults: dict[str, str | None],
    captured_volumes: set[str],
    volumes: dict[str, str],
) -> None:
    output.error(f"ERROR: Decoy teardown incomplete: {reason}")
    output.error("  Remaining exact VMs:")
    if servers:
        for server in servers:
            fault = faults.get(server["id"])
            output.error(
                f"    {server['name']} id={server['id']} "
                f"status={server['status']} fault={fault or 'none reported'}"
            )
    else:
        output.error("    none in last successful cohort query")
    output.error("  Captured boot volumes:")
    if captured_volumes:
        for volume_id in sorted(captured_volumes):
            status = volumes.get(volume_id, "not reported by last volume query")
            output.error(f"    {volume_id} status={status}")
    else:
        output.error("    none captured")
    output.error("  Registry record remains open; SSH state was preserved.")
