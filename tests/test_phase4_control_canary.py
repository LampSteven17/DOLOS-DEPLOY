from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from deployment_engine.core.config import DeploymentConfig
from deployment_engine.decoy import spinup


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_CONFIG = REPOSITORY_ROOT / "deployments" / "decoy-controls" / "config.yaml"
CONTROL_ROOT = (
    REPOSITORY_ROOT
    / "contracts"
    / "phase-workflow-plan-v1"
    / "controls"
)
CANONICAL = (
    "scripted-cpu",
    "mchp-cpu",
    "browseruse-gpu",
    "smolagents-gpu",
)


class Phase4ControlCanaryTests(unittest.TestCase):
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
        self.assertEqual(spinup._validate_behavior_source(None, config), [])

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                spinup, "WORKFLOW_CONTROL_ROOT", Path(temporary)
            ):
                errors = spinup._validate_behavior_source(None, config)
        self.assertEqual(len(errors), 4)
        for sup_config in CANONICAL:
            self.assertTrue(
                any(sup_config in error and "source-controlled plan" in error for error in errors)
            )

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
        self.assertIn(
            'contracts/phase-workflow-plan-v1/controls/$CONFIG_KEY/behavior.json',
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
        start = tasks["Start canonical workflow service after Stage 2"]
        wait = tasks["Wait for canonical workflow service to become active"]
        assertion = tasks["Assert canonical workflow service is active"]

        for sup_config in CANONICAL:
            self.assertIn(sup_config, start["when"])
            self.assertIn(sup_config, wait["when"])
        self.assertEqual(start["systemd"]["state"], "started")
        self.assertTrue(start["systemd"]["daemon_reload"])
        self.assertEqual(wait["retries"], 12)
        self.assertIn("active", wait["until"])
        self.assertEqual(assertion["fail"]["msg"].count("behavior.json"), 0)

    def test_four_control_plans_validate_and_share_one_schedule(self):
        from phase_workflow.loader import load_workflow_plan

        normalized = []
        for sup_config in CANONICAL:
            path = CONTROL_ROOT / sup_config / "behavior.json"
            load_workflow_plan(path, sup_config)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["sup_config"], sup_config)
            self.assertEqual(document["timezone"], "America/New_York")
            self.assertEqual(document["target_profile"], "control-default")

            comparable = copy.deepcopy(document)
            comparable.pop("sup_config")
            for window in comparable["schedule"]:
                for occurrence in window["sequence"]:
                    occurrence.pop("brain")
            normalized.append(comparable)

        for document in normalized[1:]:
            self.assertEqual(document, normalized[0])


if __name__ == "__main__":
    unittest.main()
