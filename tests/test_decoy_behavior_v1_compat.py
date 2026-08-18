from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import random
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.behavioral_config import (
    MODE_CONTROLS,
    apply_phase_seed,
    load_behavioral_config,
)
from common.logging.agent_logger import AgentLogger
from runners.run_config import get_config


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "decoy" / "v1"


def _read_json(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


CASES = _read_json("cases.json")
EXPECTED = _read_json("expected-normalized.json")
SOURCE_ROOT = Path(CASES["source_root"])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_dir(case: dict) -> Path:
    return (SOURCE_ROOT / case["relative_path"]).parent


def _expected_config(config_key: str) -> dict:
    expected = copy.deepcopy(EXPECTED["shared"])
    expected.update(EXPECTED["overrides"][config_key])
    return expected


class CanonicalSourceTests(unittest.TestCase):
    def test_fixture_is_anchored_to_requested_baseline(self):
        baseline = "142733a6b7bf7c4465d475951c15ded07a08b8bd"
        manifest_hash = (
            "41206dde30c478c0bc623631f86a82994"
            "c6bb16f19026f18b6ee07e74316d540"
        )
        self.assertEqual(CASES["baseline_commit"], baseline)
        self.assertEqual(EXPECTED["baseline_commit"], baseline)
        self.assertEqual(CASES["manifest"]["sha256"], manifest_hash)
        self.assertEqual(EXPECTED["manifest_sha256"], manifest_hash)

    def test_manifest_and_behavior_byte_hashes(self):
        manifest_path = SOURCE_ROOT / CASES["manifest"]["relative_path"]
        self.assertTrue(manifest_path.is_file(), manifest_path)
        self.assertEqual(_sha256(manifest_path), CASES["manifest"]["sha256"])

        manifest = json.loads(manifest_path.read_text())
        manifest_keys = {run["sup_config"] for run in manifest["sup_runs"]}
        fixture_keys = {case["config_key"] for case in CASES["cases"]}
        self.assertEqual(manifest_keys, fixture_keys)
        self.assertEqual(manifest["mode"], MODE_CONTROLS)

        for case in CASES["cases"]:
            with self.subTest(config_key=case["config_key"]):
                path = SOURCE_ROOT / case["relative_path"]
                self.assertTrue(path.is_file(), path)
                self.assertEqual(_sha256(path), case["sha256"])
                document = json.loads(path.read_text())
                metadata = document["_metadata"]
                self.assertEqual(metadata["sup_config"], case["config_key"])
                self.assertEqual(metadata["mode"], MODE_CONTROLS)
                self.assertNotIn("contract_version", metadata)


class NormalizedConfigurationTests(unittest.TestCase):
    def test_all_seven_controls_match_pinned_normalized_goldens(self):
        for case in CASES["cases"]:
            key = case["config_key"]
            with self.subTest(config_key=key):
                actual = asdict(load_behavioral_config(_case_dir(case), key))
                self.assertEqual(actual, _expected_config(key))

    def test_seed_application_and_deterministic_logger_identity(self):
        for case in CASES["cases"]:
            key = case["config_key"]
            seed = EXPECTED["overrides"][key]["seed"]
            config = SimpleNamespace(seed=42)
            with self.subTest(config_key=key), tempfile.TemporaryDirectory() as td:
                with patch.dict(os.environ, {"SUP_OLLAMA_SEED": "stale"}), patch(
                    "random.seed"
                ) as seed_global, patch("builtins.print"):
                    applied = apply_phase_seed(config, _case_dir(case))
                    self.assertEqual(applied, seed)
                    self.assertEqual(config.seed, seed)
                    self.assertEqual(os.environ["SUP_OLLAMA_SEED"], str(seed))
                    seed_global.assert_called_once_with(seed)

                    logger = AgentLogger(key, log_dir=td)
                    expected_sid = f"{random.Random(seed).getrandbits(32):08x}"
                    self.assertEqual(logger.session_id, expected_sid)
                    logger._file_handle.close()

    def test_utc_window_and_fence_values(self):
        for case in CASES["cases"]:
            key = case["config_key"]
            with self.subTest(config_key=key):
                config = load_behavioral_config(_case_dir(case), key)
                self.assertEqual(config.active_minute_windows, [[780, 840]])
                self.assertEqual(config.active_hour, 13)
                self.assertEqual(config.off_hour, 14)
                self.assertEqual(config.min_window_minutes, 60)
                self.assertEqual(config.hard_fence_seconds, 60)


class _FakeLogger:
    instances: list["_FakeLogger"] = []

    def __init__(self, agent_type: str):
        self.agent_type = agent_type
        self.starts = []
        self.ends = 0
        self.__class__.instances.append(self)

    def session_start(self, config=None, **_kwargs):
        self.starts.append(config)

    def session_fail(self, **_kwargs):
        raise AssertionError("controls dispatch unexpectedly failed")

    def session_end(self):
        self.ends += 1

    def info(self, *_args, **_kwargs):
        pass


class ControlsDispatchTests(unittest.TestCase):
    RUNNERS = {
        "browseruse": ("runners.run_browseruse", "run_browseruse_loop"),
        "smolagents": ("runners.run_smolagents", "run_smolagents_loop"),
        "mchp": ("runners.run_mchp", "run_mchp"),
    }

    def setUp(self):
        _FakeLogger.instances = []

    def test_all_configs_dispatch_to_scripted_controls_runner(self):
        controls = importlib.import_module("brains.controls")
        for case in CASES["cases"]:
            key = case["config_key"]
            module_name, entrypoint_name = self.RUNNERS[case["brain"]]
            runner_module = importlib.import_module(module_name)
            entrypoint = getattr(runner_module, entrypoint_name)
            config = copy.deepcopy(get_config(key))
            calls = []

            def fake_controls(config_key, behavior_config_dir=None, logger=None):
                calls.append((config_key, behavior_config_dir, logger))

            with self.subTest(config_key=key), patch.object(
                runner_module, "AgentLogger", _FakeLogger
            ), patch.object(controls, "run_controls", fake_controls), patch(
                "random.seed"
            ), patch("builtins.print"), patch.dict(os.environ, {}, clear=False):
                entrypoint(config, behavior_config_dir=str(_case_dir(case)))

            self.assertEqual(case["dispatch"], "scripted_baseline")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], key)
            self.assertEqual(Path(calls[0][1]), _case_dir(case))
            self.assertEqual(config.seed, EXPECTED["overrides"][key]["seed"])
            logger = _FakeLogger.instances[-1]
            self.assertEqual(logger.starts[0]["brain"], "controls")
            self.assertEqual(logger.starts[0]["launched_from"], case["brain"])
            self.assertEqual(logger.ends, 1)

    def test_controls_runner_uses_half_open_utc_window(self):
        runner = importlib.import_module("brains.controls.runner")
        windows = [[780, 840]]

        class FrozenDateTime:
            current = datetime(2026, 1, 1, 12, 59, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                self.assertIs(tz, timezone.utc)
                return cls.current

        with patch.object(runner, "datetime", FrozenDateTime):
            self.assertIsNone(runner._current_window(windows))
            FrozenDateTime.current = datetime(
                2026, 1, 1, 13, 0, tzinfo=timezone.utc
            )
            self.assertEqual(runner._current_window(windows), (780, 840))
            FrozenDateTime.current = datetime(
                2026, 1, 1, 13, 59, 59, tzinfo=timezone.utc
            )
            self.assertEqual(runner._current_window(windows), (780, 840))
            FrozenDateTime.current = datetime(
                2026, 1, 1, 14, 0, tzinfo=timezone.utc
            )
            self.assertIsNone(runner._current_window(windows))

    def test_controls_runner_honors_final_minute_fence(self):
        runner = importlib.import_module("brains.controls.runner")
        case = next(c for c in CASES["cases"] if c["config_key"] == "M1")

        class StopLoop(Exception):
            pass

        class FrozenDateTime:
            @classmethod
            def now(cls, tz=None):
                self.assertIs(tz, timezone.utc)
                return datetime(2026, 1, 1, 13, 59, 30, tzinfo=timezone.utc)

        with patch.object(runner, "datetime", FrozenDateTime), patch.object(
            runner, "sleep", side_effect=StopLoop
        ) as sleep_mock, patch.object(
            runner, "_fetch_search", return_value=True
        ) as search_mock, patch.object(
            runner, "_fetch_browse", return_value=True
        ) as browse_mock:
            with self.assertRaises(StopLoop):
                runner.run_controls("M1", behavior_config_dir=str(_case_dir(case)))

        search_mock.assert_not_called()
        browse_mock.assert_not_called()
        sleep_mock.assert_called_once_with(31.0)


class ExemptionAndFailureTests(unittest.TestCase):
    def test_c0_and_m0_are_explicit_no_behavior_exemptions(self):
        exemptions = {item["config_key"] for item in CASES["no_behavior_exemptions"]}
        self.assertEqual(exemptions, {"C0", "M0"})

        spinup = importlib.import_module("deployment_engine.decoy.spinup")
        config = SimpleNamespace(
            deployments=[{"behavior": "C0"}, {"behavior": "M0"}]
        )
        self.assertEqual(spinup._validate_behavior_source(str(SOURCE_ROOT), config), [])

    def test_loader_fails_loud_for_missing_malformed_or_incompatible_input(self):
        cases = {
            "missing": (None, RuntimeError, "behavior.json missing"),
            "malformed": ("{not-json", json.JSONDecodeError, None),
            "bad-mode": (
                json.dumps({"_metadata": {"mode": "mystery"}, "timing": {}}),
                RuntimeError,
                "mode contract violated",
            ),
            "unsupported-version": (
                json.dumps(
                    {
                        "_metadata": {
                            "mode": "controls",
                            "contract_version": "ruse.decoy.behavior/v99",
                        },
                        "timing": {},
                    }
                ),
                RuntimeError,
                "contract unsupported",
            ),
        }

        for name, (contents, error_type, message) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as td:
                if contents is not None:
                    (Path(td) / "behavior.json").write_text(contents)
                context = (
                    self.assertRaisesRegex(error_type, message)
                    if message
                    else self.assertRaises(error_type)
                )
                with context, patch("builtins.print"):
                    load_behavioral_config(Path(td), "B0.gemma")


if __name__ == "__main__":
    unittest.main()
