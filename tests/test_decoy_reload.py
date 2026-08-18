from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from common.emulation_loop import BaseEmulationLoop


@dataclass
class Prompts:
    task: str
    content: str


class Workflow:
    name = "BrowseWeb"

    def __init__(self):
        self.prompts = Prompts("native task", "native content")
        self.max_steps = 7
        self._agent = object()


class Loop(BaseEmulationLoop):
    apply_count = 0

    def _load_workflows(self):
        return []

    def _execute_workflow(self, workflow):
        return True

    def _apply_brain_specific_config(self, fc):
        self.apply_count += 1

    def _agent_type_label(self):
        return "test"


class ReloadReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.loop = Loop()
        self.workflow = Workflow()
        self.loop.workflows = [self.workflow]

    def apply(self, text):
        return self.loop._apply_prompt_overlay(
            Prompts, "fallback task", text, reset_agent=True
        )

    def test_prompt_overlay_is_idempotent_and_replaceable(self):
        applied, changed = self.apply("first")
        self.assertEqual((applied, changed), (1, 1))
        self.assertEqual(
            self.workflow.prompts.content,
            "native content\n\n[PHASE Behavioral Guidance]\nfirst",
        )

        marker = object()
        self.workflow._agent = marker
        _, changed = self.apply("first")
        self.assertEqual(changed, 0)
        self.assertIs(self.workflow._agent, marker)

        self.apply("second")
        self.assertNotIn("first", self.workflow.prompts.content)
        self.assertTrue(self.workflow.prompts.content.endswith("second"))

        self.apply("")
        self.assertEqual(self.workflow.prompts, Prompts("native task", "native content"))

    def test_baseline_attr_never_tracks_mutated_value(self):
        self.assertEqual(self.loop._baseline_attr(self.workflow, "max_steps"), 7)
        self.workflow.max_steps = 99
        self.assertEqual(self.loop._baseline_attr(self.workflow, "max_steps"), 7)

    def test_versioned_schedule_rejects_overlap_and_unknown_workflows(self):
        overlap = [
            {"hour_range": [0, 12], "workflow_weights": {"BrowseWeb": 1}},
            {"hour_range": [11, 24], "workflow_weights": {"BrowseWeb": 1}},
        ]
        with self.assertRaisesRegex(RuntimeError, "more than once"):
            self.loop._build_schedule_by_hour(overlap, strict=True)

        unknown = [
            {"hour_range": [0, 24], "workflow_weights": {"InventedTask": 1}}
        ]
        with self.assertRaisesRegex(RuntimeError, "unsupported workflow"):
            self.loop._build_schedule_by_hour(unknown, strict=True)


class FakeService:
    def __init__(self):
        self.updates = []
        self.stops = 0
        self.controller = "unset"
        self.budgets = []

    def update_config(self, *args, **kwargs):
        self.updates.append((args, kwargs))

    def stop(self):
        self.stops += 1

    def set_controller(self, controller):
        self.controller = controller

    def set_conn_budget_per_min(self, budget):
        self.budgets.append(budget)


class FileReloadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "behavior.json"
        self.document = {
            "_metadata": {"mode": "controls", "seed": 1},
            "timing": {
                "active_minute_windows": [[0, 60]],
                "target_conn_per_minute_during_active": 1,
                "min_window_minutes": 15,
                "hard_fence_seconds": 0,
            },
            "content": {},
            "behavior": {},
        }
        self.write()
        self.loop = Loop(
            behavior_config_dir=self.tempdir.name,
            config_key="B0.gemma",
        )
        self.loop.workflows = [Workflow()]

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.document, sort_keys=True))

    def test_identical_file_is_not_reapplied(self):
        self.assertTrue(self.loop._reload_behavioral_config())
        self.assertFalse(self.loop._reload_behavioral_config())
        self.assertEqual(self.loop.apply_count, 1)
        self.document["timing"]["hard_fence_seconds"] = 1
        self.write()
        self.assertTrue(self.loop._reload_behavioral_config())
        self.assertEqual(self.loop.apply_count, 2)

    def test_removed_diversity_stops_and_disables_existing_consumers(self):
        self.loop._background_svc = FakeService()
        self.loop._scripted_svc = FakeService()
        self.loop._persistent_svc = FakeService()
        self.loop._shape_controller = FakeService()
        self.loop._floor_svc = FakeService()

        self.loop._reload_behavioral_config()

        self.assertEqual(self.loop._persistent_svc.stops, 1)
        self.assertEqual(self.loop._floor_svc.stops, 1)
        self.assertEqual(self.loop._persistent_svc.updates[0][0][0], {})
        self.assertEqual(self.loop._floor_svc.updates[0][0][0], {})
        self.assertEqual(self.loop._background_svc.updates[0][0][0], {})
        self.assertIsNone(self.loop._floor_svc.controller)

    def test_declared_unknown_contract_is_rejected(self):
        self.document["_metadata"]["contract_version"] = "ruse.decoy.behavior/v99"
        self.write()
        with self.assertRaisesRegex(RuntimeError, "contract unsupported"):
            self.loop._reload_behavioral_config()
        self.assertIsNone(self.loop._behavior_config_digest)


if __name__ == "__main__":
    unittest.main()
