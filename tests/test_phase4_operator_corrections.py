from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml

from deployment_engine.core import feedback
from deployment_engine.core.plan import build_deploy_plan
from deployment_engine.core.vm_naming import make_run_dep_id, make_vm_prefix
from deployment_engine.decoy import spinup


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES_PATH = (
    REPOSITORY_ROOT
    / "contracts"
    / "phase-workflow-plan-v1"
    / "capabilities-v1.json"
)
CURRENT_RUN = "2026-08-20_175127Z"
LEGACY_RUN = "061326144439"
CANONICAL = (
    ("scripted-cpu", "v1.14vcpu.28g"),
    ("mchp-cpu", "v1.14vcpu.28g"),
    ("browseruse-gpu", "v100-1gpu.14vcpu.28g"),
    ("smolagents-gpu", "v100-1gpu.14vcpu.28g"),
)


class _Cloud:
    def __init__(self, statuses: dict[str, str]):
        self.statuses = statuses

    def server_status_map(self):
        return dict(self.statuses)


def _write_control_config(root: Path) -> Path:
    config_dir = root / "decoy-controls"
    config_dir.mkdir(parents=True)
    document = {
        "deployment_name": "decoy-controls",
        "purpose": "control",
        "target": None,
        "capture_interface": "eno2",
        "deployments": [
            {"behavior": behavior, "flavor": flavor, "count": 1}
            for behavior, flavor in CANONICAL
        ],
    }
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    (config_dir / "hosts.ini").write_text(
        "[openstack_controller]\nlocalhost ansible_connection=local\n",
        encoding="utf-8",
    )
    return config_dir


class Phase4OperatorCorrectionTests(unittest.TestCase):
    def test_four_controls_render_contract_brains_models_and_header(self):
        deployments = [
            {"behavior": behavior, "flavor": flavor, "count": 1}
            for behavior, flavor in CANONICAL
        ]
        lines = feedback.config_vm_table_lines(deployments, indent="")
        rendered = "\n".join(lines)

        self.assertEqual(
            [line.split() for line in lines[2:]],
            [
                ["scripted-cpu", "Scripted", "v1.14vcpu.28g", "—"],
                ["mchp-cpu", "MCHP", "v1.14vcpu.28g", "—"],
                ["browseruse-gpu", "BrowserUse", "v100-1gpu.14vcpu.28g", "gemma4:26b"],
                ["smolagents-gpu", "SmolAgents", "v100-1gpu.14vcpu.28g", "gemma4:26b"],
            ],
        )
        self.assertTrue(lines[0].startswith("SUP config"))
        self.assertNotIn("Behavior", rendered)

    def test_display_reads_the_existing_capabilities_contract(self):
        self.assertEqual(feedback.WORKFLOW_CAPABILITIES_PATH, CAPABILITIES_PATH)
        capabilities = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))
        profile_name = next(iter(
            capabilities["brain_profiles"]["browseruse-gpu"]
        ))
        capabilities["brain_profiles"]["browseruse-gpu"][profile_name]["model"][
            "ollama"
        ] = "contract-test-model"

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capabilities-v1.json"
            path.write_text(json.dumps(capabilities), encoding="utf-8")
            with mock.patch.object(feedback, "WORKFLOW_CAPABILITIES_PATH", path):
                lines = feedback.config_vm_table_lines(
                    [{"behavior": "browseruse-gpu", "flavor": "gpu", "count": 1}],
                    indent="",
                )
        self.assertIn("contract-test-model", lines[-1])

    def test_controls_are_labeled_control_not_baseline(self):
        with mock.patch(
            "deployment_engine.core.plan.find_decoy_control_generation",
            return_value=Path("/controls/2026-08-24_1456Z"),
        ):
            plan = build_deploy_plan(
                "decoy",
                want_controls=True,
                want_feedback=False,
                configs_spec=None,
                single_selector=None,
                target=None,
                source=None,
                deploy_dir=REPOSITORY_ROOT / "deployments",
            )
        self.assertEqual(plan[0]["label"], "decoy-controls (control)")
        self.assertNotIn("baseline", plan[0]["label"])

    def test_legacy_run_is_silent_unparsed_and_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = _write_control_config(Path(temporary))
            legacy = config_dir / "runs" / LEGACY_RUN
            legacy.mkdir(parents=True)
            malformed = legacy / "config.yaml"
            malformed.write_text("not: [valid\n", encoding="utf-8")
            before = malformed.read_bytes()

            stderr = StringIO()
            with (
                mock.patch.object(
                    spinup, "OpenStack", side_effect=AssertionError("cloud queried")
                ),
                redirect_stderr(stderr),
            ):
                active = spinup._active_current_runs(
                    config_dir, "decoy-controls"
                )

            self.assertEqual(active, [])
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(malformed.read_bytes(), before)
            self.assertTrue(legacy.is_dir())

    def test_exact_active_dated_run_blocks_before_provisioning(self):
        with tempfile.TemporaryDirectory() as temporary:
            deploy_dir = Path(temporary)
            config_dir = _write_control_config(deploy_dir)
            prior = config_dir / "runs" / CURRENT_RUN
            prior.mkdir(parents=True)
            sentinel = prior / "historical-state"
            sentinel.write_text("untouched\n", encoding="utf-8")
            prefix = make_vm_prefix(make_run_dep_id("decoy-controls", CURRENT_RUN))
            cloud = _Cloud({prefix + "scripted-cpu-0": "BUILD"})

            stderr = StringIO()
            with (
                mock.patch.object(spinup, "OpenStack", return_value=cloud),
                mock.patch.object(spinup, "AnsibleRunner") as runner,
                redirect_stderr(stderr),
            ):
                result = spinup.run_decoy_spinup("decoy-controls", deploy_dir)

            self.assertEqual(result, 1)
            runner.assert_not_called()
            self.assertIn(
                f"./teardown decoy-controls-{CURRENT_RUN}", stderr.getvalue()
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")
            self.assertEqual(
                sorted(path.name for path in (config_dir / "runs").iterdir()),
                [CURRENT_RUN],
            )

    def test_ended_historical_run_does_not_block_fresh_deploy(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = _write_control_config(Path(temporary))
            ended = config_dir / "runs" / CURRENT_RUN
            ended.mkdir(parents=True)
            record = ended / "deployment.json"
            record.write_text(
                json.dumps({"ended_at": "2026-08-20T21:00:00Z"}),
                encoding="utf-8",
            )

            with mock.patch.object(
                spinup, "OpenStack", return_value=_Cloud({})
            ):
                active = spinup._active_current_runs(
                    config_dir, "decoy-controls"
                )

            self.assertEqual(active, [])
            self.assertTrue(ended.is_dir())
            self.assertTrue(record.is_file())

    def test_deploy_spinup_has_no_automatic_destructive_path(self):
        source = Path(spinup.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_teardown_matching_prior_runs", source)
        self.assertNotIn("server_delete_many", source)
        self.assertNotIn("close_deployment", source)
        self.assertNotIn("safe_rmtree", source)
        self.assertNotIn("dropped prior run_dir", source)


if __name__ == "__main__":
    unittest.main()
