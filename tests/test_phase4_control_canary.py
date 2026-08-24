from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from deployment_engine import __main__ as deployment_cli
from deployment_engine.core import feedback, plan
from deployment_engine.core.config import DeploymentConfig
from deployment_engine.decoy import spinup


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_CONFIG = REPOSITORY_ROOT / "deployments" / "decoy-controls" / "config.yaml"
CONTROL_ROOT = Path("/home/ubuntu/PHASE/plans/feedback-v2-rewrite/fixtures/controls")
CANONICAL = (
    "scripted-cpu",
    "mchp-cpu",
    "browseruse-gpu",
    "smolagents-gpu",
)


class Phase4ControlCanaryTests(unittest.TestCase):
    def _generation(self, root: Path, timestamp: str) -> Path:
        generation = root / timestamp
        generation.mkdir(parents=True)
        for sup_config in CANONICAL:
            shutil.copy2(
                CONTROL_ROOT / feedback.DECOY_PLAN_FILENAMES[sup_config],
                generation / feedback.DECOY_PLAN_FILENAMES[sup_config],
            )
        return generation

    def _deploy_root(self, root: Path) -> Path:
        deploy_root = root / "deployments"
        destination = deploy_root / "decoy-controls"
        destination.mkdir(parents=True)
        shutil.copy2(CONTROL_CONFIG, destination / "config.yaml")
        return deploy_root

    def test_decoy_controls_config_is_exactly_the_four_canonical_vms(self):
        document = yaml.safe_load(CONTROL_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            document,
            {
                "deployment_name": "decoy-controls",
                "purpose": "control",
                "target": None,
                "capture_interface": "eno2",
                "flavor_capacity": {
                    "v1.14vcpu.28g": 2,
                    "v100-1gpu.14vcpu.28g": 2,
                },
                "deployments": [
                    {
                        "behavior": "scripted-cpu",
                        "flavor": "v1.14vcpu.28g",
                        "count": 1,
                    },
                    {
                        "behavior": "mchp-cpu",
                        "flavor": "v1.14vcpu.28g",
                        "count": 1,
                    },
                    {
                        "behavior": "browseruse-gpu",
                        "flavor": "v100-1gpu.14vcpu.28g",
                        "count": 1,
                    },
                    {
                        "behavior": "smolagents-gpu",
                        "flavor": "v100-1gpu.14vcpu.28g",
                        "count": 1,
                    },
                ],
            },
        )
        self.assertNotIn("behavior_source", document)
        serialized = CONTROL_CONFIG.read_text(encoding="utf-8")
        for legacy in ("C0", "M0", "M1", "B0.gemma", "S0.gemma"):
            self.assertNotIn(legacy, serialized)

    def test_canonical_sources_are_required_before_provisioning(self):
        config = DeploymentConfig.load(CONTROL_CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            generation = self._generation(
                Path(temporary), "2026-08-24_1456Z"
            )
            self.assertEqual(
                spinup._validate_behavior_source(str(generation), config), []
            )
        self.assertEqual(
            spinup._validate_behavior_source(None, config),
            ["canonical control config has no behavior_source"],
        )

    def test_newest_calendar_valid_generation_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = self._generation(root, "2026-08-24_0900Z")
            newer = self._generation(root, "2026-08-24_1456Z")
            self._generation(root, "2026-99-99_9999Z")
            (root / "latest").mkdir()
            with mock.patch.object(feedback, "DECOY_CONTROL_BASE", root):
                selected = feedback.find_decoy_control_generation()
        self.assertEqual(selected, newer)
        self.assertNotEqual(selected, older)

    def test_invalid_newest_control_generation_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = self._generation(root, "2026-08-24_0900Z")
            newest = self._generation(root, "2026-08-24_1456Z")
            (newest / "mchp-v1.json").unlink()
            with (
                mock.patch.object(feedback, "DECOY_CONTROL_BASE", root),
                self.assertRaisesRegex(
                    feedback.FeedbackSourceError, "mchp-v1.json"
                ),
            ):
                feedback.find_decoy_control_generation()
            self.assertTrue(older.is_dir())

    def test_invalid_control_plans_abort_before_plan_display_or_execution(self):
        def missing(path):
            (path / "scripted-v1.json").unlink()

        def extra(path):
            (path / "extra.json").write_text("{}\n")

        def malformed(path):
            (path / "mchp-v1.json").write_text("{\n")

        def schema_invalid(path):
            plan_path = path / "browseruse-v1.json"
            document = json.loads(plan_path.read_text())
            document.pop("timezone")
            plan_path.write_text(json.dumps(document))

        def capability_invalid(path):
            plan_path = path / "smolagents-v1.json"
            document = json.loads(plan_path.read_text())
            document["max_parallel"] = 11
            plan_path.write_text(json.dumps(document))

        def sup_mismatch(path):
            plan_path = path / "scripted-v1.json"
            document = json.loads(plan_path.read_text())
            document["sup_config"] = "mchp-cpu"
            plan_path.write_text(json.dumps(document))

        for name, mutation in (
            ("missing", missing),
            ("extra", extra),
            ("malformed", malformed),
            ("schema", schema_invalid),
            ("capability", capability_invalid),
            ("SUP mismatch", sup_mismatch),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                control_root = root / "controls"
                generation = self._generation(control_root, "2026-08-24_1456Z")
                mutation(generation)
                deploy_root = self._deploy_root(root)
                with (
                    mock.patch.object(
                        feedback, "DECOY_CONTROL_BASE", control_root
                    ),
                    mock.patch.object(
                        deployment_cli, "DEPLOY_DIR", deploy_root
                    ),
                    mock.patch.object(plan, "show_plan_and_confirm") as show,
                    mock.patch.object(plan, "execute_plan") as execute,
                ):
                    result = deployment_cli._cmd_deploy([
                        "--decoy", "--controls"
                    ])
                self.assertEqual(result, 1)
                show.assert_not_called()
                execute.assert_not_called()

    def test_canonical_controls_skip_legacy_distribution(self):
        canonical = DeploymentConfig.load(CONTROL_CONFIG)
        self.assertFalse(spinup._requires_legacy_behavior_distribution(canonical))
        self.assertIsNone(
            spinup._legacy_distribution_source("/should/not/be/copied", canonical)
        )

        legacy = DeploymentConfig(
            deployment_name="legacy",
            purpose="control",
            target=None,
            deployments=[{"behavior": "M1"}],
            behavior_source="/legacy/controls",
        )
        self.assertTrue(spinup._requires_legacy_behavior_distribution(legacy))
        self.assertEqual(
            spinup._legacy_distribution_source("/legacy/controls", legacy),
            "/legacy/controls",
        )

        distribution = (
            REPOSITORY_ROOT
            / "deployment_engine"
            / "playbooks"
            / "decoy"
            / "distribute-behavior-configs.yaml"
        ).read_text(encoding="utf-8")
        for task_name in (
            "Derive behavior directory",
            "Resolve source directory",
            "Copy configs to SUP",
            "Verify behavior.json landed on VM",
            "Start SUP service (post behavior.json land)",
        ):
            self.assertIn(f"- name: {task_name}", distribution)

    def test_installer_owns_plan_copy_and_starts_canonical_services(self):
        installer = (REPOSITORY_ROOT / "INSTALL_SUP.sh").read_text(encoding="utf-8")
        self.assertNotIn("phase-workflow-plan-v1/controls", installer)
        self.assertIn(
            'local workflow_behavior_path="${RUSE_WORKFLOW_BEHAVIOR_PATH:-}"',
            installer,
        )
        self.assertIn(
            '"$dest_dir/behavioral_configurations/behavior.json"',
            installer,
        )

        playbook_path = (
            REPOSITORY_ROOT
            / "deployment_engine"
            / "playbooks"
            / "decoy"
            / "install-sups.yaml"
        )
        play = yaml.safe_load(playbook_path.read_text(encoding="utf-8"))[0]
        tasks = {task["name"]: task for task in play["tasks"]}
        self.assertEqual(tuple(play["vars"]["canonical_workflow_configs"]), CANONICAL)
        start = tasks["Start canonical workflow service after Stage 2"]
        wait = tasks["Wait for canonical workflow service to become active"]
        assertion = tasks["Assert canonical workflow service is active"]

        self.assertEqual(start["when"], "sup_behavior in canonical_workflow_configs")
        self.assertEqual(wait["when"], "sup_behavior in canonical_workflow_configs")
        self.assertEqual(start["systemd"]["state"], "started")
        self.assertTrue(start["systemd"]["daemon_reload"])
        self.assertEqual(wait["retries"], 12)
        self.assertIn("active", wait["until"])
        self.assertEqual(assertion["fail"]["msg"].count("behavior.json"), 0)

        stage = tasks["Stage assigned canonical workflow plan"]
        self.assertEqual(
            stage["copy"]["src"],
            "{{ behavior_source }}/{{ canonical_plan_filenames[sup_behavior] }}",
        )
        self.assertEqual(
            stage["when"], "sup_behavior in canonical_workflow_configs"
        )

    def test_four_control_plans_validate_and_share_one_schedule(self):
        from phase_workflow.loader import load_workflow_plan

        normalized = []
        for sup_config in CANONICAL:
            path = CONTROL_ROOT / feedback.DECOY_PLAN_FILENAMES[sup_config]
            load_workflow_plan(path, sup_config)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["sup_config"], sup_config)
            self.assertEqual(document["timezone"], "America/New_York")
            self.assertEqual(document["resource_profile"], "controls-v2")

            comparable = copy.deepcopy(document)
            comparable.pop("sup_config")
            for window in comparable["schedule"]:
                for occurrence in window["sequence"]:
                    occurrence.pop("brain")
            normalized.append(comparable)

        for document in normalized[1:]:
            self.assertEqual(document, normalized[0])

    def test_controls_render_fixed_topology_and_pass_selected_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control_root = root / "controls"
            generation = self._generation(control_root, "2026-08-24_1456Z")
            deploy_root = self._deploy_root(root)
            lines = []
            with (
                mock.patch.object(feedback, "DECOY_CONTROL_BASE", control_root),
                mock.patch.object(deployment_cli, "DEPLOY_DIR", deploy_root),
                mock.patch.object(plan.output, "info", side_effect=lines.append),
                mock.patch.object(plan.output, "banner", side_effect=lines.append),
                mock.patch.object(plan, "execute_plan", return_value=0) as execute,
            ):
                result = deployment_cli._cmd_deploy(["--decoy", "--controls"])
        self.assertEqual(result, 0)
        tasks = execute.call_args.args[0]
        self.assertEqual(tasks[0]["behavior_source"], generation)
        rendered = "\n".join(lines)
        for expected in (
            "scripted-cpu",
            "Scripted",
            "mchp-cpu",
            "MCHP",
            "browseruse-gpu",
            "BrowserUse",
            "smolagents-gpu",
            "SmolAgents",
            "v1.14vcpu.28g",
            "v100-1gpu.14vcpu.28g",
            "gemma4:26b",
        ):
            self.assertIn(expected, rendered)

        with mock.patch.object(spinup, "run_decoy_spinup", return_value=0) as deploy:
            result = plan.execute_plan(
                tasks, "decoy", None, deploy_root, gpu_tier="v100"
            )
        self.assertEqual(result, 0)
        self.assertEqual(deploy.call_args.args[0], "decoy-controls")
        self.assertEqual(deploy.call_args.args[2], str(generation))
        self.assertIsNone(deploy.call_args.args[3])

    def test_repository_contains_no_control_plan_copies(self):
        duplicate_root = (
            REPOSITORY_ROOT / "contracts" / "phase-workflow-plan-v1" /
            "controls"
        )
        self.assertFalse(any(duplicate_root.glob("**/behavior.json")))


if __name__ == "__main__":
    unittest.main()
