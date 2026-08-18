from __future__ import annotations

import copy
import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.behavior_v2 import (
    BEHAVIOR_CONTRACT_V2,
    BehaviorV2Error,
    BehaviorV2ReloadManager,
    BehaviorV2Snapshot,
    BehaviorV2StaticChangeError,
    MCHP_WORKFLOWS,
    SCHEMA_PATH,
    SIDECAR_LEGACY_KEYS,
    infer_runtime_capability,
    load_behavior_v2_bytes,
)
from common.emulation_loop import BaseEmulationLoop
from common.behavioral_config import (
    load_behavioral_config,
    load_workflow_registration,
    resolve_runtime_dispatch,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "decoy" / "v2"
V1_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "decoy" / "v1"
SCHEMA_SHA256 = "d7bb0afddcd2fafa67306fa7ae295e735559ed02a873257b097d9c5250a9a1be"
FIXTURES = {
    "browseruse-rtx.json": ("B0R.gemma", "browseruse", "rtx"),
    "smolagents-cpu.json": ("S0C.gemma", "smolagents", "cpu"),
    "mchp.json": ("M1", "mchp", "cpu"),
}


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _dump(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


class AcceptedArtifactTests(unittest.TestCase):
    def test_frozen_schema_matches_accepted_phase_bytes(self):
        self.assertEqual(hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(), SCHEMA_SHA256)

    def test_representative_documents_load_as_immutable_snapshots(self):
        for name, (config_key, brain, hardware) in FIXTURES.items():
            with self.subTest(name=name):
                raw = (FIXTURE_DIR / name).read_bytes()
                snapshot = load_behavior_v2_bytes(raw, config_key)
                self.assertIsInstance(snapshot, BehaviorV2Snapshot)
                self.assertEqual(snapshot.contract_version, BEHAVIOR_CONTRACT_V2)
                self.assertEqual(snapshot.brain, brain)
                self.assertEqual(snapshot.document["execution"]["hardware_class"], hardware)
                self.assertEqual(snapshot.raw_sha256, hashlib.sha256(raw).hexdigest())
                with self.assertRaises(TypeError):
                    snapshot.document["shape"] = {}  # type: ignore[index]
                with self.assertRaises(TypeError):
                    snapshot.document["execution"]["brain"] = "mchp"  # type: ignore[index]
                self.assertIsInstance(snapshot.document["activity"]["windows_utc"], tuple)

    def test_companion_report_is_rejected_as_runtime_configuration(self):
        report = (FIXTURE_DIR / "example-report.json").read_bytes()
        with self.assertRaisesRegex(BehaviorV2Error, "expected contract"):
            load_behavior_v2_bytes(report, "B0R.gemma")

    def test_capability_manifest_matches_loader_and_frozen_schema(self):
        manifest_path = SCHEMA_PATH.with_name("capabilities-v2.json")
        manifest = json.loads(manifest_path.read_text())
        from runners.run_config import CONFIGS

        self.assertEqual(manifest["schema"]["sha256"], SCHEMA_SHA256)
        self.assertEqual(
            manifest["runtime_baseline"],
            "142733a6b7bf7c4465d475951c15ded07a08b8bd",
        )
        self.assertEqual(set(manifest["brains"]["mchp"]), set(MCHP_WORKFLOWS))
        registered = {
            config_key
            for group in manifest["sup_capabilities"]
            for config_key in group["config_keys"]
        }
        self.assertEqual(registered, set(CONFIGS) - {"C0", "M0"})
        self.assertEqual(
            manifest["reload"]["identity"],
            "sha256 of exact file bytes",
        )

    def test_every_sidecar_key_is_normalized_to_the_existing_ruse_key(self):
        document = _load("browseruse-rtx.json")
        rates = {
            key: index
            for index, key in enumerate(SIDECAR_LEGACY_KEYS, start=1)
        }
        document["sidecar"]["topology_inbound"] = {
            "enabled": True,
            "probes_per_hour": rates,
        }
        snapshot = load_behavior_v2_bytes(_dump(document), "B0R.gemma")
        self.assertEqual(
            dict(snapshot.sidecar_legacy_rates),
            {
                legacy_key: rates[v2_key]
                for v2_key, legacy_key in SIDECAR_LEGACY_KEYS.items()
            },
        )
        with self.assertRaises(TypeError):
            snapshot.sidecar_legacy_rates["inbound_smb_per_hour"] = 9


class CapabilityAndDispatchTests(unittest.TestCase):
    def test_capability_inference_matches_supported_tiers(self):
        self.assertEqual(infer_runtime_capability("M1"), ("mchp", "cpu"))
        self.assertEqual(infer_runtime_capability("B0C.gemma"), ("browseruse", "cpu"))
        self.assertEqual(infer_runtime_capability("S0R.gemma"), ("smolagents", "rtx"))
        self.assertEqual(infer_runtime_capability("B2.gemma"), ("browseruse", "gpu"))
        with self.assertRaisesRegex(BehaviorV2Error, "pinned capability registry"):
            infer_runtime_capability("B99.gemma")

    def test_sup_brain_and_hardware_mismatches_are_rejected(self):
        mutations = (
            ("sup", lambda d: d["_metadata"].__setitem__("sup_config", "M2")),
            ("brain", lambda d: d["execution"].__setitem__("brain", "browseruse")),
            ("hardware", lambda d: d["execution"].__setitem__("hardware_class", "gpu")),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                document = _load("mchp.json")
                mutate(document)
                with self.assertRaises(BehaviorV2Error):
                    load_behavior_v2_bytes(_dump(document), "M1")

    def test_mchp_whitelist_is_exact_and_microsoft_paint_is_rejected(self):
        document = _load("mchp.json")
        for workflow in sorted(MCHP_WORKFLOWS - set(document["execution"]["enabled_workflows"])):
            document["execution"]["enabled_workflows"].append(workflow)
            document["channels"]["workflows"]["starts_per_utc_hour"][workflow] = (
                [0] * 9 + [0.2] * 9 + [0] * 6
            )
        snapshot = load_behavior_v2_bytes(_dump(document), "M1")
        self.assertEqual(set(snapshot.enabled_workflows), set(MCHP_WORKFLOWS))

        document["execution"]["enabled_workflows"].append("MicrosoftPaint")
        document["channels"]["workflows"]["starts_per_utc_hour"]["MicrosoftPaint"] = (
            [0] * 9 + [0.2] * 9 + [0] * 6
        )
        with self.assertRaises(BehaviorV2Error):
            load_behavior_v2_bytes(_dump(document), "M1")

    def test_version_first_m1_dispatch(self):
        v2 = load_behavior_v2_bytes((FIXTURE_DIR / "mchp.json").read_bytes(), "M1")
        self.assertNotIn("mode", v2.document["_metadata"])
        self.assertEqual(resolve_runtime_dispatch(v2, "mchp", "M1"), "mchp")

        cases = json.loads((V1_FIXTURE_DIR / "cases.json").read_text())
        case = next(case for case in cases["cases"] if case["config_key"] == "M1")
        v1_dir = Path(cases["source_root"]) / Path(case["relative_path"]).parent
        v1 = load_behavioral_config(v1_dir, "M1")
        self.assertIsNone(v1.contract_version)
        self.assertEqual(
            resolve_runtime_dispatch(v1, "mchp", "M1"),
            "scripted_baseline",
        )
        v1.mode = "feedback"
        self.assertEqual(
            resolve_runtime_dispatch(v1, "mchp", "M1"),
            "scripted_baseline",
        )

    def test_v2_mode_injection_cannot_force_controls_dispatch(self):
        document = _load("mchp.json")
        document["_metadata"]["mode"] = "controls"
        with self.assertRaises(BehaviorV2Error):
            load_behavior_v2_bytes(_dump(document), "M1")

    def test_v2_registration_is_exact_and_v1_uses_legacy_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "behavior.json"
            path.write_bytes((FIXTURE_DIR / "mchp.json").read_bytes())
            self.assertEqual(
                load_workflow_registration(Path(td), "M1"),
                tuple(_load("mchp.json")["execution"]["enabled_workflows"]),
            )
            legacy = {"_metadata": {"mode": "controls", "seed": 1}}
            path.write_text(json.dumps(legacy))
            self.assertIsNone(load_workflow_registration(Path(td), "M1"))

    def test_m1_runner_takes_mchp_path_for_explicit_v2(self):
        runner = importlib.import_module("runners.run_mchp")
        brains_mchp = importlib.import_module("brains.mchp")
        controls = importlib.import_module("brains.controls")
        from runners.run_config import get_config

        calls = []

        class FakeLogger:
            def __init__(self, *_args, **_kwargs):
                pass

            def session_start(self, **_kwargs):
                pass

            def session_success(self, **_kwargs):
                pass

            def session_fail(self, **_kwargs):
                raise AssertionError("MCHP V2 dispatch unexpectedly failed")

            def session_end(self):
                pass

            def info(self, *_args, **_kwargs):
                pass

        class FakeAgent:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def run(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            Path(td, "behavior.json").write_bytes(
                (FIXTURE_DIR / "mchp.json").read_bytes()
            )
            config = copy.deepcopy(get_config("M1"))
            with patch.object(runner, "AgentLogger", FakeLogger), patch.object(
                brains_mchp, "MCHPAgent", FakeAgent
            ), patch.object(controls, "run_controls") as controls_run, patch(
                "builtins.print"
            ):
                runner.run_mchp(config, behavior_config_dir=td)

        self.assertEqual(len(calls), 1)
        controls_run.assert_not_called()


class SemanticValidationTests(unittest.TestCase):
    def test_enabled_workflows_must_equal_budget_keys(self):
        document = _load("mchp.json")
        del document["channels"]["workflows"]["starts_per_utc_hour"]["WhoisLookup"]
        with self.assertRaisesRegex(BehaviorV2Error, "exactly equal"):
            load_behavior_v2_bytes(_dump(document), "M1")

    def test_sub_token_and_outside_gate_budgets_are_rejected(self):
        document = _load("smolagents-cpu.json")
        document["channels"]["workflows"]["starts_per_utc_hour"]["WhoisLookup"] = (
            [0] * 8 + [0.1] * 10 + [0] * 6
        )
        with self.assertRaisesRegex(BehaviorV2Error, "eligible tokens"):
            load_behavior_v2_bytes(_dump(document), "S0C.gemma")

        document = _load("smolagents-cpu.json")
        document["channels"]["workflows"]["starts_per_utc_hour"]["WhoisLookup"] = (
            [1] + [0] * 23
        )
        with self.assertRaisesRegex(BehaviorV2Error, "outside eligible"):
            load_behavior_v2_bytes(_dump(document), "S0C.gemma")

    def test_report_fields_and_unknown_actionable_fields_are_rejected(self):
        document = _load("mchp.json")
        document["model_evidence"] = []
        with self.assertRaisesRegex(BehaviorV2Error, "schema validation"):
            load_behavior_v2_bytes(_dump(document), "M1")

    def test_brain_fields_and_content_pools_follow_workflow_capabilities(self):
        smol = _load("smolagents-cpu.json")
        smol["brain"]["page_dwell_seconds"] = {"min": 1, "max": 2}
        with self.assertRaises(BehaviorV2Error):
            load_behavior_v2_bytes(_dump(smol), "S0C.gemma")

        for workflow, pool in (
            ("BrowseWeb", "browse_urls"),
            ("BrowseYouTube", "youtube_video_ids"),
            ("WebSearch", "web_search_queries"),
            ("DownloadFiles", "download_urls"),
            ("WhoisLookup", "whois_domains"),
        ):
            with self.subTest(workflow=workflow):
                document = _load("browseruse-rtx.json")
                document["brain"]["content_pools"][pool] = []
                with self.assertRaises(BehaviorV2Error):
                    load_behavior_v2_bytes(_dump(document), "B0R.gemma")

                document = _load("browseruse-rtx.json")
                document["execution"]["enabled_workflows"].remove(workflow)
                del document["channels"]["workflows"]["starts_per_utc_hour"][workflow]
                with self.assertRaises(BehaviorV2Error):
                    load_behavior_v2_bytes(_dump(document), "B0R.gemma")

    def test_disabled_channels_shape_dependencies_and_uri_schemes(self):
        document = _load("smolagents-cpu.json")
        document["channels"]["shape_floor"]["opens_per_utc_hour"][8] = 1
        with self.assertRaises(BehaviorV2Error):
            load_behavior_v2_bytes(_dump(document), "S0C.gemma")

        document = _load("browseruse-rtx.json")
        document["shape"] = {"enabled": False}
        with self.assertRaises(BehaviorV2Error):
            load_behavior_v2_bytes(_dump(document), "B0R.gemma")

        for channel in ("persistent_sessions", "shape_floor"):
            document = _load("browseruse-rtx.json")
            document["channels"][channel]["endpoint_pool"][0] = "http://example.org/"
            with self.assertRaises(BehaviorV2Error):
                load_behavior_v2_bytes(_dump(document), "B0R.gemma")

        document = _load("browseruse-rtx.json")
        document["brain"]["content_pools"]["browse_urls"][0] = (
            "http://example.org/resource"
        )
        load_behavior_v2_bytes(_dump(document), "B0R.gemma")

    def test_monotonic_probability_and_cluster_integer_invariants(self):
        mutations = (
            lambda d: d["shape"]["orig_bytes"].__setitem__("p75", 1),
            lambda d: d["brain"]["download_outcome_weights"].__setitem__(
                "success", 0.5
            ),
            lambda d: d["channels"]["workflows"]["cluster"]
            ["workflows_per_cluster"].__setitem__("p50", 1.5),
        )
        for mutate in mutations:
            document = _load("browseruse-rtx.json")
            mutate(document)
            with self.assertRaises(BehaviorV2Error):
                load_behavior_v2_bytes(_dump(document), "B0R.gemma")

        document = _load("browseruse-rtx.json")
        self.assertGreater(document["shape"]["orig_bytes"]["max"], 4096)
        load_behavior_v2_bytes(_dump(document), "B0R.gemma")


class ReloadAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.raw = (FIXTURE_DIR / "mchp.json").read_bytes()
        self.manager = BehaviorV2ReloadManager("M1")
        self.assertTrue(self.manager.activate_bytes(self.raw))
        self.original = self.manager.current

    def test_identical_raw_bytes_are_a_noop_but_reformat_is_a_new_identity(self):
        self.assertFalse(self.manager.activate_bytes(self.raw))
        document = json.loads(self.raw)
        reformatted = json.dumps(document, indent=4).encode()
        self.assertTrue(self.manager.activate_bytes(reformatted))
        self.assertNotEqual(self.manager.current.raw_sha256, self.original.raw_sha256)

    def test_hot_provenance_changes_activate_atomically(self):
        document = json.loads(self.raw)
        metadata = document["_metadata"]
        metadata["feedback_run_id"] = "replacement-run"
        metadata["generated_at"] = "2026-08-18T21:00:00Z"
        metadata["model_namespace"] = "replacement-model"
        metadata["model_version"] = "v2.1"
        metadata["iteration"] = 1
        self.assertTrue(self.manager.activate_bytes(_dump(document)))
        self.assertEqual(
            self.manager.current.document["_metadata"]["feedback_run_id"],
            "replacement-run",
        )

    def test_hot_actuator_changes_activate_as_one_new_snapshot(self):
        document = json.loads(self.raw)
        document["brain"]["page_dwell_seconds"]["max"] += 1
        document["channels"]["workflows"]["starts_per_utc_hour"][
            "BrowseWeb"
        ][10] += 0.1
        before = self.manager.current
        self.assertTrue(self.manager.activate_bytes(_dump(document)))
        self.assertIsNot(self.manager.current, before)
        self.assertEqual(
            self.manager.current.document["brain"]["page_dwell_seconds"]["max"],
            document["brain"]["page_dwell_seconds"]["max"],
        )

    def test_each_static_field_change_is_rejected_without_activation(self):
        mutations = (
            lambda d: d["_metadata"].__setitem__(
                "contract_version", "ruse.decoy.behavior/v3"
            ),
            lambda d: d["_metadata"].__setitem__("sup_config", "M2"),
            lambda d: d["_metadata"].__setitem__("seed", 7),
            lambda d: d["_metadata"].__setitem__("producer", "other.producer"),
            lambda d: d["_metadata"].__setitem__("target_dataset", "other"),
            lambda d: d["execution"].__setitem__("driver", "scripted"),
            lambda d: d["execution"].__setitem__("brain", "browseruse"),
            lambda d: d["execution"].__setitem__("hardware_class", "gpu"),
            lambda d: d["execution"].__setitem__("enabled_workflows", ["ListFiles"]),
            lambda d: d["sidecar"]["topology_inbound"].__setitem__("enabled", True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                manager = BehaviorV2ReloadManager("M1")
                manager.activate_bytes(self.raw)
                before = manager.current
                document = json.loads(self.raw)
                mutate(document)
                with self.assertRaises(BehaviorV2Error):
                    manager.activate_bytes(_dump(document))
                self.assertIs(manager.current, before)
                self.assertEqual(manager.raw_sha256, before.raw_sha256)

    def test_valid_but_static_change_reports_restart_required(self):
        document = json.loads(self.raw)
        document["_metadata"]["seed"] += 1
        with self.assertRaisesRegex(
            BehaviorV2StaticChangeError,
            "restart/redeploy required",
        ):
            self.manager.activate_bytes(_dump(document))
        self.assertIs(self.manager.current, self.original)

    def test_invalid_replacement_leaves_prior_snapshot_active(self):
        document = json.loads(self.raw)
        del document["execution"]
        with self.assertRaises(BehaviorV2Error):
            self.manager.activate_bytes(_dump(document))
        self.assertIs(self.manager.current, self.original)
        self.assertEqual(self.manager.raw_sha256, hashlib.sha256(self.raw).hexdigest())

    def test_companion_report_replacement_leaves_prior_snapshot_active(self):
        report = (FIXTURE_DIR / "example-report.json").read_bytes()
        with self.assertRaises(BehaviorV2Error):
            self.manager.activate_bytes(report)
        self.assertIs(self.manager.current, self.original)


class _LoaderOnlyLoop(BaseEmulationLoop):
    def _load_workflows(self):
        return []

    def _execute_workflow(self, workflow):
        return True

    def _apply_brain_specific_config(self, fc):
        raise AssertionError("R1 must not project V2 into legacy consumers")

    def _agent_type_label(self):
        return "r1-loader-test"


class LoopReloadIntegrationTests(unittest.TestCase):
    def test_invalid_v2_file_leaves_loop_snapshot_and_digest_active(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "behavior.json")
            path.write_bytes((FIXTURE_DIR / "mchp.json").read_bytes())
            loop = _LoaderOnlyLoop(behavior_config_dir=td, config_key="M1")
            self.assertTrue(loop._reload_behavioral_config())
            snapshot = loop._behavior_v2_snapshot
            digest = loop._behavior_config_digest

            document = _load("mchp.json")
            del document["execution"]
            path.write_bytes(_dump(document))
            with self.assertRaises(BehaviorV2Error):
                loop._reload_behavioral_config()

            self.assertIs(loop._behavior_v2_snapshot, snapshot)
            self.assertEqual(loop._behavior_config_digest, digest)

    def test_static_v2_reload_leaves_loop_snapshot_active(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "behavior.json")
            path.write_bytes((FIXTURE_DIR / "mchp.json").read_bytes())
            loop = _LoaderOnlyLoop(behavior_config_dir=td, config_key="M1")
            loop._reload_behavioral_config()
            snapshot = loop._behavior_v2_snapshot

            document = _load("mchp.json")
            document["_metadata"]["seed"] += 1
            path.write_bytes(_dump(document))
            with self.assertRaises(BehaviorV2StaticChangeError):
                loop._reload_behavioral_config()
            self.assertIs(loop._behavior_v2_snapshot, snapshot)

    def test_startup_candidate_guards_first_reload_against_static_race(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "behavior.json")
            raw = (FIXTURE_DIR / "mchp.json").read_bytes()
            initial = load_behavior_v2_bytes(raw, "M1")
            path.write_bytes(raw)
            loop = _LoaderOnlyLoop(
                behavior_config_dir=td,
                config_key="M1",
                initial_behavior_snapshot=initial,
            )

            document = _load("mchp.json")
            document["sidecar"]["topology_inbound"] = {
                "enabled": True,
                "probes_per_hour": {"smb": 1},
            }
            path.write_bytes(_dump(document))
            with self.assertRaises(BehaviorV2StaticChangeError):
                loop._reload_behavioral_config()
            self.assertIs(loop._behavior_v2_snapshot, initial)
            self.assertIsNone(loop._behavior_config_digest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
