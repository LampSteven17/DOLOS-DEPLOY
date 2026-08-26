from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml

from deployment_engine import __main__ as deployment_cli
from deployment_engine import list as deployment_list
from deployment_engine import teardown as deployment_teardown
from deployment_engine.core import teardown_steps
from deployment_engine.core.phase_run_registry import (
    PhaseRunRegistryError,
    create_deployment,
)
from deployment_engine.core.run_status import (
    CLEANED,
    FAILED,
    OK,
    read_run_status,
    write_run_status,
)
from deployment_engine.core.vm_naming import make_run_dep_id, make_vm_prefix
from deployment_engine.decoy import teardown as decoy_teardown


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CURRENT_RUN = "2026-08-20_130523Z"
LEGACY_RUN = "082026130523"
CANONICAL = (
    ("scripted-cpu", "v1.14vcpu.28g"),
    ("mchp-cpu", "v1.14vcpu.28g"),
    ("browseruse-gpu", "v100-1gpu.14vcpu.28g"),
    ("smolagents-gpu", "v100-1gpu.14vcpu.28g"),
)


def _write_config(deploy_dir: Path, name: str = "decoy-controls") -> Path:
    config_dir = deploy_dir / name
    config_dir.mkdir(parents=True)
    config = {
        "deployment_name": name,
        "purpose": "control",
        "target": None,
        "capture_interface": "eno2",
        "deployments": [
            {"behavior": behavior, "flavor": flavor, "count": 1}
            for behavior, flavor in CANONICAL
        ],
    }
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    return config_dir


def _write_purpose_config(
    deploy_dir: Path, name: str, purpose: str, target: str | None
) -> Path:
    config_dir = deploy_dir / name
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "deployment_name": name,
                "purpose": purpose,
                "target": target,
                "capture_interface": "eno2",
                "deployments": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "runs" / CURRENT_RUN).mkdir(parents=True)
    return config_dir


class _Cloud:
    def __init__(self, statuses: dict[str, str]):
        self.statuses = statuses
        self.status_map_calls = 0

    def server_status_map(self):
        self.status_map_calls += 1
        return dict(self.statuses)


