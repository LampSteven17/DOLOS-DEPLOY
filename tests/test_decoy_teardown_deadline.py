from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest import mock

from deployment_engine.core.openstack import OpenStack
from deployment_engine.decoy import teardown


RUN_ID = "2026-08-20_175127Z"
CONFIG_NAME = "decoy-controls"


def _server(
    server_id: str,
    name: str,
    status: str,
    *volume_ids: str,
) -> dict:
    return {
        "id": server_id,
        "name": name,
        "status": status,
        "volume_ids": list(volume_ids),
    }


class _Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Cloud:
    def __init__(
        self,
        clock: _Clock,
        *,
        servers: list[list[dict]],
        volumes: list[dict[str, str]] | None = None,
        delete_ok: bool = True,
        force_ok: bool = True,
        volume_delete_ok: bool = True,
        faults: dict[str, str] | None = None,
        lifecycles: dict[str, dict] | None = None,
        reset_ok: bool = True,
        stop_ok: bool = True,
        events: dict[str, list[dict]] | None = None,
        event_details: dict[tuple[str, str], dict] | None = None,
        call_cost: float = 0.1,
    ):
        self.clock = clock
        self.server_results = list(servers)
        self.volume_results = list(volumes or [{}])
        self.delete_ok = delete_ok
        self.force_ok = force_ok
        self.volume_delete_ok = volume_delete_ok
        self.faults = faults or {}
        self.lifecycles = lifecycles or {}
        self.reset_ok = reset_ok
        self.stop_ok = stop_ok
        self.events = events or {}
        self.event_details = event_details or {}
        self.attachments: dict[str, list[str]] = {}
        for snapshot in servers:
            for server in snapshot:
                self.attachments.setdefault(
                    server["id"], list(server.get("volume_ids", []))
                )
        self.call_cost = call_cost
        self.calls: list[tuple] = []
        self.timeouts: list[float] = []

    def _consume(self, name: str, timeout_s: float | None, *args) -> None:
        self.calls.append((name, *args))
        if timeout_s is not None:
            self.timeouts.append(timeout_s)
        self.clock.now += self.call_cost

    @staticmethod
    def _next(results: list):
        if len(results) > 1:
            return results.pop(0)
        return results[0]

    def server_cohort(self, prefix: str, *, timeout_s: float) -> list[dict]:
        self._consume("server_cohort", timeout_s, prefix)
        return deepcopy(self._next(self.server_results))

    def server_attached_volume_ids(
        self, server_id: str, *, timeout_s: float
    ) -> list[str]:
        self._consume("server_attached_volume_ids", timeout_s, server_id)
        return list(self.attachments.get(server_id, []))

    def server_delete_many(
        self, ids: list[str], *, wait: bool, timeout_s: float
    ) -> bool:
        self._consume("server_delete_many", timeout_s, tuple(ids), wait)
        return self.delete_ok

    def server_force_delete_many(
        self, ids: list[str], *, timeout_s: float
    ) -> bool:
        self._consume("server_force_delete_many", timeout_s, tuple(ids))
        return self.force_ok

    def server_lifecycle(self, server_id: str, *, timeout_s: float) -> dict:
        self._consume("server_lifecycle", timeout_s, server_id)
        return deepcopy(self.lifecycles.get(server_id, {
            "host": "axes-test.maas",
            "status": "ERROR",
            "vm_state": "error",
            "task_state": None,
            "power_state": 1,
            "fault": self.faults.get(server_id),
        }))

    def server_reset_state_active(
        self, server_id: str, *, timeout_s: float
    ) -> bool:
        self._consume("server_reset_state_active", timeout_s, server_id)
        return self.reset_ok

    def server_stop(self, server_id: str, *, timeout_s: float) -> bool:
        self._consume("server_stop", timeout_s, server_id)
        return self.stop_ok

    def server_events(self, server_id: str, *, timeout_s: float) -> list[dict]:
        self._consume("server_events", timeout_s, server_id)
        result = self.events.get(server_id, [])
        if result and isinstance(result[0], list):
            result = self._next(result)
        return deepcopy(result)

    def server_event_show(
        self, server_id: str, request_id: str, *, timeout_s: float
    ) -> dict:
        self._consume("server_event_show", timeout_s, server_id, request_id)
        return deepcopy(self.event_details.get((server_id, request_id), {}))

    def server_fault(
        self, server_id: str, *, timeout_s: float | None = None
    ) -> str | None:
        self._consume("server_fault", timeout_s, server_id)
        return self.faults.get(server_id)

    def volume_statuses(
        self, ids: set[str], *, timeout_s: float
    ) -> dict[str, str]:
        self._consume("volume_statuses", timeout_s, tuple(sorted(ids)))
        result = self._next(self.volume_results)
        return {key: value for key, value in result.items() if key in ids}

    def volume_delete_many(
        self, ids: list[str], *, timeout_s: float
    ) -> bool:
        self._consume("volume_delete_many", timeout_s, tuple(ids))
        return self.volume_delete_ok


