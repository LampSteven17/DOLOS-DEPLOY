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
        call_cost: float = 0.1,
    ):
        self.clock = clock
        self.server_results = list(servers)
        self.volume_results = list(volumes or [{}])
        self.delete_ok = delete_ok
        self.force_ok = force_ok
        self.volume_delete_ok = volume_delete_ok
        self.faults = faults or {}
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
    def _run(self, cloud: _Cloud, clock: _Clock, *, timeout_s: float = 10.0):
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

    def test_initial_error_is_repeatedly_force_deleted_while_it_remains_error(self):
        clock = _Clock()
        cloud = _Cloud(
            clock,
            servers=[
                [
                    _server("vm-error", "d-exact-error", "ERROR", "vol-error"),
                    _server("vm-active", "d-exact-active", "ACTIVE", "vol-active"),
                ],
                [_server("vm-error", "d-exact-error", "ERROR", "vol-error")],
                [],
            ],
            volumes=[
                {"vol-error": "available", "vol-active": "available"},
                {},
            ],
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
            [
                ("server_force_delete_many", ("vm-error",)),
                ("server_force_delete_many", ("vm-error",)),
            ],
        )
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_delete_many"],
            [("server_delete_many", ("vm-active",), False)],
        )
        names = [call[0] for call in cloud.calls]
        self.assertLess(
            names.index("server_force_delete_many"), names.index("server_delete_many")
        )
        self.assertFalse(any(call[0] == "server_fault" for call in cloud.calls))

    def test_error_force_delete_failure_reports_fault_and_preserves_state(self):
        clock = _Clock()
        exact = _server("vm-error", "d-exact-one", "ERROR", "vol-error")
        cloud = _Cloud(
            clock,
            servers=[
                [_server("vm-error", "d-exact-one", "ACTIVE", "vol-error")],
                [exact],
                [exact],
            ],
            volumes=[{"vol-error": "in-use"}],
            force_ok=False,
            faults={"vm-error": "No valid host"},
        )
        with mock.patch.object(teardown, "make_vm_prefix", return_value="d-exact-"):
            result, final_calls, rendered = self._run(cloud, clock)

        self.assertEqual(result, 1)
        self.assertEqual(final_calls, [])
        self.assertIn(
            "d-exact-one id=vm-error status=ERROR fault=No valid host", rendered
        )
        self.assertIn("vol-error status=in-use", rendered)
        self.assertIn("Registry record remains open; SSH state was preserved", rendered)
        self.assertEqual(
            [call for call in cloud.calls if call[0] == "server_force_delete_many"],
            [("server_force_delete_many", ("vm-error",))],
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

    def test_volume_delete_failure_does_not_finalize(self):
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
        self.assertIn("batch volume delete failed", rendered)
        self.assertIn("vol-1 status=available", rendered)

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


if __name__ == "__main__":
    unittest.main()
