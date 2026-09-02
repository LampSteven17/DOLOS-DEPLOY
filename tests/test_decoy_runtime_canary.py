from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest import mock

import yaml

from deployment_engine import __main__ as deployment_cli
from deployment_engine import list as deployment_list
from deployment_engine import teardown as deployment_teardown
from deployment_engine.core import plan, teardown_steps
from deployment_engine.core.feedback import (
    DECOY_FEEDBACK_SUP_CONFIGS,
    DECOY_PLAN_FILENAMES,
    validate_decoy_canary_generation,
)
from deployment_engine.core.run_status import CLEANED, read_run_status
from deployment_engine.decoy import spinup


ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "deployments" / "decoy-runtime-canary"


class DecoyRuntimeCanaryTests(unittest.TestCase):
    def test_four_plans_share_schedule_and_cover_qualification_matrix(self):
        plans = validate_decoy_canary_generation(CANARY / "plans")
        self.assertEqual(tuple(plans), DECOY_FEEDBACK_SUP_CONFIGS)
        sequences = []
        for sup_config, loaded in plans.items():
            self.assertEqual(loaded.resource_profile, "feedback-v2")
            self.assertEqual(loaded.max_parallel, 10)
            entries = [entry for window in loaded.windows for entry in window.sequence]
            self.assertEqual(len(entries), 285)
            self.assertEqual(
                {entry.workflow for entry in entries},
                {"WebResearch", "VideoViewing", "FileDownload", "DocumentCreation", "FileSyncUpload", "NetworkShareAccess"},
            )
            if sup_config in {"scripted-cpu", "mchp-cpu"}:
                self.assertTrue(all(entry.instruction is None for entry in entries))
            else:
                self.assertTrue(all(entry.instruction for entry in entries))
            sequences.append([
                (window.start_minute, window.end_minute,
                 [(entry.offset_minutes, entry.workflow, entry.resource_id) for entry in window.sequence])
                for window in loaded.windows
            ])
        self.assertTrue(all(sequence == sequences[0] for sequence in sequences[1:]))
        same_download = next(window for window in plans["mchp-cpu"].windows if window.start_minute == 1326)
        self.assertEqual(len(same_download.sequence), 2)
        self.assertEqual(same_download.sequence[0].resource_id, same_download.sequence[1].resource_id)
        self.assertTrue(any(
            sum(1 for entry in window.sequence if entry.offset_minutes == 0) == 10
            for window in plans["mchp-cpu"].windows
        ))

    def test_plan_is_one_four_vm_ruse_only_task(self):
        tasks = plan.build_decoy_canary_plan(ROOT / "deployments")
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertTrue(task["is_canary"])
        self.assertFalse(task["is_controls"])
        self.assertEqual(task["config_name"], "decoy-runtime-canary")
        self.assertEqual([item["behavior"] for item in task["deployments"]], list(DECOY_FEEDBACK_SUP_CONFIGS))
        config = yaml.safe_load((CANARY / "config.yaml").read_text())
        self.assertEqual(config["purpose"], "other")
        self.assertIsNone(config["target"])

    def test_cli_is_exclusive_and_never_uses_production_selectors(self):
        for argv in (
            ["--canary", "--controls"], ["--canary", "--feedback"],
            ["--canary", "--target", "axes-summer24"],
            ["--canary", "--source", "/tmp/source"], ["--canary", "--gpu", "rtx"],
            ["--canary", "another-config"], ["--canary", "--ghosts"],
        ):
            with self.subTest(argv=argv), mock.patch.object(plan, "execute_plan") as execute:
                self.assertEqual(deployment_cli._cmd_deploy(argv), 1)
                execute.assert_not_called()
        with mock.patch.object(plan.output, "confirm", return_value=False), mock.patch.object(plan, "execute_plan") as execute:
            self.assertEqual(deployment_cli._cmd_deploy(["--decoy", "--canary"]), 0)
            execute.assert_not_called()

    def test_canary_requires_share_and_is_validated_before_runner(self):
        self.assertEqual(spinup._validate_behavior_source(str(CANARY / "plans"), SimpleNamespace(
            purpose="other", deployments=[{"behavior": value} for value in DECOY_FEEDBACK_SUP_CONFIGS]
        )), [])
        with tempfile.TemporaryDirectory() as temporary:
            bad = Path(temporary)
            for filename in DECOY_PLAN_FILENAMES.values():
                (bad / filename).write_text("{}\n")
            errors = spinup._validate_behavior_source(str(bad), SimpleNamespace(
                purpose="other", deployments=[{"behavior": value} for value in DECOY_FEEDBACK_SUP_CONFIGS]
            ))
        self.assertTrue(errors)

    def test_spinup_snapshots_plans_installs_them_and_skips_phase(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run_playbook(self, playbook, _inventory, *, extra_vars, **_kwargs):
                self.calls.append((playbook, dict(extra_vars)))
                run_dir = Path(extra_vars["run_dir"])
                if playbook == "shared/provision-vms.yaml":
                    (run_dir / "inventory.ini").write_text(
                        "[sup_hosts]\n" + "\n".join(
                            f"vm-{index} ansible_host=10.0.0.{index} sup_behavior={sup_config}"
                            for index, sup_config in enumerate(DECOY_FEEDBACK_SUP_CONFIGS, 1)
                        ) + "\n"
                    )
                return SimpleNamespace(rc=0, log_path=run_dir / (playbook.replace("/", "-") + ".log"))

        runner = Runner()
        started = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            deploy_root = Path(temporary) / "deployments"
            config_dir = deploy_root / "decoy-runtime-canary"
            shutil.copytree(CANARY, config_dir)
            (deploy_root / "hosts.ini").write_text("[openstack_controller]\n")
            with (
                mock.patch.object(spinup, "_active_current_runs", return_value=[]),
                mock.patch.object(spinup, "resolve_ruse_revision", return_value="a" * 40),
                mock.patch.object(spinup, "utc_deployment_start", return_value=started),
                mock.patch.object(spinup, "AnsibleRunner", return_value=runner),
                mock.patch.object(spinup, "_provision_share_sidecar", return_value={
                    "name": "d-runtimecanary2026-09-02_200000Z-share-0",
                    "ip": "10.0.0.10", "flavor": "v1.small", "sup_config": None,
                }),
                mock.patch.object(spinup, "ssh_connectivity_test", return_value=4),
                mock.patch.object(spinup, "register_phase_run") as register,
            ):
                result = spinup.run_decoy_spinup(
                    "decoy-runtime-canary", deploy_root, str(config_dir / "plans")
                )
            self.assertEqual(result, 0)
            register.assert_not_called()
            run_dir = config_dir / "runs" / "2026-09-02_200000Z"
            self.assertTrue((run_dir / "evidence").is_dir())
            self.assertEqual(
                {path.name for path in (run_dir / "plans").iterdir()},
                set(DECOY_PLAN_FILENAMES.values()),
            )
            install = next(call for call in runner.calls if call[0] == "decoy/install-sups.yaml")
            self.assertEqual(install[1]["behavior_source"], str(run_dir / "plans"))
            self.assertIn("decoy/prepare-share.yaml", [call[0] for call in runner.calls])

    def test_filtered_teardown_never_selects_canary(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_root = Path(temporary)
            config_dir = deploy_root / "decoy-runtime-canary"
            run_dir = config_dir / "runs" / "2026-09-02_170000Z"
            run_dir.mkdir(parents=True)
            (config_dir / "config.yaml").write_text((CANARY / "config.yaml").read_text())
            with mock.patch.object(deployment_teardown.OpenStack, "server_status_map", return_value={"d-runtimecanary2026-09-02_170000Z-scripted-cpu-0": "ACTIVE"}), mock.patch.object(deployment_teardown.output, "confirm") as confirm:
                self.assertEqual(deployment_teardown.run_teardown_filtered(
                    deploy_root, {"decoy": True, "rampart": False, "ghosts": False}
                ), 0)
            confirm.assert_not_called()

    def test_ruse_only_finalization_marks_cleaned_without_phase_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with mock.patch.object(teardown_steps, "deployment_path", side_effect=AssertionError("PHASE touched")), mock.patch("deployment_engine.core.ssh_config.remove_ssh_config") as remove:
                self.assertTrue(teardown_steps.finalize_verified_teardown(
                    "decoy-runtime-canary", "2026-09-02_170000Z", run_dir, ruse_only=True,
                ))
            self.assertEqual(read_run_status(run_dir), CLEANED)
            remove.assert_called_once_with("decoy-runtime-canary/2026-09-02_170000Z")

    def test_list_marks_live_canary_without_phase_registry_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_root = Path(temporary)
            config_dir = deploy_root / "decoy-runtime-canary"
            run_dir = config_dir / "runs" / "2026-09-02_170000Z"
            run_dir.mkdir(parents=True)
            (config_dir / "config.yaml").write_text((CANARY / "config.yaml").read_text())
            (run_dir / "inventory.ini").write_text("vm ansible_host=10.0.0.1 sup_behavior=scripted-cpu\n")
            lines = []
            with mock.patch.object(deployment_list.OpenStack, "server_status_map", return_value={"d-runtimecanary2026-09-02_170000Z-scripted-cpu-0": "ACTIVE"}), mock.patch.object(deployment_list, "deployment_path", side_effect=AssertionError("PHASE touched")), mock.patch.object(deployment_list.output, "info", side_effect=lines.append), mock.patch.object(deployment_list.output, "table", side_effect=lambda headers, rows, **kwargs: lines.extend(" ".join(row) for row in rows)):
                self.assertEqual(deployment_list.run_list(deploy_root), 0)
            self.assertIn("canary", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