class DecoyTeardownDeadlineTests(unittest.TestCase):
    def _run(
        self,
        cloud: _Cloud,
        clock: _Clock,
        *,
        timeout_s: float = 10.0,
        grace_s: float = 3.0,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        deploy_dir = Path(temporary.name)
        config_dir = deploy_dir / CONFIG_NAME
        (config_dir / "runs" / RUN_ID).mkdir(parents=True)
        final_calls: list[tuple[str, str, Path]] = []

        def finalize(config_name: str, run_id: str, run_dir: Path) -> bool:
            cloud.calls.append(("finalize", config_name, run_id, run_dir))
            final_calls.append((config_name, run_id, run_dir))
            return True

        stderr = StringIO()
        with (
            mock.patch.object(teardown, "OpenStack", return_value=cloud),
            mock.patch.object(teardown, "TEARDOWN_TIMEOUT_S", timeout_s),
            mock.patch.object(teardown, "POLL_INTERVAL_S", 1.0),
            mock.patch.object(teardown, "FORCE_DELETE_GRACE_S", grace_s),
            mock.patch.object(teardown.time, "monotonic", side_effect=clock.monotonic),
            mock.patch.object(teardown.time, "sleep", side_effect=clock.sleep),
            mock.patch.object(
                teardown, "finalize_verified_teardown", side_effect=finalize
            ),
            redirect_stderr(stderr),
        ):
            result = teardown.run_decoy_teardown(
                config_dir, CONFIG_NAME, RUN_ID, deploy_dir
            )
        return result, final_calls, stderr.getvalue()

    def test_batches_exact_vms_then_volumes_under_one_nonresetting_deadline(self):
        clock = _Clock()
        prefix = "d-exact-"
        cloud = _Cloud(
            clock,
            servers=[
                [
                    _server("vm-1", prefix + "scripted", "ACTIVE", "vol-1"),
                    _server("vm-2", prefix + "mchp", "ACTIVE", "vol-2"),
                ],
                [
                    _server("vm-1", prefix + "scripted", "DELETING", "vol-1"),
                    _server("vm-2", prefix + "mchp", "ERROR", "vol-2"),
                ],
                [],
            ],
            volumes=[
                {"vol-1": "in-use", "vol-2": "in-use"},
                {"vol-1": "available", "vol-2": "available"},
                {},
            ],
            faults={"vm-2": "host failure"},
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value=prefix):
            result, final_calls, _ = self._run(cloud, clock)

        self.assertEqual(result, 0)
        self.assertEqual(
            [(name, run_id) for name, run_id, _ in final_calls],
            [(CONFIG_NAME, RUN_ID)],
        )
        self.assertIn(
            ("server_delete_many", ("vm-1", "vm-2"), False), cloud.calls
        )
        self.assertIn(("server_force_delete_many", ("vm-2",)), cloud.calls)
        self.assertIn(("volume_delete_many", ("vol-1", "vol-2")), cloud.calls)
        names = [call[0] for call in cloud.calls]
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_attached_volume_ids"],
            [
                ("server_attached_volume_ids", "vm-1"),
                ("server_attached_volume_ids", "vm-2"),
            ],
        )
        self.assertLess(
            names.index("server_attached_volume_ids"), names.index("server_delete_many")
        )
        self.assertLess(names.index("server_delete_many"), names.index("server_cohort", 1))
        self.assertLess(names.index("server_force_delete_many"), names.index("volume_delete_many"))
        self.assertLess(names.index("volume_delete_many"), names.index("finalize"))
        self.assertTrue(
            all(later < earlier for earlier, later in zip(cloud.timeouts, cloud.timeouts[1:]))
        )
        self.assertNotIn("server_fault", names)

    def test_initial_error_first_force_delete_succeeds_without_recovery(self):
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[
                [
                    _server("vm-error", "d-exact-error", "ERROR", "vol-error"),
                ],
                [],
            ],
            volumes=[{"vol-error": "available"}, {}],
            faults={"vm-error": "must not be inspected on success"},
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(cloud, clock)

        self.assertEqual(result, 0)
        self.assertEqual(
            [(name, run_id) for name, run_id, _ in final_calls],
            [(CONFIG_NAME, RUN_ID)],
        )
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_force_delete_many"],
            [("server_force_delete_many", ("vm-error",))],
        )
        self.assertFalse(any(call[0] == "server_reset_state_active" for call in cloud.calls))
        self.assertFalse(any(call[0] == "server_fault" for call in cloud.calls))

    def test_normal_delete_is_submitted_once_without_recovery(self):
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[
                [_server("vm-active", "d-exact-active", "ACTIVE", "vol-1")],
                [],
            ],
            volumes=[{"vol-1": "available"}, {}],
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(cloud, clock)

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_delete_many"],
            [("server_delete_many", ("vm-active",), False)],
        )
        self.assertFalse(any(
            call[0] in {
                "server_force_delete_many",
                "server_reset_state_active",
                "server_stop",
            }
            for call in cloud.calls
        ))

    def test_stuck_error_runs_one_recovery_sequence_then_disappears(self):
        clock = _Clock()
        error = _server("vm-error", "d-exact-error", "ERROR", "vol-error")
        cloud = _Cloud(
            clock,
            servers=[
                [error],
                [error],
                [error],
                [error],
                [error],
                [_server("vm-error", "d-exact-error", "ACTIVE", "vol-error")],
                [_server("vm-error", "d-exact-error", "ACTIVE", "vol-error")],
                [_server("vm-error", "d-exact-error", "SHUTOFF", "vol-error")],
                [],
            ],
            volumes=[{"vol-error": "available"}, {}],
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(cloud, clock)

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_force_delete_many"],
            [
                ("server_force_delete_many", ("vm-error",)),
                ("server_force_delete_many", ("vm-error",)),
            ],
        )
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_reset_state_active"],
            [("server_reset_state_active", "vm-error")],
        )
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_stop"],
            [("server_stop", "vm-error")],
        )
        names = [call[0] for call in cloud.calls]
        self.assertGreater(
            names.index("volume_statuses"),
            max(
                index for index, item in enumerate(names)
                if item == "server_force_delete_many"
            ),
        )

    def test_force_delete_is_not_repeated_during_75_second_grace(self):
        self.assertEqual(teardown.FORCE_DELETE_GRACE_S, 75.0)
        clock = _Clock()
        error = _server("vm-error", "d-exact-error", "ERROR", "vol-error")
        cloud = _Cloud(clock, servers=[[error]], call_cost=0.0)
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(
                cloud, clock, timeout_s=5.0, grace_s=75.0
            )

        self.assertEqual(result, 1)
        self.assertEqual(final_calls, [])
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_force_delete_many"],
            [("server_force_delete_many", ("vm-error",))],
        )
        self.assertFalse(any(call[0] == "server_reset_state_active" for call in cloud.calls))

    def test_recovery_requires_idle_task_state(self):
        error = _server("vm-error", "d-exact-error", "ERROR", "vol-error")
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[[error]],
            lifecycles={
                "vm-error": {
                    "host": "axes-test.maas",
                    "status": "ERROR",
                    "vm_state": "error",
                    "fault": None,
                    "task_state": "deleting",
                    "power_state": 0,
                }
            },
            call_cost=0.0,
        )
        with mock.patch.object(
            teardown, "make_vm_prefix", return_value="d-exact-"
        ):
            result, final_calls, _ = self._run(
                cloud, clock, timeout_s=4.0, grace_s=1.0
            )
        self.assertEqual(result, 1)
        self.assertEqual(final_calls, [])
        self.assertEqual(
            len([
                call for call in cloud.calls
                if call[0] == "server_force_delete_many"
            ]),
            1,
        )
        self.assertFalse(any(
            call[0] == "server_reset_state_active"
            for call in cloud.calls
        ))

    def test_powered_off_idle_error_resets_skips_stop_and_final_force_deletes(self):
        clock = _Clock()
        error = _server("vm-error", "d-exact-error", "ERROR", "vol-error")
        cloud = _Cloud(
            clock,
            servers=[[error], [error], [error], []],
            volumes=[{"vol-error": "available"}, {}],
            lifecycles={
                "vm-error": {
                    "host": "axes-test.maas",
                    "status": "ERROR",
                    "vm_state": "error",
                    "task_state": None,
                    "power_state": 0,
                    "fault": None,
                }
            },
            call_cost=0.0,
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, rendered = self._run(
                cloud, clock, timeout_s=10.0, grace_s=0.0
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_force_delete_many"],
            [
                ("server_force_delete_many", ("vm-error",)),
                ("server_force_delete_many", ("vm-error",)),
            ],
        )
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_reset_state_active"],
            [("server_reset_state_active", "vm-error")],
        )
        self.assertFalse(any(call[0] == "server_stop" for call in cloud.calls))
        self.assertIn("skipping stop", rendered)

    def test_nonzero_force_delete_succeeds_when_exact_vm_is_verified_absent(self):
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[
                [_server("vm-error", "d-exact-one", "ERROR")],
                [],
            ],
            force_ok=False,
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(cloud, clock)

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_force_delete_many"],
            [("server_force_delete_many", ("vm-error",))],
        )

    def test_nonzero_ordinary_delete_succeeds_when_exact_vm_is_verified_absent(self):
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[
                [_server("vm-active", "d-exact-one", "ACTIVE")],
                [],
            ],
            delete_ok=False,
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(cloud, clock)

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_delete_many"],
            [("server_delete_many", ("vm-active",), False)],
        )

    def test_nonzero_volume_delete_succeeds_when_volume_is_verified_absent(self):
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[
                [_server("vm-1", "d-exact-one", "ACTIVE", "vol-1")],
                [],
            ],
            volumes=[{"vol-1": "available"}, {}],
            volume_delete_ok=False,
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(cloud, clock)

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "volume_delete_many"],
            [("volume_delete_many", ("vol-1",))],
        )

    def test_nonzero_reset_or_stop_reconciles_absent_vm(self):
        error = _server("vm-error", "d-exact-one", "ERROR", "vol-error")
        cases = (
            (
                "reset",
                [[error], [error], []],
                {"reset_ok": False},
                "server_reset_state_active",
            ),
            (
                "stop",
                [
                    [error],
                    [error],
                    [_server("vm-error", "d-exact-one", "ACTIVE", "vol-error")],
                    [],
                ],
                {"stop_ok": False},
                "server_stop",
            ),
        )
        for name, snapshots, options, expected_command in cases:
            with self.subTest(name=name):
                clock = _Clock()
                cloud = _Cloud(
                    clock,
                    servers=snapshots,
                    volumes=[{"vol-error": "available"}, {}],
                    **options,
                )
                with mock.patch.object(
                    teardown, "make_vm_prefix", return_value="d-exact-"
                ):
                    result, final_calls, _ = self._run(
                        cloud, clock, grace_s=0.0
                    )
                self.assertEqual(result, 0)
                self.assertEqual(len(final_calls), 1)
                self.assertEqual(
                    len([
                        call for call in cloud.calls
                        if call[0] == expected_command
                    ]),
                    1,
                )
                self.assertEqual(
                    len([
                        call for call in cloud.calls
                        if call[0] == "server_force_delete_many"
                    ]),
                    1,
                )

    def test_completed_final_delete_error_fails_immediately_with_evidence(self):
        clock = _Clock()
        exact = _server("vm-error", "d-exact-one", "ERROR", "vol-error")
        cloud = _Cloud(
            clock,
            servers=[
                [_server("vm-error", "d-exact-one", "ACTIVE", "vol-error")],
                [exact],
                [exact],
                [exact],
                [_server("vm-error", "d-exact-one", "ACTIVE", "vol-error")],
                [_server("vm-error", "d-exact-one", "SHUTOFF", "vol-error")],
                [exact],
            ],
            volumes=[{"vol-error": "in-use"}],
            force_ok=False,
            lifecycles={
                "vm-error": {
                    "host": "axes-2u19.maas",
                    "status": "ERROR",
                    "vm_state": "error",
                    "task_state": None,
                    "power_state": 1,
                    "fault": "InternalServerError",
                }
            },
            events={
                "vm-error": [
                    [{
                        "Request ID": "req-first",
                        "Action": "delete",
                        "Start Time": "2026-08-28T23:09:00Z",
                    }],
                    [
                        {
                            "Request ID": "req-first",
                            "Action": "delete",
                            "Start Time": "2026-08-28T23:09:00Z",
                        },
                        {
                            "Request ID": "req-final",
                            "Action": "delete",
                            "Start Time": "2026-08-28T23:11:00Z",
                        },
                    ],
                ]
            },
            event_details={
                ("vm-error", "req-final"): {
                    "events": [{
                        "event": "compute_terminate_instance",
                        "result": "Error",
                        "start_time": "2026-08-28T23:11:00Z",
                        "finish_time": "2026-08-28T23:12:02Z",
                    }]
                }
            },
            call_cost=0.0,
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, rendered = self._run(
                cloud, clock, timeout_s=8.0, grace_s=1.0
            )

        self.assertEqual(result, 1)
        self.assertEqual(final_calls, [])
        self.assertNotIn("five-minute teardown deadline expired", rendered)
        self.assertIn("final force-delete completed with Error", rendered)
        self.assertIn("d-exact-one id=vm-error host=axes-2u19.maas", rendered)
        self.assertIn(
            "status=ERROR vm_state=error task_state=null power_state=1", rendered
        )
        self.assertIn("fault=InternalServerError", rendered)
        self.assertIn("request_id=req-final compute_result=Error", rendered)
        self.assertIn("start=2026-08-28T23:11:00Z", rendered)
        self.assertIn("finish=2026-08-28T23:12:02Z", rendered)
        self.assertIn("Registry record remains open; SSH state was preserved", rendered)
        self.assertEqual(
            len([call for call in cloud.calls if call[0] == "server_force_delete_many"]),
            2,
        )
        self.assertFalse(any(call[0] == "finalize" for call in cloud.calls))
        self.assertFalse(any(call[0] == "volume_delete_many" for call in cloud.calls))

    def test_vm_absence_overrides_final_delete_error_and_allows_finalization(self):
        clock = _Clock()
        error = _server("vm-error", "d-exact-one", "ERROR", "vol-error")
        cloud = _Cloud(
            clock,
            servers=[
                [error],
                [error],
                [_server("vm-error", "d-exact-one", "ACTIVE", "vol-error")],
                [_server("vm-error", "d-exact-one", "SHUTOFF", "vol-error")],
                [],
            ],
            volumes=[{"vol-error": "available"}, {}],
            force_ok=False,
            events={
                "vm-error": [[{
                    "Request ID": "req-first",
                    "Action": "delete",
                    "Start Time": "2026-08-28T23:09:00Z",
                }]]
            },
            event_details={
                ("vm-error", "req-final"): {
                    "events": [{
                        "event": "compute_terminate_instance",
                        "result": "Error",
                        "start_time": "2026-08-28T23:11:00Z",
                        "finish_time": "2026-08-28T23:12:02Z",
                    }]
                }
            },
            call_cost=0.0,
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(
                cloud, clock, timeout_s=10.0, grace_s=0.0
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertIn(("volume_delete_many", ("vol-error",)), cloud.calls)
        self.assertEqual(
            len([call for call in cloud.calls if call[0] == "server_force_delete_many"]),
            2,
        )

    def test_one_deadline_times_out_without_closing_or_resetting(self):
        clock = _Clock()
        stuck = _server("vm-stuck", "d-exact-stuck", "DELETING", "vol-stuck")
        cloud = _Cloud(clock, servers=[[stuck]], call_cost=0.0)
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, rendered = self._run(
                cloud, clock, timeout_s=2.0
            )

        self.assertEqual(result, 1)
        self.assertEqual(final_calls, [])
        self.assertIn("five-minute teardown deadline expired", rendered)
        self.assertIn("vm-stuck", rendered)
        self.assertIn("status=DELETING", rendered)
        self.assertIn("vol-stuck", rendered)
        self.assertFalse(any(call[0] == "server_fault" for call in cloud.calls))

    def test_deadline_starts_with_the_deployment_worker(self):
        clock = _Clock()
        clock.now = 900.0
        cloud = _Cloud(clock, servers=[[]], call_cost=0.0)
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, _ = self._run(
                cloud, clock, timeout_s=300.0
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(cloud.timeouts[0], 300.0)

    def test_persistent_nonzero_volume_delete_times_out_without_finalizing(self):
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[
                [_server("vm-1", "d-exact-one", "ACTIVE", "vol-1")],
                [],
                [],
            ],
            volumes=[{"vol-1": "available"}],
            volume_delete_ok=False,
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, rendered = self._run(cloud, clock)

        self.assertEqual(result, 1)
        self.assertEqual(final_calls, [])
        self.assertIn("five-minute teardown deadline expired", rendered)
        self.assertIn("vol-1 status=available", rendered)
        self.assertGreater(
            len([call for call in cloud.calls if call[0] == "volume_delete_many"]),
            1,
        )

    def test_decoy_teardown_has_no_ansible_or_sequential_wait_playbook(self):
        source = Path(teardown.__file__).read_text(encoding="utf-8")
        self.assertNotIn("AnsibleRunner", source)
        self.assertNotIn("run_playbook", source)
        self.assertFalse(
            (
                Path(__file__).resolve().parents[1]
                / "deployment_engine"
                / "playbooks"
                / "decoy"
                / "teardown.yaml"
            ).exists()
        )
        self.assertEqual(
            source.count("time.monotonic() + TEARDOWN_TIMEOUT_S"), 1
        )
        self.assertNotIn("server_status_map", source)
        self.assertNotIn("server_list_with_ids", source)


class OpenStackCohortTests(unittest.TestCase):
    def test_list_without_volume_data_requires_server_show_capture(self):
        rows = [
            {
                "ID": "vm-1",
                "Name": "d-exact-one",
                "Status": "ACTIVE",
            },
            {
                "ID": "vm-2",
                "Name": "d-exact-two",
                "Status": "ERROR",
            },
            {
                "ID": "unrelated",
                "Name": "d-exactly-not-this-run",
                "Status": "ACTIVE",
            },
        ]
        list_result = subprocess.CompletedProcess([], 0, json.dumps(rows), "")
        show_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"volumes_attached": [{"id": "vol-1"}]}),
            "",
        )
        client = OpenStack()
        with mock.patch.object(
            client, "_run", side_effect=[list_result, show_result]
        ) as command:
            cohort = client.server_cohort("d-exact-", timeout_s=17.0)
            volume_ids = client.server_attached_volume_ids(
                "vm-1", timeout_s=16.0
            )

        self.assertEqual(
            cohort,
            [
                {"id": "vm-1", "name": "d-exact-one", "status": "ACTIVE"},
                {"id": "vm-2", "name": "d-exact-two", "status": "ERROR"},
            ],
        )
        self.assertEqual(volume_ids, ["vol-1"])
        self.assertNotIn("Volumes Attached", command.call_args_list[0].args)
        self.assertEqual(command.call_args_list[0].kwargs["timeout_s"], 17.0)
        self.assertEqual(command.call_args_list[1].kwargs["timeout_s"], 16.0)

    def test_exact_server_recovery_commands_and_lifecycle_fields(self):
        lifecycle_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({
                "OS-EXT-SRV-ATTR:host": "axes-2u19.maas",
                "status": "ERROR",
                "OS-EXT-STS:vm_state": "error",
                "OS-EXT-STS:task_state": None,
                "OS-EXT-STS:power_state": 1,
                "fault": {"message": "InternalServerError"},
            }),
            "",
        )
        ok = subprocess.CompletedProcess([], 0, "", "")
        client = OpenStack()
        with mock.patch.object(
            client, "_run", side_effect=[lifecycle_result, ok, ok]
        ) as command:
            lifecycle = client.server_lifecycle("vm-exact", timeout_s=30.0)
            reset = client.server_reset_state_active(
                "vm-exact", timeout_s=29.0
            )
            stopped = client.server_stop("vm-exact", timeout_s=28.0)

        self.assertEqual(
            lifecycle,
            {
                "host": "axes-2u19.maas",
                "status": "ERROR",
                "vm_state": "error",
                "task_state": None,
                "power_state": 1,
                "fault": '{"message":"InternalServerError"}',
            },
        )
        self.assertTrue(reset)
        self.assertTrue(stopped)
        self.assertEqual(
            command.call_args_list[1].args,
            ("server", "set", "--state", "active", "vm-exact"),
        )
        self.assertEqual(
            command.call_args_list[2].args,
            ("server", "stop", "vm-exact"),
        )

    def test_exact_server_event_queries_use_server_and_request_ids(self):
        list_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps([{"Request ID": "req-delete", "Action": "delete"}]),
            "",
        )
        show_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({
                "events": [{
                    "event": "compute_terminate_instance",
                    "result": "Error",
                }]
            }),
            "",
        )
        client = OpenStack()
        with mock.patch.object(
            client, "_run", side_effect=[list_result, show_result]
        ) as command:
            events = client.server_events("vm-exact", timeout_s=27.0)
            details = client.server_event_show(
                "vm-exact", "req-delete", timeout_s=26.0
            )

        self.assertEqual(events[0]["Request ID"], "req-delete")
        self.assertEqual(
            details["events"][0]["event"], "compute_terminate_instance"
        )
        self.assertEqual(
            command.call_args_list[0].args,
            ("server", "event", "list", "vm-exact", "-f", "json"),
        )
        self.assertEqual(
            command.call_args_list[1].args,
            (
                "server",
                "event",
                "show",
                "vm-exact",
                "req-delete",
                "-f",
                "json",
            ),
        )


if __name__ == "__main__":
    unittest.main()