class OperatorCommandTests(unittest.TestCase):
    def test_list_omits_legacy_and_valid_zero_vm_runs_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            legacy = config_dir / "runs" / LEGACY_RUN
            current = config_dir / "runs" / CURRENT_RUN
            legacy.mkdir(parents=True)
            current.mkdir()
            (legacy / "inventory.ini").write_text("historical\n")
            (current / "inventory.ini").write_text("current\n")
            before = {
                path.relative_to(deploy_dir): path.read_bytes()
                for path in deploy_dir.rglob("*")
                if path.is_file()
            }

            stderr = StringIO()
            with (
                mock.patch.object(deployment_list, "OpenStack", return_value=_Cloud({})),
                redirect_stderr(stderr),
            ):
                self.assertEqual(deployment_list.run_list(deploy_dir), 0)

            self.assertIn("No active deployments.", stderr.getvalue())
            self.assertNotIn(LEGACY_RUN, stderr.getvalue())
            self.assertNotIn(CURRENT_RUN, stderr.getvalue())
            self.assertTrue(legacy.is_dir())
            self.assertTrue(current.is_dir())
            after = {
                path.relative_to(deploy_dir): path.read_bytes()
                for path in deploy_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_list_requires_an_exact_run_vm_prefix_and_reports_non_active_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            (config_dir / "runs" / CURRENT_RUN).mkdir(parents=True)
            prefix = make_vm_prefix(make_run_dep_id("decoy-controls", CURRENT_RUN))
            partial = prefix.removesuffix("-") + "extra-scripted-cpu-0"

            stderr = StringIO()
            with (
                mock.patch.object(
                    deployment_list,
                    "OpenStack",
                    return_value=_Cloud({partial: "ACTIVE"}),
                ),
                redirect_stderr(stderr),
            ):
                deployment_list.run_list(deploy_dir)
            self.assertIn("No active deployments.", stderr.getvalue())

            stderr = StringIO()
            exact = prefix + "scripted-cpu-0"
            with (
                mock.patch.object(
                    deployment_list,
                    "OpenStack",
                    return_value=_Cloud({partial: "ACTIVE", exact: "BUILD"}),
                ),
                redirect_stderr(stderr),
            ):
                deployment_list.run_list(deploy_dir)
            rendered = stderr.getvalue()
            self.assertIn(f"decoy-controls-{CURRENT_RUN}", rendered)
            self.assertIn("1 BUILD", rendered)

    def test_list_marks_exact_resources_without_phase_record_unregistered(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            run_dir = config_dir / "runs" / CURRENT_RUN
            run_dir.mkdir(parents=True)
            (run_dir / "inventory.ini").write_text(
                "[sup_hosts]\n"
                + "\n".join(
                    f"vm-{index} ansible_host=10.0.0.{index} "
                    f"sup_behavior={behavior}"
                    for index, (behavior, _flavor) in enumerate(CANONICAL, 1)
                )
                + "\n",
                encoding="utf-8",
            )
            prefix = make_vm_prefix(make_run_dep_id(config_dir.name, CURRENT_RUN))
            statuses = {
                **{
                    prefix + f"{behavior}-0": "ACTIVE"
                    for behavior, _flavor in CANONICAL
                },
                prefix + "share-0": "ACTIVE",
            }
            missing_record = Path(temporary) / "missing" / "deployment.json"

            stderr = StringIO()
            with (
                mock.patch.object(
                    deployment_list, "OpenStack", return_value=_Cloud(statuses)
                ),
                mock.patch.object(
                    deployment_list, "deployment_path", return_value=missing_record
                ),
                redirect_stderr(stderr),
            ):
                self.assertEqual(deployment_list.run_list(deploy_dir), 0)

            rendered = stderr.getvalue()
            self.assertIn(f"decoy-controls-{CURRENT_RUN}", rendered)
            self.assertIn("4/4", rendered)
            self.assertIn("unregistered", rendered)
            self.assertIn("share ACTIVE", rendered)
            self.assertNotIn("+share", rendered)

            registered_record = Path(temporary) / "registered" / "deployment.json"
            registered_record.parent.mkdir()
            registered_record.write_text("{}\n", encoding="utf-8")
            stderr = StringIO()
            with (
                mock.patch.object(
                    deployment_list, "OpenStack", return_value=_Cloud(statuses)
                ),
                mock.patch.object(
                    deployment_list,
                    "deployment_path",
                    return_value=registered_record,
                ),
                redirect_stderr(stderr),
            ):
                self.assertEqual(deployment_list.run_list(deploy_dir), 0)
            registered = stderr.getvalue()
            self.assertIn("+share", registered)
            self.assertNotIn("unregistered", registered)

    def test_filtered_teardown_ignores_legacy_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            (config_dir / "runs" / LEGACY_RUN).mkdir(parents=True)
            (config_dir / "runs" / CURRENT_RUN).mkdir()
            prefix = make_vm_prefix(make_run_dep_id("decoy-controls", CURRENT_RUN))
            cloud = _Cloud({prefix + "scripted-cpu-0": "ACTIVE"})

            stderr = StringIO()
            with (
                mock.patch.object(
                    deployment_teardown, "OpenStack", return_value=cloud
                ),
                mock.patch.object(deployment_teardown.output, "confirm", return_value=False),
                redirect_stderr(stderr),
            ):
                result = deployment_teardown.run_teardown_filtered(
                    deploy_dir,
                    types={"decoy": True, "rampart": False, "ghosts": False},
                    purpose=None,
                )

            self.assertEqual(result, 0)
            self.assertIn(CURRENT_RUN, stderr.getvalue())
            self.assertNotIn(LEGACY_RUN, stderr.getvalue())
            self.assertTrue((config_dir / "runs" / LEGACY_RUN).is_dir())
            self.assertEqual(cloud.status_map_calls, 1)

    def test_filtered_teardown_selects_explicit_control_or_feedback_purpose(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            control = _write_purpose_config(
                deploy_dir, "name-that-says-feedback", "control", None
            )
            feedback = _write_purpose_config(
                deploy_dir, "name-that-says-controls", "feedback", "axes-summer24"
            )
            statuses = {}
            for config_dir in (control, feedback):
                prefix = make_vm_prefix(
                    make_run_dep_id(config_dir.name, CURRENT_RUN)
                )
                statuses[prefix + "scripted-cpu-0"] = "ACTIVE"

            for purpose, included, excluded in (
                ("control", control.name, feedback.name),
                ("feedback", feedback.name, control.name),
            ):
                cloud = _Cloud(statuses)
                stderr = StringIO()
                with (
                    mock.patch.object(
                        deployment_teardown, "OpenStack", return_value=cloud
                    ),
                    mock.patch.object(
                        deployment_teardown.output, "confirm", return_value=False
                    ),
                    redirect_stderr(stderr),
                ):
                    result = deployment_teardown.run_teardown_filtered(
                        deploy_dir,
                        types={
                            "decoy": True,
                            "rampart": False,
                            "ghosts": False,
                        },
                        purpose=purpose,
                    )

                self.assertEqual(result, 0)
                rendered = stderr.getvalue()
                self.assertIn(f"{included}/{CURRENT_RUN}", rendered)
                self.assertNotIn(f"{excluded}/{CURRENT_RUN}", rendered)
                self.assertEqual(cloud.status_map_calls, 1)

    def test_list_and_filtered_teardown_share_exact_cloud_presence(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            (config_dir / "runs" / CURRENT_RUN).mkdir(parents=True)
            prefix = make_vm_prefix(make_run_dep_id(config_dir.name, CURRENT_RUN))
            partial = prefix.removesuffix("-") + "extra-scripted-cpu-0"

            for statuses in ({}, {partial: "ACTIVE"}):
                list_cloud = _Cloud(statuses)
                list_output = StringIO()
                with (
                    mock.patch.object(
                        deployment_list, "OpenStack", return_value=list_cloud
                    ),
                    redirect_stderr(list_output),
                ):
                    self.assertEqual(deployment_list.run_list(deploy_dir), 0)
                self.assertIn("No active deployments.", list_output.getvalue())
                self.assertEqual(list_cloud.status_map_calls, 1)

                teardown_cloud = _Cloud(statuses)
                teardown_output = StringIO()
                with (
                    mock.patch.object(
                        deployment_teardown, "OpenStack", return_value=teardown_cloud
                    ),
                    mock.patch.object(
                        deployment_teardown.output, "confirm"
                    ) as confirm,
                    redirect_stderr(teardown_output),
                ):
                    self.assertEqual(
                        deployment_teardown.run_teardown_filtered(
                            deploy_dir,
                            types={
                                "decoy": True,
                                "rampart": False,
                                "ghosts": False,
                            },
                        ),
                        0,
                    )
                self.assertIn("No deployments match", teardown_output.getvalue())
                confirm.assert_not_called()
                self.assertEqual(teardown_cloud.status_map_calls, 1)

            exact_statuses = {
                prefix + "scripted-cpu-0": "ACTIVE",
                prefix + "mchp-cpu-0": "BUILD",
                prefix + "browseruse-gpu-0": "ERROR",
                prefix + "smolagents-gpu-0": "SHUTOFF",
            }
            list_cloud = _Cloud(exact_statuses)
            list_output = StringIO()
            with (
                mock.patch.object(
                    deployment_list, "OpenStack", return_value=list_cloud
                ),
                redirect_stderr(list_output),
            ):
                self.assertEqual(deployment_list.run_list(deploy_dir), 0)
            self.assertIn(f"decoy-controls-{CURRENT_RUN}", list_output.getvalue())
            self.assertEqual(list_cloud.status_map_calls, 1)

            teardown_cloud = _Cloud(exact_statuses)
            teardown_output = StringIO()
            with (
                mock.patch.object(
                    deployment_teardown, "OpenStack", return_value=teardown_cloud
                ),
                mock.patch.object(
                    deployment_teardown.output, "confirm", return_value=False
                ),
                redirect_stderr(teardown_output),
            ):
                self.assertEqual(
                    deployment_teardown.run_teardown_filtered(
                        deploy_dir,
                        types={
                            "decoy": True,
                            "rampart": False,
                            "ghosts": False,
                        },
                    ),
                    0,
                )
            self.assertIn(
                f"decoy-controls/{CURRENT_RUN}", teardown_output.getvalue()
            )
            self.assertEqual(teardown_cloud.status_map_calls, 1)

    def test_failed_filter_keeps_zero_vm_failed_run_as_explicit_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            run_dir = config_dir / "runs" / CURRENT_RUN
            run_dir.mkdir(parents=True)
            write_run_status(run_dir, FAILED, "test failure")
            cloud = _Cloud({})

            stderr = StringIO()
            with (
                mock.patch.object(
                    deployment_teardown, "OpenStack", return_value=cloud
                ),
                mock.patch.object(
                    deployment_teardown.output, "confirm", return_value=False
                ),
                redirect_stderr(stderr),
            ):
                self.assertEqual(
                    deployment_teardown.run_teardown_filtered(
                        deploy_dir,
                        types={
                            "decoy": True,
                            "rampart": False,
                            "ghosts": False,
                        },
                        failed_only=True,
                    ),
                    0,
                )

            self.assertIn(f"decoy-controls/{CURRENT_RUN}", stderr.getvalue())
            self.assertEqual(cloud.status_map_calls, 1)

    def test_cleaned_run_is_not_selected_by_failed_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            run_dir = config_dir / "runs" / CURRENT_RUN
            run_dir.mkdir(parents=True)
            write_run_status(run_dir, CLEANED, "resources cleaned")
            cloud = _Cloud({})

            stderr = StringIO()
            with (
                mock.patch.object(
                    deployment_teardown, "OpenStack", return_value=cloud
                ),
                mock.patch.object(
                    deployment_teardown.output, "confirm"
                ) as confirm,
                redirect_stderr(stderr),
            ):
                result = deployment_teardown.run_teardown_filtered(
                    deploy_dir,
                    types={
                        "decoy": True,
                        "rampart": False,
                        "ghosts": False,
                    },
                    failed_only=True,
                )

            self.assertEqual(result, 0)
            self.assertIn("No failed deployments match", stderr.getvalue())
            confirm.assert_not_called()

    def test_teardown_purpose_cli_dispatch_and_validation(self):
        for flag, expected in (("--controls", "control"), ("--feedback", "feedback")):
            with mock.patch(
                "deployment_engine.teardown.run_teardown_filtered", return_value=0
            ) as filtered:
                self.assertEqual(deployment_cli._cmd_teardown(["--decoy", flag]), 0)
            self.assertEqual(filtered.call_args.kwargs["purpose"], expected)
            self.assertEqual(
                filtered.call_args.kwargs["types"],
                {"decoy": True, "rampart": False, "ghosts": False},
            )

            stderr = StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(deployment_cli._cmd_teardown([flag]), 1)
            self.assertIn("requires a system selector", stderr.getvalue())

            with mock.patch(
                "deployment_engine.teardown.run_teardown_all"
            ) as teardown_all:
                self.assertEqual(deployment_cli._cmd_teardown(["--all", flag]), 1)
            teardown_all.assert_not_called()

        with self.assertRaises(SystemExit):
            deployment_cli._cmd_teardown(
                ["--decoy", "--controls", "--feedback"]
            )

        help_text = deployment_cli._teardown_parser().format_help()
        self.assertIn("--controls", help_text)
        self.assertNotIn("--control ", help_text)

    def test_failed_filter_still_spans_systems_and_composes_with_purpose(self):
        with mock.patch(
            "deployment_engine.teardown.run_teardown_filtered", return_value=0
        ) as filtered:
            self.assertEqual(deployment_cli._cmd_teardown(["--failed"]), 0)
        self.assertEqual(
            filtered.call_args.kwargs["types"],
            {"decoy": True, "rampart": True, "ghosts": True},
        )
        self.assertIsNone(filtered.call_args.kwargs["purpose"])
        self.assertTrue(filtered.call_args.kwargs["failed_only"])

        with mock.patch(
            "deployment_engine.teardown.run_teardown_filtered", return_value=0
        ) as filtered:
            self.assertEqual(
                deployment_cli._cmd_teardown(
                    ["--decoy", "--controls", "--failed"]
                ),
                0,
            )
        self.assertEqual(filtered.call_args.kwargs["purpose"], "control")
        self.assertTrue(filtered.call_args.kwargs["failed_only"])

    def test_explicit_dated_teardown_dispatches_only_the_exact_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_config(deploy_dir)
            (config_dir / "runs" / CURRENT_RUN).mkdir(parents=True)

            with mock.patch(
                "deployment_engine.decoy.teardown.run_decoy_teardown",
                return_value=0,
            ) as dispatch:
                result = deployment_teardown.run_teardown(
                    f"decoy-controls-{CURRENT_RUN}", deploy_dir
                )

            self.assertEqual(result, 0)
            dispatch.assert_called_once()
            self.assertEqual(dispatch.call_args.args[1:3], ("decoy-controls", CURRENT_RUN))

            with mock.patch(
                "deployment_engine.decoy.teardown.run_decoy_teardown"
            ) as legacy_dispatch:
                result = deployment_teardown.run_teardown(
                    f"decoy-controls-{LEGACY_RUN}", deploy_dir
                )
            self.assertEqual(result, 1)
            legacy_dispatch.assert_not_called()

            with mock.patch(
                "deployment_engine.decoy.teardown.run_decoy_teardown"
            ) as unsafe_dispatch:
                result = deployment_teardown.run_teardown(
                    f"../decoy-controls-{CURRENT_RUN}", deploy_dir
                )
            self.assertEqual(result, 1)
            unsafe_dispatch.assert_not_called()

    def test_teardown_failures_preserve_registry_run_and_ssh_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "decoy-controls"
            run_dir = config_dir / "runs" / CURRENT_RUN
            run_dir.mkdir(parents=True)
            sentinel = run_dir / "deployment-sentinel"
            sentinel.write_text("unchanged\n")
            record_path = Path(temporary) / "deployment.json"
            record_path.write_text("{}\n")

            cloud = mock.Mock()
            cloud.count_vms_with_prefix.return_value = 0
            with (
                mock.patch.object(teardown_steps, "OpenStack", return_value=cloud),
                mock.patch.object(
                    teardown_steps, "deployment_path", return_value=record_path
                ),
                mock.patch.object(
                    teardown_steps,
                    "close_deployment",
                    side_effect=PhaseRunRegistryError("cannot close"),
                ),
                mock.patch(
                    "deployment_engine.core.ssh_config.remove_ssh_config"
                ) as remove_ssh,
            ):
                result = teardown_steps.finalize_teardown(
                    "decoy-controls",
                    config_dir,
                    CURRENT_RUN,
                    run_dir,
                    "d-controls-exact-",
                )

            self.assertFalse(result)
            self.assertEqual(sentinel.read_text(), "unchanged\n")
            self.assertTrue(run_dir.is_dir())
            remove_ssh.assert_not_called()

            self.assertFalse(
                (
                    REPOSITORY_ROOT
                    / "deployment_engine"
                    / "playbooks"
                    / "decoy"
                    / "teardown.yaml"
                ).exists()
            )
            shared = (
                REPOSITORY_ROOT
                / "deployment_engine"
                / "playbooks"
                / "shared"
                / "teardown-all.yaml"
            )
            self.assertNotIn("inventory.ini", shared.read_text(encoding="utf-8"))

    def test_vm_delete_failure_does_not_close_or_remove_the_exact_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = deploy_dir / "decoy-controls"
            run_dir = config_dir / "runs" / CURRENT_RUN
            run_dir.mkdir(parents=True)
            sentinel = run_dir / "registry-sentinel"
            sentinel.write_text("open\n")
            cloud = mock.Mock()
            cloud.server_cohort.return_value = [
                {
                    "id": "vm-exact",
                    "name": "d-controls-exact-scripted",
                    "status": "ACTIVE",
                    "volume_ids": ["vol-exact"],
                }
            ]
            cloud.server_delete_many.return_value = False
            cloud.server_attached_volume_ids.return_value = ["vol-exact"]
            cloud.volume_statuses.return_value = {"vol-exact": "in-use"}
            cloud.server_fault.return_value = None

            with (
                mock.patch.object(decoy_teardown, "OpenStack", return_value=cloud),
                mock.patch.object(
                    decoy_teardown, "make_vm_prefix", return_value="d-controls-exact-"
                ),
                mock.patch.object(
                    decoy_teardown, "finalize_verified_teardown"
                ) as finalize,
            ):
                result = decoy_teardown.run_decoy_teardown(
                    config_dir, "decoy-controls", CURRENT_RUN, deploy_dir
                )

            self.assertEqual(result, 1)
            finalize.assert_not_called()
            self.assertTrue(run_dir.is_dir())
            self.assertEqual(sentinel.read_text(), "open\n")
            self.assertEqual(
                cloud.server_delete_many.call_args.args[0], ["vm-exact"]
            )

    def test_successful_teardown_closes_exact_record_but_preserves_run_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "decoy-controls"
            run_dir = config_dir / "runs" / CURRENT_RUN
            run_dir.mkdir(parents=True)
            sentinel = run_dir / "historical-state"
            sentinel.write_text("retained\n")
            axes_root = Path(temporary) / "axes"
            started_at = datetime.strptime(
                CURRENT_RUN, "%Y-%m-%d_%H%M%SZ"
            ).replace(tzinfo=timezone.utc)
            created_run, record_path = create_deployment(
                experiment_id="decoy-controls",
                system="decoy",
                purpose="control",
                target=None,
                started_at=started_at,
                capture_interface="eno2",
                vms=[
                    {
                        "name": "d-controls-exact-scripted-cpu-0",
                        "ip": "192.0.2.10",
                        "sup_config": "scripted-cpu",
                    }
                ],
                experiments_root=axes_root / "experiments",
            )
            self.assertEqual(created_run, CURRENT_RUN)
            cloud = mock.Mock()
            cloud.count_vms_with_prefix.return_value = 0

            with (
                mock.patch.dict(
                    os.environ, {"PHASE_AXES_ROOT": str(axes_root)}
                ),
                mock.patch.object(teardown_steps, "OpenStack", return_value=cloud),
                mock.patch(
                    "deployment_engine.core.ssh_config.remove_ssh_config"
                ) as remove_ssh,
            ):
                result = teardown_steps.finalize_teardown(
                    "decoy-controls",
                    config_dir,
                    CURRENT_RUN,
                    run_dir,
                    "d-controls-exact-",
                )

            self.assertTrue(result)
            remove_ssh.assert_called_once_with(f"decoy-controls/{CURRENT_RUN}")
            self.assertIsNotNone(json.loads(record_path.read_text())["ended_at"])
            self.assertTrue(run_dir.is_dir())
            self.assertEqual(sentinel.read_text(), "retained\n")

    def test_failed_pre_registration_teardown_cleans_without_registry_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "decoy-feedback"
            run_dir = config_dir / "runs" / CURRENT_RUN
            run_dir.mkdir(parents=True)
            sentinel = run_dir / "historical-state"
            sentinel.write_text("retained\n")
            write_run_status(run_dir, FAILED, "install failed")
            missing_record = Path(temporary) / "missing" / "deployment.json"
            cloud = mock.Mock()
            cloud.count_vms_with_prefix.return_value = 0
            stderr = StringIO()

            with (
                mock.patch.object(teardown_steps, "OpenStack", return_value=cloud),
                mock.patch.object(
                    teardown_steps, "deployment_path", return_value=missing_record
                ),
                mock.patch.object(teardown_steps, "close_deployment") as close,
                mock.patch(
                    "deployment_engine.core.ssh_config.remove_ssh_config"
                ) as remove_ssh,
                redirect_stderr(stderr),
            ):
                result = teardown_steps.finalize_teardown(
                    "decoy-feedback",
                    config_dir,
                    CURRENT_RUN,
                    run_dir,
                    "d-feedback-exact-",
                )

            self.assertTrue(result)
            close.assert_not_called()
            remove_ssh.assert_called_once_with(f"decoy-feedback/{CURRENT_RUN}")
            self.assertIn("nothing to close", stderr.getvalue())
            self.assertEqual(read_run_status(run_dir), CLEANED)
            self.assertTrue(run_dir.is_dir())
            self.assertEqual(sentinel.read_text(), "retained\n")

    def test_missing_registry_record_for_nonfailed_run_preserves_state(self):
        for local_status in (OK, None):
            with self.subTest(local_status=local_status):
                with tempfile.TemporaryDirectory() as temporary:
                    config_dir = Path(temporary) / "decoy-controls"
                    run_dir = config_dir / "runs" / CURRENT_RUN
                    run_dir.mkdir(parents=True)
                    if local_status is not None:
                        write_run_status(run_dir, local_status, "deploy complete")
                    status_before = (
                        run_dir / "deploy_status.json"
                    ).read_bytes() if local_status is not None else None
                    cloud = mock.Mock()
                    cloud.count_vms_with_prefix.return_value = 0

                    with (
                        mock.patch.object(
                            teardown_steps, "OpenStack", return_value=cloud
                        ),
                        mock.patch.object(
                            teardown_steps,
                            "deployment_path",
                            return_value=Path(temporary) / "missing.json",
                        ),
                        mock.patch.object(
                            teardown_steps, "close_deployment"
                        ) as close,
                        mock.patch(
                            "deployment_engine.core.ssh_config.remove_ssh_config"
                        ) as remove_ssh,
                    ):
                        result = teardown_steps.finalize_teardown(
                            "decoy-controls",
                            config_dir,
                            CURRENT_RUN,
                            run_dir,
                            "d-controls-exact-",
                        )

                    self.assertFalse(result)
                    close.assert_not_called()
                    remove_ssh.assert_not_called()
                    self.assertTrue(run_dir.is_dir())
                    status_path = run_dir / "deploy_status.json"
                    if status_before is None:
                        self.assertFalse(status_path.exists())
                    else:
                        self.assertEqual(status_path.read_bytes(), status_before)

    def test_deploy_command_builds_only_four_controls_without_provisioning(self):
        captured: dict = {}
        selected = Path("/data/axes-mirror/controls/2026-08-24_1456Z")

        def refuse_execution(plan, deploy_type, config_name, deploy_dir, **kwargs):
            captured.update(plan=plan, deploy_type=deploy_type, deploy_dir=deploy_dir)
            return 0

        with (
            mock.patch.object(deployment_cli, "DEPLOY_DIR", REPOSITORY_ROOT / "deployments"),
            mock.patch(
                "deployment_engine.core.plan.execute_plan",
                side_effect=refuse_execution,
            ) as execute,
            mock.patch(
                "deployment_engine.core.plan.find_decoy_control_generation",
                return_value=selected,
            ),
        ):
            result = deployment_cli._cmd_deploy(["--decoy", "--controls"])

        self.assertEqual(result, 0)
        execute.assert_called_once()
        self.assertEqual(captured["deploy_type"], "decoy")
        self.assertEqual(len(captured["plan"]), 1)
        task = captured["plan"][0]
        self.assertTrue(task["is_controls"])
        self.assertEqual(
            task["behavior_source"], selected
        )
        self.assertIsNone(task["configs_spec"])
        self.assertEqual(
            [(item["behavior"], item["flavor"], item["count"]) for item in task["deployments"]],
            [(behavior, flavor, 1) for behavior, flavor in CANONICAL],
        )

    def test_root_wrappers_are_executable_and_dispatch_from_any_directory(self):
        for command in ("deploy", "teardown", "list"):
            wrapper = REPOSITORY_ROOT / command
            self.assertTrue(wrapper.is_file())
            self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)

        for command in ("deploy", "teardown"):
            result = subprocess.run(
                [str(REPOSITORY_ROOT / command), "--help"],
                cwd="/",
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"usage: {command}", result.stdout.lower())

        with tempfile.TemporaryDirectory() as temporary:
            fake_home = Path(temporary)
            (fake_home / "vxn3kr-bot-rc").write_text("# test credentials\n")
            fake_bin = fake_home / "bin"
            fake_bin.mkdir()
            openstack = fake_bin / "openstack"
            openstack.write_text("#!/bin/sh\nexit 0\n")
            openstack.chmod(0o755)
            env = dict(os.environ)
            env["HOME"] = str(fake_home)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(
                [str(REPOSITORY_ROOT / "list")],
                cwd="/",
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No active deployments.", result.stderr)

    def test_shrink_is_absent(self):
        self.assertFalse((REPOSITORY_ROOT / "shrink").exists())
        self.assertFalse((REPOSITORY_ROOT / "deployment_engine" / "shrink.py").exists())
        self.assertNotIn("shrink", deployment_cli._teardown_parser().format_help())


if __name__ == "__main__":
    unittest.main()
