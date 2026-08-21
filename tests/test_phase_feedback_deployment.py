from __future__ import annotations

import inspect
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from deployment_engine import __main__ as deployment_cli
from deployment_engine.core import deploy_steps, feedback, plan
from deployment_engine.core.config import DeploymentConfig
from deployment_engine.decoy import spinup


ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = ROOT / "contracts" / "phase-workflow-plan-v1" / "controls"
CANONICAL = feedback.DECOY_FEEDBACK_SUP_CONFIGS
TARGETS = feedback.DECOY_FEEDBACK_TARGETS


class CanonicalFeedbackDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.feedback_root = self.base / "feedback"
        self.deploy_root = self.base / "deployments"
        self.preset = "colfix_v12.5.0"

    def tearDown(self):
        self.temporary.cleanup()

    def generation(
        self,
        target: str,
        timestamp: str = "2026-08-21_1832Z",
        *,
        missing: str | None = None,
        extra: bool = False,
    ) -> Path:
        generation = self.feedback_root / self.preset / target / timestamp
        generation.mkdir(parents=True, exist_ok=True)
        for sup_config in CANONICAL:
            if sup_config == missing:
                continue
            shutil.copy2(
                CONTROL_ROOT / sup_config / "behavior.json",
                generation / f"{sup_config}_behavior.json",
            )
        if extra:
            (generation / "legacy_behavior.json").write_text("{}\n")
        return generation

    def all_targets(self) -> None:
        for target in TARGETS:
            self.generation(target)

    def patch_root(self):
        return mock.patch.object(
            feedback, "DECOY_FEEDBACK_BASE", self.feedback_root
        )

    def test_all_eight_targets_are_discovered_in_canonical_order(self):
        self.all_targets()
        with self.patch_root():
            sources = feedback.find_all_decoy_feedback_sources(self.preset)
        self.assertEqual([item["target"] for item in sources], list(TARGETS))
        self.assertEqual(len(sources), 8)

    def test_exact_target_and_explicit_generation_select_one_fleet(self):
        source = self.generation("axes-summer24")
        with self.patch_root():
            targeted = plan.build_deploy_plan(
                "decoy", want_controls=False, want_feedback=True,
                configs_spec="all", single_selector="axes-summer24",
                target="axes-summer24", source=None, preset=self.preset,
                deploy_dir=self.deploy_root,
            )
        explicit = plan.build_deploy_plan(
            "decoy", want_controls=False, want_feedback=True,
            configs_spec="all", single_selector=str(source),
            target=None, source=str(source), preset=None,
            deploy_dir=self.deploy_root,
        )
        self.assertEqual([task["target"] for task in targeted], ["axes-summer24"])
        self.assertEqual([task["behavior_source"] for task in explicit], [source])

    def test_newest_timestamp_selection_is_lexical_not_mtime(self):
        older = self.generation("axes-summer24", "2026-08-21_0900Z")
        newer = self.generation("axes-summer24", "2026-08-21_1832Z")
        (newer.parent / "2026-99-99_9999Z").mkdir()
        # Reverse mtimes; timestamp names remain authoritative.
        os.utime(older, (2_000_000_000, 2_000_000_000))
        os.utime(newer, (1_000_000_000, 1_000_000_000))
        with self.patch_root():
            selected = feedback.find_decoy_feedback_by_target(
                "axes-summer24", self.preset
            )
        self.assertEqual(selected, newer)

    def test_invalid_newest_generation_fails_without_older_fallback(self):
        self.generation("axes-summer24", "2026-08-21_0900Z")
        newest = self.generation(
            "axes-summer24", "2026-08-21_1832Z", missing="mchp-cpu"
        )
        with self.patch_root(), self.assertRaisesRegex(
            feedback.FeedbackSourceError, "mchp-cpu_behavior.json"
        ):
            feedback.find_decoy_feedback_by_target("axes-summer24", self.preset)
        self.assertTrue(newest.is_dir())

    def test_generation_requires_exact_four_behavior_files_and_loader_validity(self):
        missing = self.generation("axes-summer24", missing="scripted-cpu")
        with self.assertRaisesRegex(feedback.FeedbackSourceError, "missing"):
            feedback.validate_decoy_feedback_generation(missing)

        shutil.rmtree(missing)
        extra = self.generation("axes-summer24", extra=True)
        with self.assertRaisesRegex(feedback.FeedbackSourceError, "unexpected"):
            feedback.validate_decoy_feedback_generation(extra)

        (extra / "legacy_behavior.json").unlink()
        (extra / "browseruse-gpu_behavior.json").write_text("{}\n")
        with self.assertRaisesRegex(
            feedback.FeedbackSourceError, "invalid browseruse-gpu plan"
        ):
            feedback.validate_decoy_feedback_generation(extra)

    def test_loader_failure_aborts_before_provisioning_runner_exists(self):
        source = self.generation("axes-summer24")
        name = feedback.generate_feedback_config(source, "all", self.deploy_root)
        (self.deploy_root / "hosts.ini").write_text("[openstack_controller]\n")
        with (
            mock.patch.object(spinup, "_active_current_runs", return_value=[]),
            mock.patch.object(spinup, "resolve_ruse_revision", return_value="a" * 40),
            mock.patch.object(
                spinup,
                "validate_decoy_feedback_generation",
                side_effect=feedback.FeedbackSourceError("loader rejected plan"),
            ),
            mock.patch.object(spinup, "AnsibleRunner") as runner,
        ):
            result = spinup.run_decoy_spinup(name, self.deploy_root)
        self.assertEqual(result, 1)
        runner.assert_not_called()

    def test_generated_config_has_fixed_topology_identity_and_no_legacy_fields(self):
        source = self.generation("axes-summer24")
        name = feedback.generate_feedback_config(source, "all", self.deploy_root)
        document = yaml.safe_load(
            (self.deploy_root / name / "config.yaml").read_text()
        )
        self.assertEqual(
            name,
            "decoy-feedback-colfix-v12-5-0-axes-summer24",
        )
        self.assertEqual(document["purpose"], "feedback")
        self.assertEqual(document["target"], "axes-summer24")
        self.assertEqual(document["capture_interface"], "eno2")
        self.assertEqual(document["behavior_source"], str(source))
        self.assertEqual(document["deployments"], list(feedback.DECOY_FEEDBACK_DEPLOYMENTS))
        self.assertEqual(
            document["flavor_capacity"],
            {"v1.14vcpu.28g": 2, "v100-1gpu.14vcpu.28g": 2},
        )
        self.assertNotIn("gpu_tier", document)
        self.assertNotIn("behavior_configs", document)
        with self.assertRaisesRegex(feedback.FeedbackSourceError, "requires V100"):
            feedback.generate_feedback_config(
                source, "all", self.deploy_root, gpu_tier="rtx"
            )

    def test_installer_selects_one_feedback_plan_before_service_start(self):
        play = yaml.safe_load(
            (ROOT / "deployment_engine/playbooks/decoy/install-sups.yaml").read_text()
        )[0]
        names = [task["name"] for task in play["tasks"]]
        stage = play["tasks"][names.index("Stage assigned canonical feedback plan")]
        self.assertEqual(
            stage["copy"]["src"],
            "{{ behavior_source }}/{{ sup_behavior }}_behavior.json",
        )
        self.assertLess(
            names.index("Stage assigned canonical feedback plan"),
            names.index("Stage 2: ollama + python + services"),
        )
        self.assertLess(
            names.index("Remove staged canonical feedback plan"),
            names.index("Start canonical workflow service after Stage 2"),
        )
        installer = (ROOT / "INSTALL_SUP.sh").read_text()
        self.assertIn("RUSE_WORKFLOW_BEHAVIOR_PATH", installer)
        self.assertIn(
            "contracts/phase-workflow-plan-v1/controls/$CONFIG_KEY/behavior.json",
            installer,
        )
        self.assertEqual(
            installer.count(
                '"$dest_dir/behavioral_configurations/behavior.json"'
            ),
            1,
        )

    def test_exact_target_and_purpose_reach_registry_writer(self):
        source = self.generation("cptc11-zeektx")
        name = feedback.generate_feedback_config(source, "all", self.deploy_root)
        config = DeploymentConfig.load(self.deploy_root / name / "config.yaml")
        with mock.patch.object(
            deploy_steps, "create_deployment", return_value=("run", Path("deployment.json"))
        ) as create:
            self.assertTrue(
                deploy_steps.register_phase_run(
                    config,
                    "decoy",
                    mock.sentinel.started_at,
                    [{"name": "vm", "ip": "10.0.0.1", "sup_config": "scripted-cpu"}],
                )
            )
        self.assertEqual(create.call_args.kwargs["purpose"], "feedback")
        self.assertEqual(create.call_args.kwargs["target"], "cptc11-zeektx")
        self.assertEqual(create.call_args.kwargs["experiment_id"], name)

    def test_eight_generated_configs_do_not_collide(self):
        self.all_targets()
        with self.patch_root():
            sources = feedback.find_all_decoy_feedback_sources(self.preset)
        names = {
            feedback.generate_feedback_config(item["path"], "all", self.deploy_root)
            for item in sources
        }
        self.assertEqual(len(names), 8)
        self.assertEqual(len(list(self.deploy_root.glob("*/config.yaml"))), 8)

    def test_canonical_path_never_reads_manifest_or_legacy_decoy_root(self):
        self.all_targets()
        with self.patch_root(), mock.patch.object(
            feedback, "load_manifest", side_effect=AssertionError("manifest read")
        ):
            sources = feedback.find_all_decoy_feedback_sources(self.preset)
            feedback.generate_feedback_config(
                sources[0]["path"], "all", self.deploy_root
            )
        canonical_source = "\n".join(
            inspect.getsource(function)
            for function in (
                feedback.find_decoy_feedback_by_target,
                feedback.find_all_decoy_feedback_sources,
                feedback.generate_feedback_config,
            )
        )
        self.assertNotIn("manifest", canonical_source)
        self.assertNotIn("/mnt/AXES2U1", canonical_source)

    def test_rampart_and_ghosts_discovery_roots_are_unchanged(self):
        old_root = self.base / "legacy-feedback"
        for system, marker in (
            ("rampart", "node/user-roles.json"),
            ("ghosts", "npc-0/timeline.json"),
        ):
            marker_path = old_root / f"{system}-controls" / self.preset / "dataset" / marker
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text("{}\n")
        with mock.patch.object(feedback, "FEEDBACK_BASE", old_root):
            self.assertEqual(
                len(feedback.find_all_feedback_sources("rampart", self.preset)), 1
            )
            self.assertEqual(
                len(feedback.find_all_feedback_sources("ghosts", self.preset)), 1
            )

    def test_cli_renders_eight_target_plan_without_execution(self):
        self.all_targets()
        lines = []
        with (
            self.patch_root(),
            mock.patch.object(deployment_cli, "DEPLOY_DIR", self.deploy_root),
            mock.patch.object(plan.output, "info", side_effect=lines.append),
            mock.patch.object(plan.output, "banner", side_effect=lines.append),
            mock.patch.object(plan.output, "confirm", return_value=False),
            mock.patch.object(plan, "execute_plan") as execute,
        ):
            result = deployment_cli._cmd_deploy(
                ["--decoy", "--feedback", "--preset", self.preset]
            )
        self.assertEqual(result, 0)
        execute.assert_not_called()
        rendered = "\n".join(lines)
        self.assertIn("DEPLOY PLAN (decoy, 8 tasks)", rendered)
        self.assertEqual(rendered.count("purpose:     feedback"), 8)
        self.assertEqual(rendered.count("VMs to provision (4)"), 8)
        for target in TARGETS:
            self.assertIn(f"target:      {target}", rendered)
        for sup_config in CANONICAL:
            self.assertEqual(rendered.count(sup_config), 8)


if __name__ == "__main__":
    unittest.main()
