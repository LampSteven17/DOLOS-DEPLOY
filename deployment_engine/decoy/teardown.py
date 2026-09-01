"""Deadline-bounded teardown for one exact Decoy deployment run."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from ..core import output
from ..core.openstack import OpenStack, OpenStackCommandError
from ..core.teardown_steps import finalize_verified_teardown
from ..core.vm_naming import make_run_dep_id, make_vm_prefix


TEARDOWN_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 5.0
FORCE_DELETE_GRACE_S = 75.0


class TeardownDeadlineExpired(RuntimeError):
    """The one deployment-wide teardown deadline expired."""


class TeardownOperationFailed(RuntimeError):
    """A scoped teardown operation reached an unsafe resource state."""


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
    delete_states: dict[str, dict] = {}
    failure_evidence: dict[str, dict] = {}
    volume_inspected_ids: set[str] = set()

    try:
        output.info("[1/2] Deleting exact Decoy VM cohort...")
        last_servers = _query_servers(os_client, vm_prefix, deadline)
        _capture_attached_volumes(
            os_client,
            last_servers,
            volume_inspected_ids,
            captured_volumes,
            deadline,
        )
        if not last_servers:
            output.info("  No Decoy VMs found")

        while last_servers:
            _capture_attached_volumes(
                os_client,
                last_servers,
                volume_inspected_ids,
                captured_volumes,
                deadline,
            )
            last_servers, advanced = _advance_server_deletion(
                os_client,
                vm_prefix,
                last_servers,
                delete_states,
                failure_evidence,
                deadline,
            )
            if not last_servers:
                break
            if advanced:
                continue

            output.info(
                f"  Waiting for {len(last_servers)} exact VMs to disappear..."
            )
            _sleep_within_deadline(deadline)
            last_servers = _query_servers(os_client, vm_prefix, deadline)

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
                delete_ok = os_client.volume_delete_many(
                    volume_ids, timeout_s=_remaining(deadline)
                )
                if delete_ok:
                    volume_delete_requested = True
                    output.info(
                        f"  Delete requested for {len(volume_ids)} boot volumes"
                    )
                else:
                    output.info(
                        "  Volume delete command returned nonzero; "
                        "reconciling exact captured IDs"
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
        _collect_remaining_evidence(
            os_client, last_servers, deadline, failure_evidence
        )
        faults = _collect_remaining_faults(
            os_client, last_servers, deadline, failure_evidence
        )
        _report_incomplete(
            reason,
            last_servers,
            faults,
            failure_evidence,
            captured_volumes,
            last_volumes,
        )
        return 1

    # Registry closure and SSH removal are deliberately outside the resource
    # loop and occur only after both exact cohorts have verified empty.
    return 0 if finalize_verified_teardown(config_name, run_id, run_dir) else 1


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


def _capture_attached_volumes(
    os_client: OpenStack,
    servers: list[dict],
    inspected_ids: set[str],
    captured: set[str],
    deadline: float,
) -> None:
    """Use server show once per discovered exact VM, before deletion."""
    for server in servers:
        server_id = server["id"]
        if server_id in inspected_ids:
            continue
        captured.update(
            os_client.server_attached_volume_ids(
                server_id, timeout_s=_remaining(deadline)
            )
        )
        inspected_ids.add(server_id)


def _advance_server_deletion(
    os_client: OpenStack,
    vm_prefix: str,
    servers: list[dict],
    states: dict[str, dict],
    evidence: dict[str, dict],
    deadline: float,
) -> tuple[list[dict], bool]:
    """Advance at most one deletion transition, then reconcile the cohort."""
    for server in servers:
        states.setdefault(
            server["id"],
            {
                "ordinary_requested": False,
                "first_force_at": None,
                "recovery_started": False,
                "final_force_requested": False,
                "prior_delete_request_ids": None,
            },
        )

    for server in servers:
        state = states[server["id"]]
        if state["final_force_requested"]:
            _fail_on_completed_final_delete_error(
                os_client, server, state, evidence, deadline
            )

    error_ids = [
        server["id"] for server in servers
        if server["status"].upper() == "ERROR"
        and states[server["id"]]["first_force_at"] is None
    ]
    if error_ids:
        requested_at = time.monotonic()
        for server_id in error_ids:
            states[server_id]["first_force_at"] = requested_at
        delete_ok = os_client.server_force_delete_many(
            error_ids, timeout_s=_remaining(deadline)
        )
        if delete_ok:
            output.info(f"  Force-delete requested for {len(error_ids)} ERROR VMs")
        else:
            output.info(
                "  Force-delete command returned nonzero; "
                "reconciling exact VM cohort"
            )
        return _query_servers(os_client, vm_prefix, deadline), True

    ordinary_ids = [
        server["id"]
        for server in servers
        if server["status"].upper() != "ERROR"
        and not states[server["id"]]["ordinary_requested"]
        and states[server["id"]]["first_force_at"] is None
    ]
    if ordinary_ids:
        for server_id in ordinary_ids:
            states[server_id]["ordinary_requested"] = True
        delete_ok = os_client.server_delete_many(
            ordinary_ids, wait=False, timeout_s=_remaining(deadline)
        )
        if delete_ok:
            output.info(f"  Delete requested for {len(ordinary_ids)} exact VMs")
        else:
            output.info(
                "  VM delete command returned nonzero; "
                "reconciling exact VM cohort"
            )
        return _query_servers(os_client, vm_prefix, deadline), True

    now = time.monotonic()
    for server in servers:
        state = states[server["id"]]
        first_force_at = state["first_force_at"]
        if (
            server["status"].upper() != "ERROR"
            or first_force_at is None
            or state["recovery_started"]
            or now - first_force_at < FORCE_DELETE_GRACE_S
        ):
            continue

        try:
            lifecycle = os_client.server_lifecycle(
                server["id"], timeout_s=_remaining(deadline)
            )
        except OpenStackCommandError:
            refreshed = _query_servers(os_client, vm_prefix, deadline)
            if _find_server(refreshed, server["id"]) is None:
                return refreshed, True
            raise
        evidence.setdefault(server["id"], {})["lifecycle"] = lifecycle
        if not _is_recoverable_stuck_error(lifecycle):
            continue
        return (
            _recover_stuck_error(
                os_client,
                vm_prefix,
                server,
                state,
                evidence,
                deadline,
            ),
            True,
        )

    return servers, False


def _recover_stuck_error(
    os_client: OpenStack,
    vm_prefix: str,
    server: dict,
    state: dict,
    evidence: dict[str, dict],
    deadline: float,
) -> list[dict]:
    """Run the one allowed exact-ID recovery sequence for a stuck ERROR VM."""
    server_id = server["id"]
    state["recovery_started"] = True
    powered_on = _is_powered_on(evidence[server_id]["lifecycle"])
    stop_step = "stop, then " if powered_on else "skip stop, then "
    output.info(
        f"  Recovering stuck ERROR VM {server['name']} ({server_id}): "
        f"reset ACTIVE, {stop_step}final force-delete"
    )

    reset_ok = os_client.server_reset_state_active(
        server_id, timeout_s=_remaining(deadline)
    )
    servers = _query_servers(os_client, vm_prefix, deadline)
    if _find_server(servers, server_id) is None:
        return servers
    if not reset_ok:
        output.info("  Reset-state returned nonzero; exact VM still present")

    stop_ok = True
    if powered_on:
        stop_ok = os_client.server_stop(
            server_id, timeout_s=_remaining(deadline)
        )
        servers = _query_servers(os_client, vm_prefix, deadline)
        if _find_server(servers, server_id) is None:
            return servers
        if not stop_ok:
            output.info("  Stop returned nonzero; proceeding to final force-delete")
    else:
        output.info("  VM was powered off; skipping stop before final force-delete")

    if powered_on and reset_ok and stop_ok:
        while True:
            current = _find_server(servers, server_id)
            if current is None:
                return servers
            if current["status"].upper() in {"SHUTOFF", "ERROR"}:
                break
            _sleep_within_deadline(deadline)
            servers = _query_servers(os_client, vm_prefix, deadline)

    state["prior_delete_request_ids"] = _delete_request_ids(
        os_client, server_id, deadline
    )
    state["final_force_requested"] = True
    delete_ok = os_client.server_force_delete_many(
        [server_id], timeout_s=_remaining(deadline)
    )
    if delete_ok:
        output.info(f"  Final force-delete requested for {server['name']}")
    else:
        output.info(
            "  Final force-delete returned nonzero; reconciling exact VM"
        )
    servers = _query_servers(os_client, vm_prefix, deadline)
    current = _find_server(servers, server_id)
    if current is not None:
        _fail_on_completed_final_delete_error(
            os_client, current, state, evidence, deadline
        )
    return servers


def _find_server(servers: list[dict], server_id: str) -> dict | None:
    return next((server for server in servers if server["id"] == server_id), None)


def _is_recoverable_stuck_error(lifecycle: dict) -> bool:
    task_state = lifecycle.get("task_state")
    task_idle = task_state is None or str(task_state).strip().lower() in {
        "", "none", "null",
    }
    return task_idle


def _is_powered_on(lifecycle: dict) -> bool:
    power_state = lifecycle.get("power_state")
    try:
        return int(str(power_state).strip()) != 0
    except (TypeError, ValueError):
        return False


def _delete_request_ids(
    os_client: OpenStack, server_id: str, deadline: float
) -> set[str] | None:
    """Snapshot delete request IDs so only the final request can fail fast."""
    try:
        events = os_client.server_events(
            server_id, timeout_s=_remaining(deadline)
        )
    except OpenStackCommandError:
        return None
    if not isinstance(events, list):
        return None
    return {
        str(request_id)
        for event in events
        if "delete" in str(
            _event_value(event, "Action", "action") or ""
        ).lower()
        if (request_id := _event_value(
            event, "Request ID", "Request Id", "request_id"
        ))
    }


def _fail_on_completed_final_delete_error(
    os_client: OpenStack,
    server: dict,
    state: dict,
    evidence: dict[str, dict],
    deadline: float,
) -> None:
    """Fail promptly only for a completed error from the final delete request."""
    prior_request_ids = state.get("prior_delete_request_ids")
    if prior_request_ids is None:
        return
    current_evidence: dict = {}
    _collect_one_server_evidence(
        os_client, server, deadline, current_evidence
    )
    if current_evidence:
        evidence.setdefault(server["id"], {}).update(current_evidence)
    delete_event = current_evidence.get("delete_event")
    if not isinstance(delete_event, dict):
        return
    request_id = delete_event.get("request_id")
    if not request_id or request_id in prior_request_ids:
        return
    result = str(delete_event.get("result") or "").strip().lower()
    if result == "error" and delete_event.get("finish"):
        raise TeardownOperationFailed(
            "final force-delete completed with Error for exact VM "
            f"{server['name']} ({server['id']}), request_id={request_id}"
        )


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
    os_client: OpenStack,
    servers: list[dict],
    deadline: float,
    evidence: dict[str, dict],
) -> dict[str, str | None]:
    """Inspect failure-only faults within the original teardown deadline."""
    faults: dict[str, str | None] = {}
    for server in servers:
        lifecycle = evidence.get(server["id"], {}).get("lifecycle", {})
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        if lifecycle.get("fault") not in (None, "", {}):
            faults[server["id"]] = str(lifecycle["fault"])
            continue
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


def _collect_remaining_evidence(
    os_client: OpenStack,
    servers: list[dict],
    deadline: float,
    evidence: dict[str, dict],
) -> None:
    """Best-effort lifecycle/event evidence under the original deadline."""
    for server in servers:
        try:
            _remaining(deadline)
        except TeardownDeadlineExpired:
            return
        _collect_one_server_evidence(
            os_client, server, deadline, evidence.setdefault(server["id"], {})
        )


def _collect_one_server_evidence(
    os_client: OpenStack,
    server: dict,
    deadline: float,
    evidence: dict,
) -> None:
    """Capture exact-server lifecycle and latest delete event when possible."""
    try:
        lifecycle = os_client.server_lifecycle(
            server["id"], timeout_s=_remaining(deadline)
        )
    except (
        OpenStackCommandError,
        TeardownDeadlineExpired,
        subprocess.TimeoutExpired,
    ):
        return
    if not isinstance(lifecycle, dict):
        return
    evidence["lifecycle"] = lifecycle

    try:
        events = os_client.server_events(
            server["id"], timeout_s=_remaining(deadline)
        )
    except (
        OpenStackCommandError,
        TeardownDeadlineExpired,
        subprocess.TimeoutExpired,
    ):
        return
    if not isinstance(events, list):
        return
    delete_events = [
        event for event in events
        if "delete" in str(_event_value(event, "Action", "action") or "").lower()
    ]
    if not delete_events:
        return
    summary = max(
        delete_events,
        key=lambda event: str(
            _event_value(
                event,
                "Start Time",
                "start_time",
                "Created At",
                "created_at",
            ) or ""
        ),
    )
    request_id = _event_value(
        summary, "Request ID", "Request Id", "request_id"
    )
    if not request_id:
        return
    try:
        details = os_client.server_event_show(
            server["id"], str(request_id), timeout_s=_remaining(deadline)
        )
    except (
        OpenStackCommandError,
        TeardownDeadlineExpired,
        subprocess.TimeoutExpired,
    ):
        return
    compute_events = details.get("events", details.get("Events", []))
    if isinstance(compute_events, str):
        try:
            compute_events = json.loads(compute_events)
        except (TypeError, json.JSONDecodeError):
            compute_events = []
    if not isinstance(compute_events, list):
        compute_events = []
    compute = next(
        (
            item for item in reversed(compute_events)
            if isinstance(item, dict)
            and _event_value(item, "event", "Event") == "compute_terminate_instance"
        ),
        None,
    )
    evidence["delete_event"] = {
        "request_id": str(request_id),
        "result": _event_value(compute or {}, "result", "Result"),
        "start": _event_value(
            compute or summary, "start_time", "Start Time", "created_at"
        ),
        "finish": _event_value(
            compute or details, "finish_time", "Finish Time", "updated_at"
        ),
    }


def _event_value(mapping: dict, *keys: str):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _report_incomplete(
    reason: str,
    servers: list[dict],
    faults: dict[str, str | None],
    evidence: dict[str, dict],
    captured_volumes: set[str],
    volumes: dict[str, str],
) -> None:
    output.error(f"ERROR: Decoy teardown incomplete: {reason}")
    output.error("  Remaining exact VMs:")
    if servers:
        for server in servers:
            lifecycle = evidence.get(server["id"], {}).get("lifecycle", {})
            if not isinstance(lifecycle, dict):
                lifecycle = {}
            fault = faults.get(server["id"])
            output.error(
                f"    {server['name']} id={server['id']} "
                f"host={lifecycle.get('host') or 'unknown'} "
                f"status={server['status']} "
                f"vm_state={lifecycle.get('vm_state') or 'unknown'} "
                f"task_state={_display_value(lifecycle.get('task_state'))} "
                f"power_state={_display_value(lifecycle.get('power_state'))} "
                f"fault={fault or 'none reported'}"
            )
            delete_event = evidence.get(server["id"], {}).get("delete_event")
            if delete_event:
                output.error(
                    "      latest_delete_event "
                    f"request_id={delete_event['request_id']} "
                    f"compute_result={delete_event.get('result') or 'unknown'} "
                    f"start={delete_event.get('start') or 'unknown'} "
                    f"finish={delete_event.get('finish') or 'unknown'}"
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


def _display_value(value) -> str:
    return "null" if value is None else str(value)
