from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from deployment_engine.core.config import DeploymentConfig
from deployment_engine.core.feedback import generate_feedback_config
from deployment_engine.core.phase_run_registry import (
    CONTRACT_PATH,
    PhaseRunRegistryError,
    close_deployment,
    create_deployment,
    deployment_path,
    run_id_from_started_at,
)


PHASE_SCHEMA = Path(
    "/home/ubuntu/PHASE/contracts/phase-run-v1/phase-run-v1.schema.json"
)
UTC = timezone.utc


def vm(name="vm-0", ip="10.0.0.10", sup_config=None):
    return {"name": name, "ip": ip, "sup_config": sup_config}


class PhaseRunRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "experiments"
        self.started = datetime(2026, 8, 19, 13, 0, 0, tzinfo=UTC)

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, experiment_id="decoy-controls", **updates):
        values = {
            "experiment_id": experiment_id,
            "system": "decoy",
            "purpose": "control",
            "target": None,
            "started_at": self.started,
            "capture_interface": "eno2",
            "vms": [vm(sup_config="scripted-cpu")],
            "experiments_root": self.root,
        }
        values.update(updates)
        return create_deployment(**values)

    def read(self, experiment_id, run_id):
        path = deployment_path(
            experiment_id, run_id, experiments_root=self.root
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_decoy_preserves_four_selected_configs_and_null_sidecar(self):
        selected = (
            "scripted-cpu",
            "mchp-cpu",
            "browseruse-gpu",
            "smolagents-gpu",
        )
        vms = [
            vm(f"unrelated-name-{index}", f"10.0.0.{index + 1}", config)
            for index, config in enumerate(selected)
        ]
        vms.append(vm("neighborhood-sidecar", "10.0.0.20", None))
        run_id, _path = self.create(vms=vms)
        record = self.read("decoy-controls", run_id)
        self.assertEqual(
            [item["sup_config"] for item in record["vms"]],
            [*selected, None],
        )
        self.assertNotIn("experiment_id", record)
        self.assertNotIn("run_id", record)

    def test_rampart_and_ghosts_have_explicit_null_sup_config(self):
        for system in ("rampart", "ghosts"):
            with self.subTest(system=system):
                experiment = f"{system}-controls"
                run_id, _path = self.create(
                    experiment,
                    system=system,
                    vms=[vm(f"{system}-node", "10.0.0.30", None)],
                )
                self.assertIsNone(
                    self.read(experiment, run_id)["vms"][0]["sup_config"]
                )

    def test_utc_run_id_is_exact_and_two_runs_do_not_overwrite(self):
        local = datetime(
            2026, 8, 19, 9, 5, 23,
            tzinfo=timezone(timedelta(hours=-4)),
        )
        first_id, first_path = self.create(started_at=local)
        second_id, second_path = self.create(
            started_at=local + timedelta(days=1)
        )
        self.assertEqual(first_id, "2026-08-19_130523Z")
        self.assertEqual(second_id, "2026-08-20_130523Z")
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertEqual(
            self.read("decoy-controls", first_id)["started_at"],
            "2026-08-19T13:05:23Z",
        )
        for invalid in ("opaque", "20260819T130523Z", "2026-99-19_130523Z"):
            with self.assertRaises(PhaseRunRegistryError):
                deployment_path(
                    "decoy-controls", invalid, experiments_root=self.root
                )

    def test_concurrent_deployment_configs_do_not_share_a_file(self):
        def register(experiment, target, ip):
            return self.create(
                experiment,
                purpose="feedback",
                target=target,
                vms=[vm(f"{target}-vm", ip, "scripted-cpu")],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(register, "summer-run", "axes-summer24", "10.0.0.41"),
                pool.submit(register, "spring-run", "axes-spring25", "10.0.0.42"),
            ]
        paths = [future.result()[1] for future in futures]
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual(
            {json.loads(path.read_text())["target"] for path in paths},
            {"axes-summer24", "axes-spring25"},
        )

    def test_duplicate_registration_fails_without_replacement(self):
        run_id, path = self.create()
        original = path.read_bytes()
        with self.assertRaisesRegex(PhaseRunRegistryError, "already exists"):
            self.create()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(run_id, "2026-08-19_130000Z")

    def test_close_is_exact_and_repeated_close_preserves_first_timestamp(self):
        first_id, _path = self.create()
        second_id, _path = self.create(
            started_at=self.started + timedelta(days=1)
        )
        first_end = self.started + timedelta(hours=3)
        close_deployment(
            "decoy-controls",
            first_id,
            ended_at=first_end,
            experiments_root=self.root,
        )
        close_deployment(
            "decoy-controls",
            first_id,
            ended_at=first_end + timedelta(hours=2),
            experiments_root=self.root,
        )
        self.assertEqual(
            self.read("decoy-controls", first_id)["ended_at"],
            "2026-08-19T16:00:00Z",
        )
        self.assertIsNone(self.read("decoy-controls", second_id)["ended_at"])

    def test_restart_does_not_create_or_modify_a_record(self):
        _run_id, path = self.create()
        before = path.read_bytes()
        paths_before = sorted(self.root.rglob("deployment.json"))
        # A service restart has no registry operation; only fleet registration
        # and teardown call the writer and closer.
        paths_after = sorted(self.root.rglob("deployment.json"))
        self.assertEqual(paths_after, paths_before)
        self.assertEqual(path.read_bytes(), before)

    def test_shrink_is_absent_from_cli_dispatch(self):
        from deployment_engine import __main__ as deployment_cli

        stderr = StringIO()
        with (
            mock.patch.object(deployment_cli.output, "start_session_log"),
            mock.patch.object(deployment_cli.output, "close_session_log"),
            redirect_stderr(stderr),
        ):
            result = deployment_cli.main(["shrink"])

        self.assertEqual(result, 1)
        self.assertIn("Unknown command: shrink", stderr.getvalue())
        self.assertNotIn("shrink", stderr.getvalue().split("Usage: ", 1)[-1])
        self.assertFalse(hasattr(deployment_cli, "_cmd_shrink"))
        self.assertFalse(hasattr(deployment_cli, "_shrink_parser"))
        repository_root = Path(__file__).resolve().parents[1]
        self.assertFalse((repository_root / "shrink").exists())
        self.assertFalse((repository_root / "deployment_engine" / "shrink.py").exists())

    def test_invalid_fields_fail_before_persistence(self):
        cases = (
            {"purpose": "unknown"},
            {"purpose": "feedback", "target": None},
            {"target": "Axes Summer 24"},
            {"system": "rampart"},
            {"capture_interface": ""},
            {"started_at": datetime(2026, 8, 19, 13, 0, 0)},
            {"vms": []},
            {"vms": [vm(ip="not-an-ip")]},
            {"vms": [{"name": "vm", "ip": "10.0.0.1"}]},
            {"vms": [{**vm(), "extra": "not-allowed"}]},
        )
        for index, updates in enumerate(cases):
            with self.subTest(updates=updates), self.assertRaises(
                PhaseRunRegistryError
            ):
                self.create(f"invalid-{index}", **updates)
        self.assertEqual(list(self.root.rglob("deployment.json")), [])

        config_path = Path(self.temporary.name) / "config.yaml"
        config_path.write_text("deployment_name: missing-fields\n")
        with self.assertRaisesRegex(ValueError, "purpose, target"):
            DeploymentConfig.load(config_path)
        config_path.write_text(
            "deployment_name: complete\n"
            "purpose: feedback\n"
            "target: axes-summer24\n"
            "capture_interface: eno2\n"
        )
        config = DeploymentConfig.load(config_path)
        self.assertEqual(
            (config.purpose, config.target, config.capture_interface),
            ("feedback", "axes-summer24", "eno2"),
        )

    def test_feedback_generator_copies_manifest_training_dataset_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source-name-must-not-be-used"
            (source / "B" / "B0").mkdir(parents=True)
            (source / "B" / "B0" / "behavior.json").write_text("{}")
            (source / "manifest.json").write_text(json.dumps({
                "deploy_key": "decoy-controls",
                "target": "decoy",
                "training_dataset": "cptc11-zeektx",
                "version_preset": "control",
            }))
            deploy_root = base / "deployments"
            name = generate_feedback_config(source, "all", deploy_root)
            generated = yaml.safe_load(
                (deploy_root / name / "config.yaml").read_text()
            )
            self.assertEqual(generated["target"], "cptc11-zeektx")
            self.assertNotEqual(generated["target"], source.name)
            self.assertNotEqual(generated["target"], "decoy")

            (source / "manifest.json").write_text(json.dumps({
                "deploy_key": "decoy-controls",
                "target": "decoy",
            }))
            with self.assertRaises(SystemExit):
                generate_feedback_config(source, "all", deploy_root)
            (source / "manifest.json").unlink()
            with self.assertRaises(SystemExit):
                generate_feedback_config(source, "all", deploy_root)

    def test_registry_never_uses_global_experiments_json(self):
        legacy = self.root.parent / "experiments.json"
        legacy.write_text('{"sentinel": true}\n')
        before = legacy.read_bytes()
        run_id, _path = self.create()
        close_deployment(
            "decoy-controls",
            run_id,
            ended_at=self.started + timedelta(hours=1),
            experiments_root=self.root,
        )
        self.assertEqual(legacy.read_bytes(), before)
        registry_source = Path(
            "deployment_engine/core/phase_run_registry.py"
        ).read_text()
        self.assertNotIn("experiments.json", registry_source)
        self.assertFalse(Path("deployment_engine/core/register_experiment.py").exists())

    def test_persisted_record_validates_against_byte_identical_phase_schema(self):
        self.assertEqual(CONTRACT_PATH.read_bytes(), PHASE_SCHEMA.read_bytes())
        run_id, path = self.create()
        record = json.loads(path.read_text())
        schema = json.loads(CONTRACT_PATH.read_text())
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).validate(record)
        self.assertEqual(
            set(record),
            {
                "system",
                "purpose",
                "target",
                "started_at",
                "ended_at",
                "capture_interface",
                "vms",
            },
        )
        self.assertFalse((path.parent / "events").exists())
        self.assertEqual(run_id_from_started_at(self.started), run_id)


if __name__ == "__main__":
    unittest.main()
