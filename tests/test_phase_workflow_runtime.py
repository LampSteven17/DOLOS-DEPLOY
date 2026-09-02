from __future__ import annotations

import ast
import asyncio
import copy
import importlib
import inspect
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, Mock, call, patch
from zoneinfo import ZoneInfo

from common.logging.agent_logger import AgentLogger
from common.logging.llm_callbacks import setup_litellm_callbacks
from phase_workflow.brains import (
    AssignedDocumentCreation,
    AssignedBoundedTransfer,
    AssignedFileDownload,
    AssignedWebResearch,
    AssignedVideoPlayback,
    FrameworkBrain,
    ResourceBrain,
    _BrowserUseEventLoop,
    _mchp_driver,
    _read_webpage,
    browseruse_runner,
    build_brain,
    smolagents_runner,
)
from phase_workflow.configurations import CONFIGURATIONS, is_workflow_configuration
from phase_workflow.executor import DailyExecutor
from phase_workflow.loader import (
    CONTRACT_ROOT,
    WorkflowPlanError,
    load_workflow_plan,
)
from phase_workflow.registry import (
    CANONICAL_HANDLERS,
    WorkflowRegistry,
    WorkflowResult,
)
from phase_workflow.runtime import (
    _select_runtime_brain_profile,
    run_workflow_runtime,
)
from phase_workflow.workflows import (
    DailyDocumentStore,
    HttpsDocumentSync,
    KerberosShareAccess,
    MCHPDocumentWorkflows,
    OpenDocumentWriter,
    SeleniumResourceWorkflows,
    _select_media_url,
    firefox_download,
    stream_https_download,
    play_video_realtime,
    structured_llm_task,
    validate_open_document,
)
CONTROL_ROOT = (
    Path("/home/ubuntu/PHASE/plans/feedback-v2-rewrite/fixtures/controls")
)
AUTHORITATIVE_SCHEMA = Path(
    "/home/ubuntu/PHASE/plans/feedback-v2-rewrite/"
    "phase-workflow-plan-v1.schema.json"
)
AUTHORITATIVE_CAPABILITIES = Path(
    "/home/ubuntu/PHASE/plans/feedback-v2-rewrite/capabilities-v1.json"
)
AUTHORITATIVE_CONTROLS_PROFILE = Path(
    "/home/ubuntu/PHASE/plans/feedback-v2-rewrite/"
    "resource-profiles/controls-v2.json"
)
CURRENT_CONTROL_ROOT = Path(
    "/data/axes-mirror/controls/2026-08-27_1552Z"
)
REPOSITORY_ROOT = CONTRACT_ROOT.parents[1]
INSTALLER = REPOSITORY_ROOT / "INSTALL_SUP.sh"
PLAN_FILENAMES = {
    "scripted-cpu": "scripted-v1.json",
    "mchp-cpu": "mchp-v1.json",
    "browseruse-gpu": "browseruse-v1.json",
    "smolagents-gpu": "smolagents-v1.json",
}
EXPECTED_CONFIGS = {
    "scripted-cpu",
    "mchp-cpu",
    "browseruse-gpu",
    "smolagents-gpu",
}
EXPECTED_RESOURCES = (
    "google_climate_change_news",
    "video_cpp_course",
    "document_team_meeting_notes",
)


def control_document(config_key="scripted-cpu"):
    document = json.loads(
        (CONTROL_ROOT / PLAN_FILENAMES[config_key]).read_text(encoding="utf-8")
    )
    instructions = json.loads(
        (CONTRACT_ROOT / "capabilities-v1.json").read_text(encoding="utf-8")
    )["instructions"]["controls-v2"]
    workflows = ("WebResearch", "VideoViewing", "DocumentCreation")
    sequence = []
    for offset, (workflow, resource_id) in enumerate(
        zip(workflows, EXPECTED_RESOURCES)
    ):
        entry = {
            "offset_minutes": offset * 15,
            "workflow": workflow,
            "resource_id": resource_id,
        }
        if config_key in {"browseruse-gpu", "smolagents-gpu"}:
            entry["instruction"] = instructions[workflow]
        sequence.append(entry)
    document["max_parallel"] = 1
    document["schedule"] = [{"window_local": [540, 600], "sequence": sequence}]
    return document


def feedback_document(config_key="scripted-cpu"):
    document = control_document(config_key)
    document["resource_profile"] = "feedback-v2"
    replacements = {
        "WebResearch": "wikipedia_compiler",
        "VideoViewing": "video_cpp_course",
        "DocumentCreation": "document_team_meeting_notes",
    }
    instructions = json.loads(
        (CONTRACT_ROOT / "capabilities-v1.json").read_text(encoding="utf-8")
    )["instructions"]["feedback-v2"]
    for window in document["schedule"]:
        for entry in window["sequence"]:
            entry["resource_id"] = replacements[entry["workflow"]]
            if "instruction" in entry:
                entry["instruction"] = instructions[entry["workflow"]]
    return document


def six_workflow_document(config_key="scripted-cpu"):
    document = feedback_document(config_key)
    instructions = json.loads(
        (CONTRACT_ROOT / "capabilities-v1.json").read_text(encoding="utf-8")
    )["instructions"]["feedback-v2"]
    resources = (
        ("WebResearch", "wikipedia_compiler"),
        ("VideoViewing", "video_cpp_course"),
        ("FileDownload", "download_ovh_1m"),
        ("DocumentCreation", "document_team_meeting_notes"),
        ("FileSyncUpload", "cloudflare_upload"),
        ("NetworkShareAccess", "share_team_notes"),
    )
    sequence = []
    for offset, (workflow, resource_id) in enumerate(resources):
        entry = {
            "offset_minutes": offset,
            "workflow": workflow,
            "resource_id": resource_id,
        }
        if config_key in {"browseruse-gpu", "smolagents-gpu"}:
            entry["instruction"] = instructions[workflow]
        sequence.append(entry)
    document["schedule"] = [{"window_local": [540, 600], "sequence": sequence}]
    return document


def load_document(document, expected=None):
    expected = expected or document["sup_config"]
    temporary = tempfile.TemporaryDirectory()
    path = Path(temporary.name) / "behavior.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    try:
        plan = load_workflow_plan(path, expected)
    except Exception:
        temporary.cleanup()
        raise
    temporary.cleanup()
    return plan


def load_control_plan(config_key="scripted-cpu"):
    return load_document(control_document(config_key), config_key)


class FakeClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


class ManualHandle:
    def __init__(self):
        self._done = False
        self._result = None
        self._error = None
        self._callbacks = []

    def done(self):
        return self._done

    def result(self):
        if self._error:
            raise self._error
        return self._result

    def add_done_callback(self, callback):
        self._callbacks.append(callback)

    def complete(self, result=None, error=None):
        self._result = result or WorkflowResult(completed=True)
        self._error = error
        self._done = True
        for callback in self._callbacks:
            callback(self)


class RecordingStarter:
    def __init__(self):
        self.starts = []

    def __call__(self, task, local_day):
        handle = ManualHandle()
        self.starts.append((task, local_day, handle))
        return handle


class RecordingBrain:
    def __init__(self, result=None):
        self.tasks = []
        self.result = result or WorkflowResult(completed=True)

    def execute(self, task, workspace):
        self.tasks.append((task, workspace))
        return self.result


class LoaderTests(unittest.TestCase):
    def test_all_six_workflows_validate_for_all_four_versioned_brains(self):
        expected = list(CANONICAL_HANDLERS)
        for sup_config in PLAN_FILENAMES:
            with self.subTest(sup_config=sup_config):
                plan = load_document(six_workflow_document(sup_config), sup_config)
                entries = plan.windows[0].sequence
                self.assertEqual({entry.workflow for entry in entries}, set(expected))
                self.assertEqual(plan.resource_profile, "feedback-v2")
                self.assertEqual(
                    plan.brain_profile_id, PLAN_FILENAMES[sup_config][:-5]
                )

    def test_contract_assets_and_plan_names_are_versioned_only(self):
        self.assertEqual(
            (CONTRACT_ROOT / "phase-workflow-plan-v1.schema.json").read_bytes(),
            AUTHORITATIVE_SCHEMA.read_bytes(),
        )
        self.assertEqual(
            (CONTRACT_ROOT / "capabilities-v1.json").read_bytes(),
            AUTHORITATIVE_CAPABILITIES.read_bytes(),
        )
        self.assertEqual(
            (CONTRACT_ROOT / "resource-profiles/controls-v2.json").read_bytes(),
            AUTHORITATIVE_CONTROLS_PROFILE.read_bytes(),
        )
        self.assertEqual(
            PLAN_FILENAMES,
            {
                "scripted-cpu": "scripted-v1.json",
                "mchp-cpu": "mchp-v1.json",
                "browseruse-gpu": "browseruse-v1.json",
                "smolagents-gpu": "smolagents-v1.json",
            },
        )
        self.assertEqual(
            {path.name for path in (CONTRACT_ROOT / "resource-profiles").iterdir()},
            {"controls-v2.json", "feedback-v2.json"},
        )
        self.assertFalse((CONTRACT_ROOT / "target-profiles").exists())
        self.assertFalse((CONTRACT_ROOT / "controls").exists())

    def test_top_level_brain_profile_is_required_and_matches_sup_config(self):
        missing = control_document()
        del missing["brain_profile"]
        with self.assertRaisesRegex(WorkflowPlanError, "brain_profile.*required"):
            load_document(missing)

        expected_profiles = {
            "scripted-cpu": "scripted-v1",
            "mchp-cpu": "mchp-v1",
            "browseruse-gpu": "browseruse-v1",
            "smolagents-gpu": "smolagents-v1",
        }
        for sup_config, expected_profile in expected_profiles.items():
            with self.subTest(sup_config=sup_config):
                document = control_document(sup_config)
                self.assertEqual(document["brain_profile"], expected_profile)
                document["brain_profile"] = next(
                    value for value in expected_profiles.values()
                    if value != expected_profile
                )
                with self.assertRaisesRegex(
                    WorkflowPlanError, "Brain profile mismatch"
                ):
                    load_document(document, sup_config)

    def test_per_entry_brain_structure_is_rejected_without_compatibility(self):
        document = control_document("browseruse-gpu")
        entry = document["schedule"][0]["sequence"][0]
        instruction = entry.pop("instruction")
        entry["brain"] = {
            "profile": document["brain_profile"],
            "instruction": instruction,
        }
        with self.assertRaisesRegex(WorkflowPlanError, "brain.*unexpected"):
            load_document(document, "browseruse-gpu")

    def test_llm_instructions_are_required_and_exact(self):
        for sup_config in ("browseruse-gpu", "smolagents-gpu"):
            with self.subTest(sup_config=sup_config, mutation="missing"):
                missing = control_document(sup_config)
                del missing["schedule"][0]["sequence"][0]["instruction"]
                with self.assertRaisesRegex(
                    WorkflowPlanError, "requires an instruction"
                ):
                    load_document(missing, sup_config)

            with self.subTest(sup_config=sup_config, mutation="changed"):
                changed = control_document(sup_config)
                changed["schedule"][0]["sequence"][0]["instruction"] = (
                    "Changed instruction."
                )
                with self.assertRaisesRegex(
                    WorkflowPlanError, "wrong instruction"
                ):
                    load_document(changed, sup_config)

    def test_non_llm_instructions_are_forbidden(self):
        for sup_config in ("scripted-cpu", "mchp-cpu"):
            with self.subTest(sup_config=sup_config):
                document = control_document(sup_config)
                document["schedule"][0]["sequence"][0]["instruction"] = (
                    "Unexpected instruction."
                )
                with self.assertRaisesRegex(
                    WorkflowPlanError, "forbids an instruction"
                ):
                    load_document(document, sup_config)

    def test_superseded_plan_fields_profiles_and_workflows_are_rejected(self):
        mutations = []
        old_field = control_document()
        old_field["target_profile"] = old_field.pop("resource_profile")
        mutations.append(old_field)
        old_resource_profile = control_document()
        old_resource_profile["resource_profile"] = "control-default"
        mutations.append(old_resource_profile)
        repeated_brain = control_document()
        repeated_brain["schedule"][0]["sequence"][0]["brain"] = {
            "profile": "scripted-v1"
        }
        mutations.append(repeated_brain)
        old_workflow = control_document()
        old_workflow["schedule"][0]["sequence"][0]["workflow"] = "WhoisLookup"
        mutations.append(old_workflow)
        for document in mutations:
            with self.subTest(document=document):
                with self.assertRaises(WorkflowPlanError):
                    load_document(document, "scripted-cpu")

    def test_four_current_controls_load_with_expanded_catalog(self):
        self.assertEqual(set(CONFIGURATIONS), EXPECTED_CONFIGS)
        plans = {
            key: load_workflow_plan(CURRENT_CONTROL_ROOT / PLAN_FILENAMES[key], key)
            for key in EXPECTED_CONFIGS
        }
        expected_workflows = {
            "WebResearch",
            "VideoViewing",
            "DocumentCreation",
            "FileDownload",
            "FileSyncUpload",
            "NetworkShareAccess",
        }
        for key, plan in plans.items():
            self.assertEqual(plan.sup_config, key)
            self.assertEqual(str(plan.timezone), "America/New_York")
            self.assertEqual(plan.resource_profile, "controls-v2")
            self.assertEqual(plan.max_parallel, 10)
            self.assertEqual(len(plan.windows), 24)
            entries = [entry for window in plan.windows for entry in window.sequence]
            self.assertEqual(
                Counter(entry.workflow for entry in entries),
                Counter({workflow: 240 for workflow in expected_workflows}),
            )
            self.assertEqual(len(entries), 1440)
            self.assertEqual(plan.brain_profile_id, PLAN_FILENAMES[key][:-5])
            with self.assertRaises(TypeError):
                entries[0].resource["kind"] = "changed"

        for key in ("browseruse-gpu", "smolagents-gpu"):
            profile = plans[key].brain_profile
            self.assertEqual(profile["model"]["key"], "gemma")
            self.assertEqual(profile["model"]["ollama"], "gemma4:26b")
            self.assertIsNone(profile["system_guidance"])
            self.assertEqual(profile["max_steps"], 10)
        self.assertIsNone(plans["mchp-cpu"].brain_profile["model"])

    def test_rejects_legacy_ids_names_profiles_resources_and_contributions(self):
        mutations = []
        legacy_id = control_document()
        legacy_id["sup_config"] = "M1"
        mutations.append((legacy_id, "M1"))
        old_workflow = control_document()
        old_workflow["schedule"][0]["sequence"][0]["workflow"] = "BrowseWeb"
        mutations.append((old_workflow, "scripted-cpu"))
        profile = control_document()
        profile["brain_profile"] = "mchp-v1"
        mutations.append((profile, "scripted-cpu"))
        resource = control_document()
        resource["schedule"][0]["sequence"][0]["resource_id"] = "unknown"
        mutations.append((resource, "scripted-cpu"))
        for document, expected in mutations:
            with self.subTest(expected=expected, value=document["schedule"][0]["sequence"][0]["workflow"]):
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "behavior.json"
                    path.write_text(json.dumps(document))
                    with self.assertRaises(WorkflowPlanError):
                        load_workflow_plan(path, expected)

    def test_behavior_is_read_once_and_invalid_plan_has_no_fallback(self):
        path = CONTROL_ROOT / PLAN_FILENAMES["scripted-cpu"]
        original = Path.read_bytes
        count = 0

        def counted(instance):
            nonlocal count
            if instance == path:
                count += 1
            return original(instance)

        with patch.object(Path, "read_bytes", counted):
            load_workflow_plan(path, "scripted-cpu")
        self.assertEqual(count, 1)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "behavior.json").write_text("{not-json")
            (root / "previous.json").write_text(json.dumps(control_document()))
            with self.assertRaisesRegex(WorkflowPlanError, "invalid JSON"):
                load_workflow_plan(root / "behavior.json", "scripted-cpu")

    def test_registration_has_no_aliases(self):
        self.assertEqual(set(CONFIGURATIONS), EXPECTED_CONFIGS)
        for key in EXPECTED_CONFIGS:
            self.assertTrue(is_workflow_configuration(key))
        for legacy in ("M1", "B0.gemma", "S0.gemma"):
            self.assertFalse(is_workflow_configuration(legacy))

    def test_rejects_requested_parallelism_above_installed_capability(self):
        document = control_document()
        document["max_parallel"] = 2
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "resource-profiles").mkdir()
            schema = json.loads(
                (CONTRACT_ROOT / "phase-workflow-plan-v1.schema.json").read_text()
            )
            capabilities = json.loads(
                (CONTRACT_ROOT / "capabilities-v1.json").read_text()
            )
            capabilities["max_parallel_workflows"] = 1
            target = json.loads(
                (CONTRACT_ROOT / "resource-profiles/controls-v2.json").read_text()
            )
            (root / "phase-workflow-plan-v1.schema.json").write_text(
                json.dumps(schema)
            )
            (root / "capabilities-v1.json").write_text(json.dumps(capabilities))
            (root / "resource-profiles/controls-v2.json").write_text(
                json.dumps(target)
            )
            behavior = root / "behavior.json"
            behavior.write_text(json.dumps(document))
            with self.assertRaisesRegex(
                WorkflowPlanError, "max_parallel exceeds installed capability"
            ):
                load_workflow_plan(
                    behavior, "scripted-cpu", contract_root=root
                )


class ExecutorTests(unittest.TestCase):
    zone = ZoneInfo("America/New_York")

    def make_executor(self, document, startup_local):
        plan = load_document(document)
        clock = FakeClock(startup_local.astimezone(timezone.utc))
        starter = RecordingStarter()
        events = []
        registry = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp/test-workspace"))
        executor = DailyExecutor(
            plan, registry, events.append, clock=clock, starter=starter
        )
        return executor, clock, starter, events

    def small_plan(self, window, offsets, max_parallel=1):
        document = control_document()
        templates = document["schedule"][0]["sequence"]
        document["max_parallel"] = max_parallel
        document["schedule"] = [{
            "window_local": list(window),
            "sequence": [
                {**copy.deepcopy(templates[index % 3]), "offset_minutes": offset}
                for index, offset in enumerate(offsets)
            ],
        }]
        return document

    def test_startup_past_due_is_missed_without_catchup(self):
        document = self.small_plan((540, 600), (0, 30))
        executor, clock, starter, events = self.make_executor(
            document, datetime(2026, 8, 19, 9, 15, tzinfo=self.zone)
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "startup_past_due")
        executor.tick()
        self.assertEqual(starter.starts, [])
        clock.value = datetime(2026, 8, 19, 9, 30, tzinfo=self.zone).astimezone(timezone.utc)
        executor.tick()
        self.assertEqual(len(starter.starts), 1)
        self.assertEqual(starter.starts[0][0].workflow, "VideoViewing")

    def test_spring_forward_miss_and_fall_back_first_occurrence_only(self):
        spring = self.small_plan((60, 240), (90, 120))
        executor, clock, starter, events = self.make_executor(
            spring, datetime(2026, 3, 8, 0, 0, tzinfo=self.zone)
        )
        self.assertEqual(
            [event["reason"] for event in events], ["dst_nonexistent_time"]
        )
        clock.value = datetime(2026, 3, 8, 3, 0, tzinfo=self.zone).astimezone(timezone.utc)
        executor.tick()
        self.assertEqual(len(starter.starts), 1)

        fall = self.small_plan((60, 180), (30,))
        executor, clock, starter, events = self.make_executor(
            fall, datetime(2026, 11, 1, 0, 0, tzinfo=self.zone)
        )
        clock.value = datetime(2026, 11, 1, 1, 30, tzinfo=self.zone, fold=0).astimezone(timezone.utc)
        executor.tick()
        self.assertEqual(len(starter.starts), 1)
        starter.starts[0][2].complete()
        executor.tick()
        clock.value = datetime(2026, 11, 1, 1, 30, tzinfo=self.zone, fold=1).astimezone(timezone.utc)
        executor.tick()
        self.assertEqual(len(starter.starts), 1)

    def test_fifo_json_ties_capacity_waiting_close_and_natural_drain(self):
        document = self.small_plan((540, 545), (0, 0, 1), max_parallel=2)
        executor, clock, starter, events = self.make_executor(
            document, datetime(2026, 8, 19, 9, 0, tzinfo=self.zone)
        )
        executor.tick()
        self.assertEqual(
            [start[0].workflow for start in starter.starts],
            ["WebResearch", "VideoViewing"],
        )
        clock.value = datetime(2026, 8, 19, 9, 1, tzinfo=self.zone).astimezone(timezone.utc)
        executor.tick()
        self.assertEqual(len(starter.starts), 2)
        starter.starts[0][2].complete()
        executor.tick()
        self.assertEqual(starter.starts[2][0].workflow, "DocumentCreation")

        serial = self.small_plan((540, 542), (0, 1), max_parallel=1)
        executor, clock, starter, events = self.make_executor(
            serial, datetime(2026, 8, 19, 9, 0, tzinfo=self.zone)
        )
        executor.tick()
        clock.value = datetime(2026, 8, 19, 9, 1, tzinfo=self.zone).astimezone(timezone.utc)
        executor.tick()
        clock.value = datetime(2026, 8, 19, 9, 2, tzinfo=self.zone).astimezone(timezone.utc)
        executor.tick()
        self.assertEqual(events[-1]["reason"], "window_closed_while_waiting")
        clock.value = datetime(2026, 8, 19, 9, 3, tzinfo=self.zone).astimezone(timezone.utc)
        starter.starts[0][2].complete(WorkflowResult(True, "/tmp/artifact.odt"))
        executor.tick()
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual(events[-1]["artifact"], "/tmp/artifact.odt")

    def test_failure_and_terminal_fields_are_exact(self):
        document = self.small_plan((540, 600), (0,))
        executor, clock, starter, events = self.make_executor(
            document, datetime(2026, 8, 19, 9, 0, tzinfo=self.zone)
        )
        executor.tick()
        starter.starts[0][2].complete(WorkflowResult(completed=False))
        clock.value = datetime(2026, 8, 19, 9, 1, tzinfo=self.zone).astimezone(timezone.utc)
        executor.tick()
        event = events[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["reason"], "workflow_failed")
        self.assertEqual(
            set(event),
            {
                "window_index", "sequence_index", "workflow", "resource_profile",
                "brain_profile", "resource_id", "scheduled_local", "scheduled_utc",
                "actual_start", "actual_end", "status", "reason",
            },
        )

    def test_worker_exception_traceback_is_logged_without_changing_terminal_schema(self):
        document = self.small_plan((540, 600), (0,))
        executor, clock, starter, events = self.make_executor(
            document, datetime(2026, 8, 19, 9, 0, tzinfo=self.zone)
        )
        executor.tick()
        starter.starts[0][2].complete(error=RuntimeError("driver exploded"))
        clock.value = datetime(
            2026, 8, 19, 9, 1, tzinfo=self.zone
        ).astimezone(timezone.utc)
        with self.assertLogs("phase_workflow.executor", level="ERROR") as captured:
            executor.tick()
        self.assertIn("driver exploded", "".join(captured.output))
        self.assertIsNotNone(captured.records[0].exc_info)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["reason"], "workflow_failed")

    def test_schedule_recurs_on_the_next_local_day(self):
        document = self.small_plan((540, 600), (0,))
        executor, clock, starter, events = self.make_executor(
            document, datetime(2026, 8, 19, 9, 0, tzinfo=self.zone)
        )
        executor.tick()
        starter.starts[0][2].complete()
        executor.tick()
        clock.value = datetime(
            2026, 8, 20, 9, 0, tzinfo=self.zone
        ).astimezone(timezone.utc)
        executor.tick()
        self.assertEqual(len(starter.starts), 2)
        self.assertEqual(
            [start[1] for start in starter.starts], ["2026-08-19", "2026-08-20"]
        )

    def test_completed_llm_terminal_fields_include_exact_instruction(self):
        document = control_document("browseruse-gpu")
        document["schedule"] = [{
            "window_local": [540, 600],
            "sequence": [copy.deepcopy(document["schedule"][0]["sequence"][0])],
        }]
        executor, clock, starter, events = self.make_executor(
            document, datetime(2026, 8, 19, 9, 0, tzinfo=self.zone)
        )
        executor.tick()
        starter.starts[0][2].complete()
        clock.value = datetime(
            2026, 8, 19, 9, 1, tzinfo=self.zone
        ).astimezone(timezone.utc)
        executor.tick()
        event = events[0]
        self.assertEqual(event["status"], "completed")
        self.assertNotIn("reason", event)
        self.assertEqual(
            event["resolved_instruction"],
            document["schedule"][0]["sequence"][0]["instruction"],
        )
        self.assertEqual(
            set(event),
            {
                "window_index", "sequence_index", "workflow", "resource_profile",
                "brain_profile", "resource_id", "resolved_instruction", "scheduled_local",
                "scheduled_utc", "actual_start", "actual_end", "status",
            },
        )


class RegistryAndBrainTests(unittest.TestCase):
    def test_exact_canonical_registry_and_same_resolved_task(self):
        self.assertEqual(
            CANONICAL_HANDLERS,
            {
                "WebResearch", "VideoViewing", "FileDownload",
                "DocumentCreation", "FileSyncUpload", "NetworkShareAccess",
            },
        )
        resolved = []
        for key in ("scripted-cpu", "mchp-cpu", "browseruse-gpu", "smolagents-gpu"):
            plan = load_control_plan(key)
            brain = RecordingBrain()
            registry = WorkflowRegistry(plan, brain, Path("/tmp/workspace"))
            self.assertEqual(registry.workflows, CANONICAL_HANDLERS)
            resolved.append(registry.resolve(plan.windows[0].sequence[0]))
        self.assertTrue(
            all(task.resource_id == EXPECTED_RESOURCES[0] for task in resolved)
        )
        self.assertTrue(all(dict(task.resource) == dict(resolved[0].resource) for task in resolved))
        self.assertIsNone(resolved[0].instruction)
        self.assertIsNone(resolved[1].instruction)
        self.assertEqual(resolved[2].instruction, resolved[3].instruction)

    def test_llm_brain_receives_exact_instruction_and_resource(self):
        plan = load_control_plan("browseruse-gpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[0]
        )
        received = []
        brain = FrameworkBrain(
            lambda value, workspace: WorkflowResult(
                completed=received.append((value, workspace)) is None
            )
        )
        result = brain.execute(task, Path("/tmp"))
        self.assertTrue(result.completed)
        self.assertIs(received[0][0], task)
        self.assertEqual(received[0][1], Path("/tmp"))
        self.assertEqual(
            received[0][0].instruction,
            plan.windows[0].sequence[0].instruction,
        )
        self.assertEqual(received[0][0].resource_id, EXPECTED_RESOURCES[0])

    def test_llm_video_is_dispatched_through_the_framework_runner(self):
        plan = load_control_plan("browseruse-gpu")
        entry = plan.windows[0].sequence[1]
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(entry)
        framework_tasks = []
        brain = FrameworkBrain(
            lambda value, workspace: WorkflowResult(
                completed=framework_tasks.append((value, workspace)) is None
            )
        )
        result = brain.execute(task, Path("/tmp"))
        self.assertTrue(result.completed)
        self.assertEqual(framework_tasks, [(task, Path("/tmp"))])
        self.assertEqual(task.resource["play_seconds"], 300)

    def test_scripted_video_uses_exact_resource_duration_and_cleans_up(self):
        class Driver:
            def __init__(self):
                self.urls = []
                self.scripts = []
                self.closed = False

            def get(self, url):
                self.urls.append(url)

            def find_element(self, by, value):
                self.find = (by, value)
                return object()

            def execute_script(self, script, video):
                self.scripts.append((script, video))

            def quit(self):
                self.closed = True

        plan = load_control_plan("scripted-cpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[1]
        )
        driver = Driver()
        sleeps = []
        result = SeleniumResourceWorkflows(
            lambda: driver, sleeper=sleeps.append
        ).video_viewing(task)
        self.assertTrue(result.completed)
        self.assertEqual(
            driver.urls,
            ["https://www.youtube.com/watch?v=" + task.resource["video_id"]],
        )
        self.assertEqual(sleeps, [300])
        self.assertEqual(driver.find, ("tag name", "video"))
        self.assertTrue(driver.closed)

    def test_scripted_chromium_does_not_import_browseruse(self):
        from phase_workflow import brains

        class Options:
            def __init__(self):
                self.arguments = []

            def add_argument(self, value):
                self.arguments.append(value)

        created = []
        webdriver = SimpleNamespace(
            ChromeOptions=Options,
            Chrome=lambda options: created.append(options) or object(),
        )
        selenium = SimpleNamespace(webdriver=webdriver)
        original_import = __import__

        def isolated_import(name, *args, **kwargs):
            if name.startswith(("browser_use", "brains.browseruse")):
                raise AssertionError(f"unexpected BrowserUse import: {name}")
            return original_import(name, *args, **kwargs)

        with patch.dict(sys.modules, {"selenium": selenium}), patch(
            "builtins.__import__", isolated_import
        ):
            brains._chromium_driver()
        self.assertEqual(len(created), 1)
        self.assertIn("--headless=new", created[0].arguments)

    def test_document_writer_uses_exact_filename_and_supplied_content(self):
        plan = load_control_plan("scripted-cpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[2]
        )
        with tempfile.TemporaryDirectory() as td:
            result = OpenDocumentWriter().create(task, Path(td))
            artifact = Path(result.artifact)
            self.assertEqual(artifact.name, task.resource["filename"])
            with zipfile.ZipFile(artifact) as archive:
                content = archive.read("content.xml").decode()
        self.assertTrue(result.completed)
        self.assertIn(task.resource["title"], content)
        for values in task.resource["sections"].values():
            for value in values:
                self.assertIn(value, content)

    def test_build_brain_selects_fixed_video_enforcer_per_gpu_brain(self):
        for config_key, runner_name, player_name in (
            ("browseruse-gpu", "browseruse_runner", "play_video_with_chromium"),
            ("smolagents-gpu", "smolagents_runner", "play_video_realtime"),
        ):
            plan = load_control_plan(config_key)
            task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
                plan.windows[0].sequence[1]
            )
            with patch(
                f"phase_workflow.brains.{runner_name}",
                return_value=WorkflowResult(completed=True),
            ) as runner, patch(f"phase_workflow.brains.{player_name}") as player:
                brain = build_brain(plan.brain, plan.brain_profile)
                result = brain.execute(task, Path("/tmp"))
            self.assertTrue(result.completed)
            expected_kwargs = dict(
                video_player=player,
                downloader=stream_https_download,
                syncer=ANY,
                share=ANY,
            )
            if config_key == "browseruse-gpu":
                expected_kwargs["async_executor"] = ANY
            runner.assert_called_once_with(
                task,
                Path("/tmp"),
                plan.brain_profile,
                None,
                **expected_kwargs,
            )
            player.assert_not_called()

    def test_smol_video_consumes_one_stream_at_real_time_for_300_seconds(self):
        plan = load_control_plan("smolagents-gpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[1]
        )
        resolutions = []
        process_calls = []

        def resolve(video_id):
            resolutions.append(video_id)
            return "https://media.example/assigned.mp4"

        def run(command, **kwargs):
            process_calls.append((command, kwargs))

        self.assertTrue(
            play_video_realtime(task, media_resolver=resolve, process_runner=run)
        )
        self.assertEqual(resolutions, [task.resource["video_id"]])
        self.assertEqual(len(process_calls), 1)
        command, kwargs = process_calls[0]
        self.assertEqual(command.count("https://media.example/assigned.mp4"), 1)
        self.assertIn("-re", command)
        self.assertEqual(command[command.index("-t") + 1], "300")
        self.assertEqual(kwargs, {"check": True, "timeout": 360})

    def test_mchp_document_dispatch_uses_assigned_libreoffice_resource(self):
        class MCHPWorkflow:
            def __init__(self, kind):
                self.kind = kind
                self.calls = []
                self.cleaned = False

            def create_assigned(self, resource, workspace, logger=None):
                self.calls.append((resource, workspace, logger))
                return workspace / resource["filename"]

            def cleanup(self):
                self.cleaned = True

        document = feedback_document("mchp-cpu")
        spreadsheet = copy.deepcopy(document["schedule"][0]["sequence"][2])
        spreadsheet["offset_minutes"] = 45
        spreadsheet["resource_id"] = "spreadsheet_expense_tracker"
        document["schedule"][0]["sequence"].append(spreadsheet)
        plan = load_document(document, "mchp-cpu")
        entries = [plan.windows[0].sequence[2], plan.windows[0].sequence[3]]
        writer = MCHPWorkflow("document")
        calc = MCHPWorkflow("spreadsheet")
        documents = MCHPDocumentWorkflows(
            writer_factory=lambda: writer,
            calc_factory=lambda: calc,
            logger="logger",
        )
        registry = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp"))
        with patch("phase_workflow.workflows.validate_open_document") as validate:
            for entry in entries:
                task = registry.resolve(entry)
                result = documents.create(task, Path("/tmp/day"))
                self.assertEqual(Path(result.artifact).name, task.resource["filename"])
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(writer.calls[0][0], entries[0].resource)
        self.assertEqual(calc.calls[0][0], entries[1].resource)
        self.assertTrue(writer.cleaned)
        self.assertTrue(calc.cleaned)

    def test_mchp_brain_routes_document_creation_to_libreoffice_adapter(self):
        class Documents:
            def __init__(self):
                self.calls = []

            def create(self, task, workspace):
                self.calls.append((task, workspace))
                return WorkflowResult(
                    completed=True,
                    artifact=str(workspace / task.resource["filename"]),
                )

        plan = load_control_plan("mchp-cpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[2]
        )
        documents = Documents()
        with patch(
            "phase_workflow.brains.MCHPDocumentWorkflows",
            return_value=documents,
        ):
            brain = build_brain(plan.brain, plan.brain_profile)
        result = brain.execute(task, Path("/tmp/day"))
        self.assertTrue(result.completed)
        self.assertEqual(documents.calls, [(task, Path("/tmp/day"))])
        self.assertEqual(Path(result.artifact).name, task.resource["filename"])


class TransferWorkflowTests(unittest.TestCase):
    @staticmethod
    def task(sup_config, workflow, occurrence_id="w0-s0"):
        plan = load_document(six_workflow_document(sup_config), sup_config)
        entry = next(
            item for item in plan.windows[0].sequence
            if item.workflow == workflow
        )
        return WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            entry, occurrence_id=occurrence_id
        )

    def test_stream_download_is_one_exact_request_and_deletes_partial_file(self):
        class Response:
            def __init__(self, chunks):
                self.chunks = chunks
                self.closed = False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                return iter(self.chunks)

            def close(self):
                self.closed = True

        class Requests:
            def __init__(self, response):
                self.response = response
                self.calls = []

            def get(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return self.response

        task = self.task("scripted-cpu", "FileDownload")
        expected = task.resource["expected_bytes"]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            response = Response([b"x" * expected])
            requests = Requests(response)
            artifact = stream_https_download(task, workspace, requests)
            self.assertEqual(artifact.stat().st_size, expected)
            self.assertEqual(len(requests.calls), 1)
            self.assertEqual(requests.calls[0][0], (task.resource["url"],))
            self.assertTrue(response.closed)

            partial = Response([b"x"])
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                stream_https_download(task, workspace, Requests(partial))
            self.assertFalse(any(path.stat().st_size == 1 for path in workspace.iterdir()))

    def test_download_action_is_immutable_exact_size_and_exactly_once(self):
        task = self.task("browseruse-gpu", "FileDownload")
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)

            def download(received, destination):
                calls.append((received, destination))
                artifact = destination / "assigned.bin"
                artifact.write_bytes(b"x" * received.resource["expected_bytes"])
                return artifact

            action = AssignedFileDownload(task, workspace, download)
            self.assertIn("completed", action.invoke())
            self.assertTrue(action.result.completed)
            self.assertIn("only once", action.invoke())
            self.assertFalse(action.result.completed)
            self.assertEqual(calls, [(task, workspace)])

    def test_scripted_and_mchp_direct_download_dispatch_exactly_once(self):
        class Web:
            web_research = None
            video_viewing = None

        for sup_config in ("scripted-cpu", "mchp-cpu"):
            with self.subTest(sup_config=sup_config), tempfile.TemporaryDirectory() as temporary:
                task = self.task(sup_config, "FileDownload")
                workspace = Path(temporary)
                calls = []

                def downloader(received, destination):
                    calls.append((received, destination))
                    artifact = destination / "assigned.bin"
                    with artifact.open("wb") as handle:
                        handle.truncate(received.resource["expected_bytes"])
                    return artifact

                result = ResourceBrain(Web(), downloader=downloader).execute(
                    task, workspace
                )
                self.assertTrue(result.completed)
                self.assertEqual(calls, [(task, workspace)])
                self.assertEqual(Path(result.artifact).stat().st_size, 1048576)

    def test_mchp_firefox_download_uses_nonblocking_browser_action(self):
        task = self.task("mchp-cpu", "FileDownload")
        owners = []
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)

            class Driver:
                def __init__(self, download_dir):
                    self.scripts = []
                    self.download_dir = download_dir

                def execute_script(self, script, url):
                    self.scripts.append((script, url))
                    with (self.download_dir / "1Mb.dat").open("wb") as handle:
                        handle.truncate(task.resource["expected_bytes"])
                    timeout = type("TimeoutException", (Exception,), {})
                    raise timeout("page navigation did not complete")

            class Owner:
                def __init__(self, download_dir):
                    self.driver = Driver(download_dir)
                    self.cleaned = False

                def cleanup(self):
                    self.cleaned = True

            def factory(path):
                self.assertEqual(path.parent, workspace / ".mchp-downloads")
                self.assertEqual(path.name, task.occurrence_id)
                owner = Owner(path)
                owners.append(owner)
                return owner

            artifact = firefox_download(
                task, workspace, factory, sleeper=lambda _delay: None
            )
            self.assertEqual(artifact.stat().st_size, task.resource["expected_bytes"])
            self.assertEqual(len(owners[0].driver.scripts), 1)
            self.assertEqual(
                owners[0].driver.scripts[0][1], task.resource["url"]
            )
            self.assertIn("document.createElement('a')", owners[0].driver.scripts[0][0])
            self.assertTrue(owners[0].cleaned)

    def test_mchp_firefox_download_rejects_partial_wrong_size_and_timeout(self):
        task = self.task("mchp-cpu", "FileDownload")
        for name, created in (
            ("partial", {"1Mb.dat.part": 10}),
            ("wrong size", {"1Mb.dat": task.resource["expected_bytes"] - 1}),
            ("missing", {}),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                cleaned = []
                clock = [0.0]

                class Driver:
                    def execute_script(self, _script, url):
                        self.url = url
                        for filename, size in created.items():
                            with (self.download_dir / filename).open("wb") as handle:
                                handle.truncate(size)

                class Owner:
                    def __init__(self, download_dir):
                        self.driver = Driver()
                        self.driver.download_dir = download_dir

                    def cleanup(self):
                        cleaned.append(True)

                def sleep(delay):
                    clock[0] += delay

                with self.assertRaisesRegex(RuntimeError, "did not complete"):
                    firefox_download(
                        task,
                        workspace,
                        lambda path: Owner(path),
                        sleeper=sleep,
                        monotonic=lambda: clock[0],
                        timeout_seconds=0.5,
                    )
                self.assertEqual(cleaned, [True])

    def test_mchp_firefox_download_accepts_real_duplicate_names_in_owned_directory(self):
        task = self.task("mchp-cpu", "FileDownload")
        for filename in ("1Mb.dat", "1Mb (1).dat", "1Mb(1).dat"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)

                class Driver:
                    def __init__(self, destination):
                        self.destination = destination

                    def execute_script(self, _script, _url):
                        with (self.destination / filename).open("wb") as handle:
                            handle.truncate(task.resource["expected_bytes"])

                class Owner:
                    def __init__(self, destination):
                        self.driver = Driver(destination)
                        self.cleaned = False

                    def cleanup(self):
                        self.cleaned = True

                owners = []
                def factory(destination):
                    owner = Owner(destination)
                    owners.append(owner)
                    return owner

                artifact = firefox_download(task, workspace, factory)
                self.assertEqual(artifact.name, filename)
                self.assertTrue(owners[0].cleaned)

    def test_simultaneous_mchp_downloads_have_strict_occurrence_ownership(self):
        same_a = self.task("mchp-cpu", "FileDownload", "w1-s0")
        same_b = self.task("mchp-cpu", "FileDownload", "w1-s1")
        other_document = six_workflow_document("mchp-cpu")
        other_entry = next(
            item for item in other_document["schedule"][0]["sequence"]
            if item["workflow"] == "FileDownload"
        )
        other_entry["resource_id"] = "download_sasag_10m"
        other_plan = load_document(other_document, "mchp-cpu")
        other = WorkflowRegistry(
            other_plan, RecordingBrain(), Path("/tmp")
        ).resolve(
            next(item for item in other_plan.windows[0].sequence if item.workflow == "FileDownload"),
            occurrence_id="w1-s2",
        )
        barrier = threading.Barrier(3)
        seen = []

        class Owner:
            def __init__(self, task, destination):
                self.task = task
                self.destination = destination
                self.driver = self
                self.cleaned = False

            def execute_script(self, _script, _url):
                barrier.wait(timeout=2)
                filename = Path(self.task.resource["url"]).name
                with (self.destination / filename).open("wb") as handle:
                    handle.truncate(self.task.resource["expected_bytes"])

            def cleanup(self):
                self.cleaned = True

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            def run(task):
                owner = Owner(task, workspace / ".mchp-downloads" / task.occurrence_id)
                seen.append(owner)
                return firefox_download(task, workspace, lambda _path: owner)
            with ThreadPoolExecutor(max_workers=3) as pool:
                artifacts = list(pool.map(run, (same_a, same_b, other)))
        self.assertEqual(len({artifact.parent for artifact in artifacts}), 3)
        self.assertTrue(all(owner.cleaned for owner in seen))

    def test_ambiguous_owned_download_fails_with_detailed_evidence(self):
        task = self.task("mchp-cpu", "FileDownload")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            class Owner:
                def __init__(self, destination):
                    self.driver = self
                    self.destination = destination
                    self.cleaned = False
                def execute_script(self, _script, _url):
                    for filename in ("1Mb.dat", "1Mb(1).dat"):
                        with (self.destination / filename).open("wb") as handle:
                            handle.truncate(task.resource["expected_bytes"])
                def cleanup(self):
                    self.cleaned = True
            owners = []
            def factory(destination):
                owners.append(Owner(destination))
                return owners[-1]
            with self.assertRaisesRegex(
                RuntimeError,
                r"resource_id=download_ovh_1m.*expected_bytes=1048576.*created_files=.*1Mb\(1\)\.dat.*observed_sizes=.*elapsed_seconds=",
            ):
                firefox_download(task, workspace, factory)
            self.assertTrue(owners[0].cleaned)
            self.assertFalse((workspace / ".mchp-downloads" / task.occurrence_id).exists())

    def test_sync_uses_oldest_document_exact_post_and_independent_state(self):
        class Response:
            def raise_for_status(self):
                return None

            def close(self):
                pass

        class Requests:
            def __init__(self):
                self.calls = []

            def post(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return Response()

        store = DailyDocumentStore()
        requests = Requests()
        task = self.task("scripted-cpu", "FileSyncUpload")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "2026-08-24"
            workspace.mkdir()
            oldest = workspace / "oldest.odt"
            newer = workspace / "newer.ods"
            oldest.write_bytes(b"oldest")
            newer.write_bytes(b"newer")
            store.register(workspace.name, oldest)
            store.register(workspace.name, newer)
            result = HttpsDocumentSync(store, requests).execute(task, workspace)
            self.assertEqual(result.artifact, str(oldest))
            self.assertEqual(requests.calls, [((task.resource["url"],), {
                "params": {"bytes": len(b"oldest")},
                "data": b"oldest",
                "timeout": (20, 360),
            })])
            self.assertEqual(store.state(workspace.name, oldest), (True, False, set()))

    def test_sync_fallback_and_failure_release_without_marking(self):
        class Requests:
            @staticmethod
            def post(*_args, **_kwargs):
                raise RuntimeError("upload failed")

        store = DailyDocumentStore()
        task = self.task("mchp-cpu", "FileSyncUpload", "w2-s4")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "2026-08-24"
            workspace.mkdir()
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                HttpsDocumentSync(store, Requests()).execute(task, workspace)
            fallback = workspace / "private-lorem-w2-s4.txt"
            self.assertTrue(fallback.is_file())
            self.assertEqual(store.state(workspace.name, fallback), (False, False, set()))
            self.assertEqual(fallback.stat().st_mode & 0o777, 0o600)

    def test_cross_channel_reservations_do_not_select_the_same_document(self):
        store = DailyDocumentStore()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "2026-08-24"
            workspace.mkdir()
            first = workspace / "first.odt"
            second = workspace / "second.ods"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            store.register(workspace.name, first)
            store.register(workspace.name, second)
            share = store.reserve(workspace.name, workspace, "share", "w0-s0")
            https = store.reserve(workspace.name, workspace, "https", "w0-s1")
            self.assertEqual((share.path, https.path), (first, second))

    def test_share_is_kerberos_required_bidirectional_and_marks_after_verify(self):
        store = DailyDocumentStore()
        task = self.task("scripted-cpu", "NetworkShareAccess", "w1-s5")
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "2026-08-24"
            workspace.mkdir()
            upload = workspace / "created.odt"
            upload.write_bytes(b"payload")
            store.register(workspace.name, upload)

            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                command = argv[-1] if "smbclient" in argv else ""
                if command.startswith('get '):
                    _, remote, local = shlex.split(command)
                    Path(local).write_bytes(
                        upload.read_bytes()
                        if remote.startswith("Incoming/")
                        else b"seed"
                    )
                return SimpleNamespace(stdout="ok\n")

            result = KerberosShareAccess(
                store, runner=run, keytab="/fleet/keytab", ccache="/run/cache"
            ).execute(task, workspace)
            self.assertTrue(result.completed)
            self.assertEqual(store.state(workspace.name, upload), (False, True, set()))
            self.assertEqual(calls[0][0], [
                "timeout", "--signal=TERM", "--kill-after=3s", "30s",
                "kinit", "-c", "/run/cache", "-kt", "/fleet/keytab",
                "ruse-share@RUSE.TEST",
            ])
            smb_calls = [call for call in calls if "smbclient" in call[0]]
            self.assertTrue(all(
                call[0][:4] == [
                    "timeout", "--signal=TERM", "--kill-after=3s", "30s"
                ]
                for call in smb_calls
            ))
            self.assertTrue(all(
                "--use-kerberos=required" in call[0] and "--no-pass" in call[0]
                for call in smb_calls
            ))
            commands = [call[0][-1] for call in smb_calls]
            self.assertIn('ls "Team"', commands)
            self.assertTrue(any('get "Team/meeting-notes.odt"' in value for value in commands))
            self.assertTrue(any(
                "Incoming/scripted-cpu/2026-08-24/w1-s5-created.odt" in value
                for value in commands
            ))
            self.assertTrue(any(value.startswith("del ") for value in commands))
            self.assertFalse(any("allinfo" in value for value in commands))
            self.assertFalse(any(workspace.glob(".*-share-upload-roundtrip")))

    def test_share_failure_releases_and_bounded_transfer_rejects_repeat(self):
        store = DailyDocumentStore()
        task = self.task("smolagents-gpu", "NetworkShareAccess")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "2026-08-24"
            workspace.mkdir()
            local = workspace / "document.odt"
            local.write_bytes(b"payload")
            store.register(workspace.name, local)

            def run(argv, **_kwargs):
                command = argv[-1] if "smbclient" in argv else ""
                if command.startswith('get '):
                    _, remote, destination = shlex.split(command)
                    Path(destination).write_bytes(
                        b"mismatch" if remote.startswith("Incoming/") else b"seed"
                    )
                return SimpleNamespace(stdout="remote verification missing\n")

            calls = []
            original_run = run

            def recording_run(argv, **kwargs):
                calls.append(argv)
                return original_run(argv, **kwargs)

            executor = KerberosShareAccess(store, runner=recording_run)
            action = AssignedBoundedTransfer(task, workspace, executor)
            self.assertIn("round-trip mismatch", action.invoke())
            self.assertFalse(action.result.completed)
            self.assertEqual(store.state(workspace.name, local), (False, False, set()))
            self.assertTrue(any(
                argv[-1].startswith("del ")
                for argv in calls if "smbclient" in argv
            ))
            self.assertFalse(any(workspace.glob(".*-share-upload-roundtrip")))
            self.assertIn("only once", action.invoke())

    @staticmethod
    def browser_api(invocations):
        class History:
            def is_done(self):
                return True

            def is_successful(self):
                return True

        class BrowserSession:
            def __init__(self, **_kwargs):
                pass

        class ActionResult:
            def __init__(self, **values):
                self.values = values

        class Tools:
            def __init__(self):
                self.registry = SimpleNamespace(
                    registry=SimpleNamespace(actions={})
                )

            def exclude_action(self, name):
                self.registry.registry.actions.pop(name, None)

            def action(self, _description, **_kwargs):
                def decorate(function):
                    self.registry.registry.actions[function.__name__] = function
                    return function
                return decorate

        class Agent:
            def __init__(self, **values):
                self.tools = values["tools"]

            async def run(self, max_steps):
                action = next(iter(self.tools.registry.registry.actions.values()))
                for _ in range(invocations):
                    result = action()
                    if inspect.isawaitable(result):
                        await result
                return History()

        return Agent, BrowserSession, Tools, ActionResult

    @staticmethod
    def smol_api(invocations):
        class Tool:
            def __init__(self, *_args, **_kwargs):
                pass

        class VisitWebpageTool(Tool):
            pass

        class LiteLLMModel:
            def __init__(self, **_kwargs):
                pass

        class CodeAgent:
            def __init__(self, **values):
                self.tool = values["tools"][0]

            def run(self, _task):
                for _ in range(invocations):
                    try:
                        self.tool.forward()
                    except RuntimeError:
                        pass
                return "prose is not evidence"

        return CodeAgent, LiteLLMModel, Tool, VisitWebpageTool

    def test_llm_transfer_actions_require_exactly_one_real_invocation(self):
        class Transfer:
            def __init__(self):
                self.calls = []

            def execute(self, task, workspace):
                self.calls.append((task, workspace))
                return WorkflowResult(completed=True, artifact=str(workspace / "doc"))

        for sup_config, runner, api_factory in (
            ("browseruse-gpu", browseruse_runner, self.browser_api),
            ("smolagents-gpu", smolagents_runner, self.smol_api),
        ):
            for workflow in ("FileSyncUpload", "NetworkShareAccess"):
                for invocations, expected in ((0, False), (1, True), (2, False)):
                    with self.subTest(
                        sup_config=sup_config,
                        workflow=workflow,
                        invocations=invocations,
                    ):
                        plan = load_document(
                            six_workflow_document(sup_config), sup_config
                        )
                        task = self.task(sup_config, workflow)
                        transfer = Transfer()
                        kwargs = {
                            "syncer": transfer if workflow == "FileSyncUpload" else None,
                            "share": transfer if workflow == "NetworkShareAccess" else None,
                            "framework_api": api_factory(invocations),
                        }
                        if runner is browseruse_runner:
                            kwargs.update({
                                "llm_factory": lambda model, logger: model,
                                "step_logger": lambda logger, result: None,
                                "chromium_args": [],
                            })
                        with patch("phase_workflow.brains._require_distribution"):
                            result = runner(
                                task, Path("/tmp/day"), plan.brain_profile, **kwargs
                            )
                        self.assertEqual(result.completed, expected)
                        self.assertEqual(len(transfer.calls), min(invocations, 1))

    def test_llm_download_actions_require_exactly_one_exact_size_file(self):
        for sup_config, runner, api_factory in (
            ("browseruse-gpu", browseruse_runner, self.browser_api),
            ("smolagents-gpu", smolagents_runner, self.smol_api),
        ):
            for invocations, expected in ((0, False), (1, True), (2, False)):
                with self.subTest(sup_config=sup_config, invocations=invocations):
                    plan = load_document(six_workflow_document(sup_config), sup_config)
                    task = self.task(sup_config, "FileDownload")
                    calls = []
                    with tempfile.TemporaryDirectory() as temporary:
                        workspace = Path(temporary)

                        def downloader(received, destination):
                            calls.append((received, destination))
                            artifact = destination / "assigned.bin"
                            artifact.write_bytes(
                                b"x" * received.resource["expected_bytes"]
                            )
                            return artifact

                        kwargs = {
                            "downloader": downloader,
                            "framework_api": api_factory(invocations),
                        }
                        if runner is browseruse_runner:
                            kwargs.update({
                                "llm_factory": lambda model, logger: model,
                                "step_logger": lambda logger, result: None,
                                "chromium_args": [],
                            })
                        with patch("phase_workflow.brains._require_distribution"):
                            result = runner(
                                task, workspace, plan.brain_profile, **kwargs
                            )
                    self.assertEqual(result.completed, expected)
                    self.assertEqual(len(calls), min(invocations, 1))

    def test_browseruse_rejects_duplicate_parsed_action_with_missing_result(self):
        plan = load_document(
            six_workflow_document("browseruse-gpu"), "browseruse-gpu"
        )
        task = self.task("browseruse-gpu", "VideoViewing")
        calls = []

        class Action:
            def model_dump(self, **_kwargs):
                return {"play_assigned_video": {}}

        class History:
            def __init__(self, action_result):
                self.history = [SimpleNamespace(
                    model_output=SimpleNamespace(action=[Action(), Action()]),
                    result=[action_result],
                )]

            def is_done(self):
                return True

            def is_successful(self):
                return True

        class BrowserSession:
            def __init__(self, **_kwargs):
                pass

        class ActionResult:
            def __init__(self, **values):
                self.__dict__.update(values)

        class Tools:
            def __init__(self):
                self.registry = SimpleNamespace(
                    registry=SimpleNamespace(actions={})
                )

            def exclude_action(self, name):
                self.registry.registry.actions.pop(name, None)

            def action(self, _description, **_kwargs):
                def decorate(function):
                    self.registry.registry.actions[function.__name__] = function
                    return function
                return decorate

        class Agent:
            def __init__(self, **values):
                self.tools = values["tools"]

            async def run(self, max_steps):
                action = self.tools.registry.registry.actions[
                    "play_assigned_video"
                ]
                return History(await action())

        with patch("phase_workflow.brains._require_distribution"):
            result = browseruse_runner(
                task,
                Path("/tmp/day"),
                plan.brain_profile,
                video_player=lambda received: calls.append(received) or True,
                framework_api=(Agent, BrowserSession, Tools, ActionResult),
                llm_factory=lambda model, logger: model,
                step_logger=lambda logger, history: None,
                chromium_args=[],
            )

        self.assertFalse(result.completed)
        self.assertEqual(calls, [task])

    def test_browseruse_bounded_actions_return_exact_assigned_evidence(self):
        for workflow in (
            "FileDownload",
            "FileSyncUpload",
            "NetworkShareAccess",
        ):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                plan = load_document(
                    six_workflow_document("browseruse-gpu"), "browseruse-gpu"
                )
                task = self.task("browseruse-gpu", workflow)
                workspace = Path(td)
                api, state = LLMVideoRunnerTests.browser_api(True)
                kwargs = {
                    "framework_api": api,
                    "llm_factory": lambda model, logger: model,
                    "step_logger": lambda logger, result: None,
                    "chromium_args": [],
                }
                if workflow == "FileDownload":
                    def downloader(received, destination):
                        artifact = destination / "assigned.bin"
                        artifact.write_bytes(
                            b"x" * received.resource["expected_bytes"]
                        )
                        return artifact

                    kwargs["downloader"] = downloader
                else:
                    class Transfer:
                        def execute(self, _task, destination):
                            artifact = destination / "verified-transfer.bin"
                            artifact.write_bytes(b"verified")
                            return WorkflowResult(
                                completed=True, artifact=str(artifact)
                            )

                    kwargs[
                        "syncer" if workflow == "FileSyncUpload" else "share"
                    ] = Transfer()
                with patch("phase_workflow.brains._require_distribution"):
                    result = browseruse_runner(
                        task, workspace, plan.brain_profile, **kwargs
                    )
                self.assertTrue(result.completed)
                evidence = state["action_result"].extracted_content
                self.assertIn(f"resource_id={task.resource_id}", evidence)
                self.assertIn(f"artifact={result.artifact}", evidence)
                self.assertIn(
                    f"observed_bytes={Path(result.artifact).stat().st_size}",
                    evidence,
                )
                if workflow == "NetworkShareAccess":
                    self.assertIn(
                        f"share=//share.ruse.test/shared/{task.resource['path']}",
                        evidence,
                    )
                else:
                    self.assertIn(
                        f"assigned_url={task.resource['url']}", evidence
                    )
                if workflow == "FileDownload":
                    self.assertIn(
                        f"expected_bytes={task.resource['expected_bytes']}",
                        evidence,
                    )

    def test_terminal_event_uses_ordinary_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            logger = AgentLogger("scripted-cpu", log_dir=td, session_id="plan")
            logger.workflow_plan_terminal({
                "window_index": 0,
                "sequence_index": 0,
                "workflow": "WebResearch",
                "resource_profile": "controls-v2",
                "brain_profile": "scripted-v1",
                "resource_id": "google_climate_change_news",
                "scheduled_local": "2026-08-19T09:00:00",
                "scheduled_utc": "2026-08-19T13:00:00Z",
                "actual_start": "2026-08-19T13:00:00Z",
                "actual_end": "2026-08-19T13:01:00Z",
                "status": "completed",
            })
            logger.close()
            event = json.loads(logger.log_file.read_text().strip())
        self.assertEqual(event["event_type"], "workflow_plan_terminal")
        self.assertEqual(event["workflow"], "WebResearch")
        self.assertEqual(event["details"]["status"], "completed")

    def test_signal_handler_cannot_reenter_jsonl_buffer(self):
        from common.logging import agent_logger as logger_module

        with tempfile.TemporaryDirectory() as td:
            logger = AgentLogger("scripted-cpu", log_dir=td, session_id="signal")
            logger._session_start_time = 1.0
            logger._orig_sigterm = signal.SIG_DFL
            logger._orig_sigint = signal.SIG_DFL
            original_write = os.write
            entered = False

            def interrupting_write(fd, payload):
                nonlocal entered
                if not entered:
                    entered = True
                    logger._signal_handler(signal.SIGTERM, None)
                return original_write(fd, payload)

            with patch.object(
                logger_module.os, "write", side_effect=interrupting_write
            ), patch.object(logger_module.os, "kill"), patch.object(
                logger_module.signal, "signal"
            ), patch.object(logger, "close"):
                logger.info("write interrupted by SIGTERM")
            logger.close()
            events = [
                json.loads(line)
                for line in logger.log_file.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [event["event_type"] for event in events],
            ["session_fail", "session_end", "info"],
        )
        self.assertEqual(events[0]["details"]["error"], "SIGTERM")


class TruthPropagationTests(unittest.TestCase):
    @staticmethod
    def browser_task():
        document = feedback_document("browseruse-gpu")
        document["schedule"][0]["sequence"][0][
            "resource_id"
        ] = "wikipedia_compiler"
        plan = load_document(document, "browseruse-gpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[0]
        )
        return plan, task

    @staticmethod
    def browser_api(history):
        class BrowserSession:
            def __init__(self, **_values):
                pass

        class Tools:
            pass

        class ActionResult:
            pass

        class Agent:
            def __init__(self, **values):
                self.values = values

            async def run(self, max_steps):
                return history

        return Agent, BrowserSession, Tools, ActionResult

    def run_browser_history(self, history):
        plan, task = self.browser_task()
        with patch("phase_workflow.brains._require_distribution"):
            result = browseruse_runner(
                task,
                Path("/tmp/day"),
                plan.brain_profile,
                framework_api=self.browser_api(history),
                llm_factory=lambda model, logger: (model, logger),
                step_logger=lambda logger, value: None,
                chromium_args=[],
            )
        return result

    def test_browseruse_done_requires_explicit_success(self):
        class FailedJudge:
            def is_done(self):
                return True

            def is_successful(self):
                return False

        self.assertFalse(self.run_browser_history(FailedJudge()).completed)

    def test_browseruse_malformed_or_failed_final_result_fails(self):
        class NotDone:
            def is_done(self):
                return False

            def is_successful(self):
                return True

        class Malformed:
            def is_done(self):
                raise ValueError("malformed action result")

            def is_successful(self):
                return True

        self.assertFalse(self.run_browser_history(None).completed)
        self.assertFalse(self.run_browser_history(NotDone()).completed)
        self.assertFalse(self.run_browser_history(Malformed()).completed)

    def test_sequential_browseruse_runs_close_resources_in_owning_loops(self):
        plan, task = self.browser_task()
        state = {"sessions": [], "agents": []}

        class BrowserSession:
            def __init__(self, **_values):
                self.loop = asyncio.get_running_loop()
                self.future = self.loop.create_future()
                self.future.set_result(True)
                self.closed = False
                state["sessions"].append(self)

        class History:
            def is_done(self):
                return True

            def is_successful(self):
                return True

        class Agent:
            def __init__(self, **values):
                self.session = values["browser_session"]
                self.closed = False
                state["agents"].append(self)

            async def run(self, max_steps):
                self.max_steps = max_steps
                self.asserted_loop = asyncio.get_running_loop()
                return History()

            async def close(self):
                current = asyncio.get_running_loop()
                if current is not self.session.loop:
                    raise RuntimeError("cross-loop BrowserUse cleanup")
                if self.session.future.get_loop() is not current:
                    raise RuntimeError("future belongs to another loop")
                await self.session.future
                self.session.closed = True
                self.closed = True

        class Tools:
            pass

        class ActionResult:
            pass

        api = Agent, BrowserSession, Tools, ActionResult
        with patch("phase_workflow.brains._require_distribution"):
            first = browseruse_runner(
                task,
                Path("/tmp/day"),
                plan.brain_profile,
                framework_api=api,
                llm_factory=lambda model, logger: (model, logger),
                step_logger=lambda logger, value: None,
                chromium_args=[],
            )
            second = browseruse_runner(
                task,
                Path("/tmp/day"),
                plan.brain_profile,
                framework_api=api,
                llm_factory=lambda model, logger: (model, logger),
                step_logger=lambda logger, value: None,
                chromium_args=[],
            )

        self.assertTrue(first.completed)
        self.assertTrue(second.completed)
        self.assertEqual(len(state["sessions"]), 2)
        self.assertIsNot(state["sessions"][0].loop, state["sessions"][1].loop)
        self.assertTrue(all(item.closed for item in state["sessions"]))
        self.assertTrue(all(item.closed for item in state["agents"]))

    @staticmethod
    def smol_task():
        document = feedback_document("smolagents-gpu")
        document["schedule"][0]["sequence"][0][
            "resource_id"
        ] = "wikipedia_compiler"
        plan = load_document(document, "smolagents-gpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[0]
        )
        return plan, task

    @staticmethod
    def smol_api(urls, visitor, final="fabricated nonempty answer"):
        state = {"visitor_calls": [], "tool_errors": []}

        class Tool:
            def __init__(self, *args, **kwargs):
                pass

        class VisitWebpageTool(Tool):
            inputs = {"url": {"type": "string"}}
            output_type = "string"

            def forward(self, url):
                state["visitor_calls"].append(url)
                return visitor(url)

        class LiteLLMModel:
            def __init__(self, **_values):
                pass

        class CodeAgent:
            def __init__(self, **values):
                self.tool = values["tools"][0]

            def run(self, _task):
                for url in urls:
                    try:
                        self.tool.forward(url)
                    except Exception as exc:
                        state["tool_errors"].append(str(exc))
                return final

        return (CodeAgent, LiteLLMModel, Tool, VisitWebpageTool), state

    def run_smol_research(self, urls, visitor):
        plan, task = self.smol_task()
        api, state = self.smol_api(urls, visitor)

        def reader(url):
            state["visitor_calls"].append(url)
            return visitor(url)

        with patch("phase_workflow.brains._require_distribution"):
            result = smolagents_runner(
                task,
                Path("/tmp/day"),
                plan.brain_profile,
                webpage_reader=reader,
                framework_api=api,
            )
        return result, task, state

    def test_smol_nonempty_answer_after_tool_failure_is_not_completion(self):
        plan, task = self.smol_task()
        assigned = task.resource["url"]

        def missing_dependency(_url):
            raise ImportError("markdownify unavailable")

        result, _task, state = self.run_smol_research(
            [assigned], missing_dependency
        )
        self.assertFalse(result.completed)
        self.assertEqual(state["visitor_calls"], [assigned])
        self.assertIn("markdownify unavailable", state["tool_errors"][0])

    def test_smol_requires_assigned_initial_and_discovered_followup(self):
        _plan, task = self.smol_task()
        assigned = task.resource["url"]
        followup = "https://en.wikipedia.org/wiki/Photovoltaics"

        def visit(url):
            if url == assigned:
                return 'Solar power [Photovoltaics](/wiki/Photovoltaics "article")'
            if url == followup:
                return "Photovoltaics convert sunlight into electricity."
            raise AssertionError(url)

        result, _task, state = self.run_smol_research(
            [assigned, followup], visit
        )
        self.assertTrue(result.completed)
        self.assertEqual(state["visitor_calls"], [assigned, followup])

        result, _task, state = self.run_smol_research(
            ["https://example.com/unassigned"], visit
        )
        self.assertFalse(result.completed)
        self.assertEqual(state["visitor_calls"], [])

    def test_web_reader_uses_identifiable_request_and_readable_markdown(self):
        calls = []

        class Response:
            text = "<html>assigned</html>"

            @staticmethod
            def raise_for_status():
                return None

        requests = ModuleType("requests")
        requests.get = lambda url, **kwargs: calls.append((url, kwargs)) or Response()
        markdown = ModuleType("markdownify")
        markdown.markdownify = lambda _html: (
            '[Compiler design](/wiki/Compiler_design "relevant article")'
        )
        with patch.dict(
            sys.modules, {"requests": requests, "markdownify": markdown}
        ):
            content = _read_webpage("https://en.wikipedia.org/wiki/Compiler")
        self.assertIn("Compiler design", content)
        self.assertEqual(calls[0][1]["timeout"], 20)
        self.assertEqual(
            calls[0][1]["headers"]["User-Agent"],
            "RUSE phase-workflow control/1.0",
        )

    def test_smol_missing_or_repeated_assigned_research_action_fails(self):
        _plan, task = self.smol_task()
        assigned = task.resource["url"]
        followup = "https://en.wikipedia.org/wiki/Photovoltaics"

        def visit(url):
            return (
                f"[Photovoltaics]({followup})"
                if url == assigned else "follow-up content"
            )

        result, _task, _state = self.run_smol_research([], visit)
        self.assertFalse(result.completed)
        result, _task, state = self.run_smol_research(
            [assigned, followup, followup], visit
        )
        self.assertFalse(result.completed)
        self.assertEqual(state["visitor_calls"], [assigned, followup])
        self.assertIn("repeatedly", state["tool_errors"][-1])

    def test_browser_step_logger_never_marks_missing_or_failed_result_success(self):
        browser_use = ModuleType("browser_use")
        browser_use.Agent = object
        browser_use.ChatOllama = object
        browser_package = ModuleType("browser_use.browser")
        session_module = ModuleType("browser_use.browser.session")
        session_module.BrowserSession = object
        modules = {
            "browser_use": browser_use,
            "browser_use.browser": browser_package,
            "browser_use.browser.session": session_module,
        }
        with patch.dict(sys.modules, modules):
            from brains.browseruse.agent import _log_bu_steps

        class Action:
            def model_dump(self, **_kwargs):
                return {"play_assigned_video": {}}

        class Logger:
            def __init__(self):
                self.successes = []
                self.errors = []

            def step_start(self, *_args, **_kwargs):
                pass

            def step_success(self, name, **_kwargs):
                self.successes.append(name)

            def step_error(self, name, message, **_kwargs):
                self.errors.append((name, message))

        for results in (
            [],
            [SimpleNamespace(error="playback failed", success=False)],
            [SimpleNamespace(error=None, success=False)],
        ):
            logger = Logger()
            step = SimpleNamespace(
                model_output=SimpleNamespace(action=[Action()]),
                result=results,
                metadata=None,
            )
            _log_bu_steps(logger, SimpleNamespace(history=[step]))
            self.assertEqual(logger.successes, [])
            self.assertEqual(len(logger.errors), 1)


class OpenDocumentValidationTests(unittest.TestCase):
    @staticmethod
    def task(resource_id):
        document = feedback_document("scripted-cpu")
        document["schedule"][0]["sequence"][2]["resource_id"] = resource_id
        plan = load_document(document, "scripted-cpu")
        return WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[2]
        )

    def test_exact_assigned_odt_and_ods_validate(self):
        for resource_id in (
            "document_team_meeting_notes",
            "spreadsheet_expense_tracker",
        ):
            with (
                self.subTest(resource_id=resource_id),
                tempfile.TemporaryDirectory() as td,
            ):
                task = self.task(resource_id)
                result = OpenDocumentWriter().create(task, Path(td))
                validate_open_document(task, Path(td), result.artifact)

    def test_corrupt_incomplete_and_wrong_format_artifacts_fail(self):
        task = self.task("spreadsheet_expense_tracker")
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            artifact = workspace / task.resource["filename"]
            artifact.write_bytes(b"not a zip")
            with self.assertRaisesRegex(RuntimeError, "invalid assigned"):
                validate_open_document(task, workspace, artifact)

            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr(
                    "mimetype", "application/vnd.oasis.opendocument.spreadsheet"
                )
                archive.writestr(
                    "content.xml",
                    OpenDocumentWriter._content_xml(
                        "office:spreadsheet",
                        "<table:table table:name=\"Sheet1\">"
                        "<table:table-row><table:table-cell>"
                        "<text:p>AfternoonA2</text:p>"
                        "</table:table-cell></table:table-row></table:table>",
                    ),
                )
            with self.assertRaisesRegex(RuntimeError, "cells do not match"):
                validate_open_document(task, workspace, artifact)

            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr(
                    "mimetype", "application/vnd.oasis.opendocument.text"
                )
                archive.writestr(
                    "content.xml",
                    OpenDocumentWriter._content_xml("office:spreadsheet", ""),
                )
            with self.assertRaisesRegex(RuntimeError, "mimetype"):
                validate_open_document(task, workspace, artifact)

        document_task = self.task("document_team_meeting_notes")
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            artifact = workspace / document_task.resource["filename"]
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr(
                    "mimetype", "application/vnd.oasis.opendocument.text"
                )
                archive.writestr(
                    "content.xml",
                    OpenDocumentWriter._content_xml(
                        "office:text",
                        f"<text:h>{document_task.resource['title']}</text:h>",
                    ),
                )
            with self.assertRaisesRegex(RuntimeError, "missing supplied content"):
                validate_open_document(document_task, workspace, artifact)

    def test_mchp_writer_ui_saves_exact_valid_assigned_odt(self):
        task = self.task("document_team_meeting_notes")
        typed_content = []
        save_path = []
        state = {"saving": False, "hotkeys": [], "pressed": [], "clicks": []}

        pyautogui = ModuleType("pyautogui")

        def write(value, interval=None):
            self.assertEqual(interval, 0.01)
            if state["saving"]:
                save_path.append(str(value))
            else:
                typed_content.append(str(value))

        def hotkey(*keys):
            state["hotkeys"].append(keys)
            if keys == ("ctrl", "shift", "s"):
                state["saving"] = True
            elif keys == ("ctrl", "a") and state["saving"]:
                save_path.clear()

        def press(key, presses=1):
            state["pressed"].append(key)
            if key != "enter" or not state["saving"]:
                return
            artifact = Path("".join(save_path))
            artifact.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(f"<text:p>{value}</text:p>" for value in typed_content)
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr(
                    "mimetype", "application/vnd.oasis.opendocument.text"
                )
                archive.writestr(
                    "content.xml",
                    OpenDocumentWriter._content_xml("office:text", body),
                )
            state["saving"] = False

        pyautogui.write = write
        pyautogui.hotkey = hotkey
        pyautogui.press = press
        pyautogui.size = lambda: (1280, 1024)
        pyautogui.click = lambda x, y: state["clicks"].append((x, y))
        lorem = ModuleType("lorem")
        lorem_text = ModuleType("lorem.text")
        lorem_text.TextLorem = object
        lorem.text = lorem_text

        sys.modules.pop(
            "brains.mchp.app.workflows.open_office_writer", None
        )
        with patch.dict(
            sys.modules,
            {"pyautogui": pyautogui, "lorem": lorem, "lorem.text": lorem_text},
        ):
            writer_module = importlib.import_module(
                "brains.mchp.app.workflows.open_office_writer"
            )

        class Process:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        process = Process()
        with tempfile.TemporaryDirectory() as td, patch.object(
            writer_module.subprocess, "Popen", return_value=process
        ) as popen, patch.object(writer_module, "sleep", return_value=None):
            workspace = Path(td)
            editor = writer_module.DocumentEditor(default_wait_time=0)
            with patch.object(
                writer_module, "wait_for_focused_window"
            ) as ready, patch.object(
                writer_module, "wait_for_stable_artifact"
            ) as stable:
                result = MCHPDocumentWorkflows(
                    writer_factory=lambda: editor
                ).create(task, workspace)
            validate_open_document(task, workspace, result.artifact)

        self.assertTrue(result.completed)
        self.assertEqual(Path(result.artifact).name, task.resource["filename"])
        self.assertEqual(typed_content[0], task.resource["title"])
        self.assertEqual(state["clicks"], [(640, 512)])
        self.assertLess(
            state["hotkeys"].index(("ctrl", "f6")),
            state["hotkeys"].index(("ctrl", "home")),
        )
        self.assertIn(("ctrl", "shift", "s"), state["hotkeys"])
        self.assertIn(("ctrl", "a"), state["hotkeys"])
        ready.assert_called_once()
        self.assertEqual(ready.call_args.args, ("LibreOffice Writer",))
        self.assertEqual(ready.call_args.kwargs["process"], process)
        self.assertEqual(
            ready.call_args.kwargs["artifact"], Path(result.artifact)
        )
        ready.call_args.kwargs["blocking_dialog_action"]()
        self.assertIn("esc", state["pressed"])
        launch = popen.call_args.args[0]
        self.assertIn("--writer", launch)
        self.assertNotIn("--nodefault", launch)
        self.assertIn("private:factory/swriter", launch)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        stable.assert_called_once_with(Path(result.artifact))
        self.assertTrue(process.terminated)
        sys.modules.pop("brains.mchp.app.workflows.open_office_writer", None)

    def test_mchp_calc_ui_sets_exact_cells_and_saves_stable_ods(self):
        task = self.task("spreadsheet_expense_tracker")
        state = {
            "mode": None,
            "row": 1,
            "column": 0,
            "save_path": [],
            "cells": {},
            "hotkeys": [],
            "pressed": [],
            "clicks": [],
        }
        pyautogui = ModuleType("pyautogui")

        def hotkey(*keys):
            state["hotkeys"].append(keys)
            if keys == ("ctrl", "home"):
                state["row"] = 1
                state["column"] = 0
            elif keys == ("ctrl", "shift", "s"):
                state["mode"] = "save"
            elif keys == ("ctrl", "a") and state["mode"] == "save":
                state["save_path"].clear()

        def write(value, interval=None):
            self.assertEqual(interval, 0.01)
            if state["mode"] == "save":
                state["save_path"].append(str(value))
            else:
                coordinate = (
                    f"{chr(ord('A') + state['column'])}{state['row']}"
                )
                state["cells"][coordinate] = str(value)

        def press(key, presses=1):
            state["pressed"].append(key)
            if key == "enter" and state["mode"] == "save":
                artifact = Path("".join(state["save_path"]))
                artifact.parent.mkdir(parents=True, exist_ok=True)
                rows = [task.resource["columns"], *task.resource["rows"]]
                rendered = []
                for row_index, row in enumerate(rows, start=1):
                    cells = []
                    for column_index, _value in enumerate(row):
                        coordinate = f"{chr(ord('A') + column_index)}{row_index}"
                        value = state["cells"].get(coordinate, "")
                        cells.append(
                            '<table:table-cell office:value-type="string">'
                            f"<text:p>{value}</text:p></table:table-cell>"
                        )
                    rendered.append(
                        "<table:table-row>" + "".join(cells)
                        + "</table:table-row>"
                    )
                body = (
                    '<table:table table:name="Sheet1">'
                    + "".join(rendered)
                    + "</table:table>"
                )
                with zipfile.ZipFile(artifact, "w") as archive:
                    archive.writestr(
                        "mimetype",
                        "application/vnd.oasis.opendocument.spreadsheet",
                    )
                    archive.writestr(
                        "content.xml",
                        OpenDocumentWriter._content_xml(
                            "office:spreadsheet", body
                        ),
                    )
                state["mode"] = None
            elif state["mode"] is None and key == "tab":
                state["column"] += 1
            elif state["mode"] is None and key == "enter":
                state["row"] += 1
            elif state["mode"] is None and key == "home":
                state["column"] = 0

        pyautogui.hotkey = hotkey
        pyautogui.typewrite = lambda _value: None
        pyautogui.write = write
        pyautogui.press = press
        pyautogui.size = lambda: (1280, 1024)
        pyautogui.click = lambda x, y: state["clicks"].append((x, y))
        lorem = ModuleType("lorem")
        lorem_text = ModuleType("lorem.text")
        lorem_text.TextLorem = object
        lorem.text = lorem_text

        module_name = "brains.mchp.app.workflows.open_office_calc"
        sys.modules.pop(module_name, None)
        with patch.dict(
            sys.modules,
            {"pyautogui": pyautogui, "lorem": lorem, "lorem.text": lorem_text},
        ):
            calc_module = importlib.import_module(module_name)

        class Process:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        process = Process()
        with tempfile.TemporaryDirectory() as td, patch.object(
            calc_module.subprocess, "Popen", return_value=process
        ) as popen, patch.object(calc_module, "sleep", return_value=None), patch.object(
            calc_module, "wait_for_focused_window"
        ) as ready, patch.object(
            calc_module, "wait_for_stable_artifact"
        ) as stable:
            workspace = Path(td)
            editor = calc_module.SpreadsheetEditor(default_wait_time=0)
            result = MCHPDocumentWorkflows(
                calc_factory=lambda: editor
            ).create(task, workspace)

        expected_cells = {}
        for row_index, row in enumerate(
            [task.resource["columns"], *task.resource["rows"]], start=1
        ):
            for column_index, value in enumerate(row):
                expected_cells[
                    f"{chr(ord('A') + column_index)}{row_index}"
                ] = str(value)
        self.assertEqual(state["cells"], expected_cells)
        self.assertEqual(state["clicks"], [(640, 512)])
        self.assertLess(
            state["hotkeys"].index(("ctrl", "f6")),
            state["hotkeys"].index(("ctrl", "home")),
        )
        self.assertIn(("ctrl", "shift", "s"), state["hotkeys"])
        ready.assert_called_once()
        self.assertEqual(ready.call_args.args, ("LibreOffice Calc",))
        self.assertEqual(ready.call_args.kwargs["process"], process)
        self.assertEqual(
            ready.call_args.kwargs["artifact"], Path(result.artifact)
        )
        ready.call_args.kwargs["blocking_dialog_action"]()
        self.assertIn("esc", state["pressed"])
        launch = popen.call_args.args[0]
        self.assertIn("--calc", launch)
        self.assertNotIn("--nodefault", launch)
        self.assertIn("private:factory/scalc", launch)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        stable.assert_called_once_with(Path(result.artifact))
        self.assertTrue(process.terminated)
        sys.modules.pop(module_name, None)

    def test_mchp_document_creation_retries_one_absent_or_invalid_artifact(self):
        for resource_id in (
            "document_team_meeting_notes",
            "spreadsheet_expense_tracker",
        ):
            with (
                self.subTest(resource_id=resource_id),
                tempfile.TemporaryDirectory() as td,
            ):
                task = self.task(resource_id)
                workspace = Path(td)
                attempts = []
                cleaned = []

                class Editor:
                    def __init__(self, attempt):
                        self.attempt = attempt

                    def create_assigned(self, _resource, destination, logger=None):
                        attempts.append(self.attempt)
                        artifact = Path(destination) / task.resource["filename"]
                        if self.attempt == 0:
                            if task.resource["kind"] == "spreadsheet":
                                with zipfile.ZipFile(artifact, "w") as archive:
                                    archive.writestr(
                                        "mimetype",
                                        "application/vnd.oasis.opendocument.spreadsheet",
                                    )
                                    archive.writestr(
                                        "content.xml",
                                        OpenDocumentWriter._content_xml(
                                            "office:spreadsheet",
                                            '<table:table table:name="Sheet1"/>',
                                        ),
                                    )
                            return artifact
                        return OpenDocumentWriter().create(
                            task, Path(destination)
                        ).artifact

                    def cleanup(self):
                        cleaned.append(self.attempt)

                def factory():
                    return Editor(len(attempts))

                kwargs = (
                    {"writer_factory": factory}
                    if task.resource["kind"] == "document"
                    else {"calc_factory": factory}
                )
                result = MCHPDocumentWorkflows(**kwargs).create(task, workspace)
                validate_open_document(task, workspace, result.artifact)
                self.assertEqual(attempts, [0, 1])
                self.assertEqual(cleaned, [0, 1])


class LibreOfficeReadinessTests(unittest.TestCase):
    @staticmethod
    def _xlib_modules():
        xlib = ModuleType("Xlib")
        xlib.X = SimpleNamespace(Above=1, RevertToParent=2, CurrentTime=3)
        display_module = ModuleType("Xlib.display")

        class Root:
            def get_wm_name(self):
                return ""
            def query_tree(self):
                return SimpleNamespace(children=[])

        class Display:
            def screen(self):
                return SimpleNamespace(root=Root())
            def close(self):
                self.closed = True

        display_module.Display = Display
        xlib.display = display_module
        return {"Xlib": xlib, "Xlib.display": display_module}

    def test_missing_window_is_bounded_and_reports_runtime_evidence(self):
        from brains.mchp.app.utility.libreoffice_gui import wait_for_focused_window
        clock = [0.0]
        artifact = Path("/tmp/expected-document.odt")
        process = SimpleNamespace(poll=lambda: None)
        with patch.dict(sys.modules, self._xlib_modules()), self.assertRaisesRegex(
            RuntimeError,
            r"process_state=running.*expected_window='LibreOffice Writer'.*elapsed_seconds=1\.000.*expected_artifact=/tmp/expected-document.odt.*artifact_exists=false",
        ):
            wait_for_focused_window(
                "LibreOffice Writer", process=process, artifact=artifact,
                timeout_s=1.0, monotonic=lambda: clock[0],
                sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay),
            )
        self.assertLessEqual(clock[0], 1.0)

    def test_early_libreoffice_exit_is_reported_immediately(self):
        from brains.mchp.app.utility.libreoffice_gui import wait_for_focused_window
        clock = [0.0]
        process = SimpleNamespace(poll=lambda: 7)
        with patch.dict(sys.modules, self._xlib_modules()), self.assertRaisesRegex(
            RuntimeError, r"process_state=exited exit_code=7",
        ):
            wait_for_focused_window(
                "LibreOffice Calc", process=process, artifact=Path("/tmp/expected.ods"),
                timeout_s=30, monotonic=lambda: clock[0],
                sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay),
            )
        self.assertEqual(clock[0], 0.0)

    def test_assigned_artifact_cleanup_removes_only_owned_sidecars(self):
        from brains.mchp.app.utility.libreoffice_gui import (
            remove_artifact_sidecars,
        )

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            artifact = workspace / "expense-tracker.ods"
            lock = workspace / ".~lock.expense-tracker.ods#"
            prior = workspace / "lu-prior.tmp"
            owned = workspace / "lu-owned.tmp"
            lock.write_text("lock")
            prior.write_text("prior")
            owned.write_text("owned")

            remove_artifact_sidecars(
                artifact,
                preexisting_temp_files={prior},
            )

            self.assertFalse(lock.exists())
            self.assertFalse(owned.exists())
            self.assertTrue(prior.exists())

    def test_owned_libreoffice_process_group_is_terminated_and_reaped(self):
        from brains.mchp.app.utility import libreoffice_gui

        process = SimpleNamespace(pid=4312)
        process.wait = Mock()
        with patch.object(
            libreoffice_gui.os,
            "killpg",
            side_effect=[None, ProcessLookupError],
        ) as killpg:
            libreoffice_gui.terminate_owned_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [call(4312, signal.SIGTERM), call(4312, 0)],
        )
        process.wait.assert_called_once()

    @staticmethod
    def _interactive_xlib_modules(children):
        focus = {"window": None}

        class Window:
            def __init__(self, window_id, title, wm_class):
                self.id = window_id
                self.title = title
                self.wm_class = wm_class

            def get_wm_name(self):
                return self.title

            def get_wm_class(self):
                return self.wm_class

            def query_tree(self):
                return SimpleNamespace(children=[])

            def configure(self, **_kwargs):
                pass

            def set_input_focus(self, *_args):
                focus["window"] = self

        class Root(Window):
            def __init__(self):
                super().__init__(0, "", ())

            def query_tree(self):
                return SimpleNamespace(children=list(children()))

        class Display:
            def screen(self):
                return SimpleNamespace(root=Root())

            def sync(self):
                pass

            def get_input_focus(self):
                return SimpleNamespace(focus=focus["window"])

            def close(self):
                pass

        xlib = ModuleType("Xlib")
        xlib.X = SimpleNamespace(Above=1, RevertToParent=2, CurrentTime=3)
        display_module = ModuleType("Xlib.display")
        display_module.Display = Display
        xlib.display = display_module
        return {"Xlib": xlib, "Xlib.display": display_module}, Window, focus

    def test_tip_dialog_is_dismissed_before_focusing_document(self):
        from brains.mchp.app.utility.libreoffice_gui import wait_for_focused_window

        windows = []
        modules, Window, focus = self._interactive_xlib_modules(lambda: windows)
        calc = Window(
            1,
            "Untitled 1 — LibreOffice Calc",
            ("libreoffice", "libreoffice-calc"),
        )
        tip = Window(2, "Tip of the Day: 1/225", ("soffice", "Soffice"))
        windows[:] = [calc, tip]
        dismissals = []

        def dismiss():
            dismissals.append("tip")
            windows.remove(tip)

        with patch.dict(sys.modules, modules):
            wait_for_focused_window(
                "LibreOffice Calc",
                process=SimpleNamespace(poll=lambda: None),
                blocking_dialog_action=dismiss,
                sleeper=lambda _delay: None,
            )

        self.assertEqual(dismissals, ["tip"])
        self.assertIs(focus["window"], calc)


class MCHPDriverLifecycleTests(unittest.TestCase):
    def test_concurrent_firefox_startup_handshakes_are_serialized(self):
        from brains.mchp.app.utility import webdriver_helper as helper_module

        state = {"active": 0, "maximum": 0}
        state_lock = threading.Lock()
        created = []

        class Options:
            def add_argument(self, _value):
                pass

            def set_preference(self, _name, _value):
                pass

        class Service:
            def __init__(self, executable_path=None):
                self.executable_path = executable_path

            def stop(self):
                pass

        class Driver:
            def quit(self):
                pass

        def create_driver(**_kwargs):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.03)
            with state_lock:
                state["active"] -= 1
            driver = Driver()
            created.append(driver)
            return driver

        with patch.object(
            helper_module.WebDriverHelper,
            "_find_geckodriver",
            return_value="/usr/local/bin/geckodriver",
        ), patch.object(
            helper_module.webdriver, "FirefoxOptions", Options, create=True
        ), patch.object(
            helper_module, "FirefoxService", Service
        ), patch.object(
            helper_module.webdriver,
            "Firefox",
            side_effect=create_driver,
            create=True,
        ):
            with ThreadPoolExecutor(max_workers=6) as pool:
                owners = list(
                    pool.map(
                        lambda _index: helper_module.WebDriverHelper.independent(),
                        range(6),
                    )
                )
            for owner in owners:
                owner.cleanup()

        self.assertEqual(len(created), 6)
        self.assertEqual(state["maximum"], 1)

    def test_concurrent_canonical_browser_workflows_use_independent_drivers(self):
        selenium = ModuleType("selenium")
        selenium_webdriver = ModuleType("selenium.webdriver")
        selenium.webdriver = selenium_webdriver
        selenium_firefox = ModuleType("selenium.webdriver.firefox")
        selenium_service = ModuleType("selenium.webdriver.firefox.service")
        selenium_service.Service = object
        modules = {
            "selenium": selenium,
            "selenium.webdriver": selenium_webdriver,
            "selenium.webdriver.firefox": selenium_firefox,
            "selenium.webdriver.firefox.service": selenium_service,
        }
        with patch.dict(sys.modules, modules):
            from brains.mchp.app.utility.webdriver_helper import WebDriverHelper
            modules[
                "brains.mchp.app.utility.webdriver_helper"
            ] = sys.modules["brains.mchp.app.utility.webdriver_helper"]

        document = six_workflow_document("mchp-cpu")
        plan = load_document(document, "mchp-cpu")
        registry = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp"))
        tasks = {
            entry.workflow: registry.resolve(entry)
            for entry in plan.windows[0].sequence
        }
        barrier = threading.Barrier(2)
        created = []

        class Driver:
            def __init__(self, download_dir):
                self.download_dir = Path(download_dir) if download_dir else None
                self.closed = False

            def get(self, _url):
                barrier.wait(timeout=2)

            def find_element(self, *_args):
                return object()

            def execute_script(self, *_args):
                if self.download_dir is None:
                    return
                barrier.wait(timeout=2)
                expected = tasks["FileDownload"].resource["expected_bytes"]
                name = tasks["FileDownload"].resource["url"].rsplit("/", 1)[-1]
                (self.download_dir / name).write_bytes(b"x" * expected)

            def quit(self):
                self.closed = True

        def initialize(instance, download_dir=None):
            instance._driver = Driver(download_dir)
            created.append(instance._driver)

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            sys.modules, modules
        ), patch.object(WebDriverHelper, "__init__", initialize):
            workspace = Path(temporary)
            videos = SeleniumResourceWorkflows(
                _mchp_driver, sleeper=lambda _seconds: None
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                video_future = pool.submit(
                    videos.video_viewing, tasks["VideoViewing"]
                )
                download_future = pool.submit(
                    firefox_download,
                    tasks["FileDownload"],
                    workspace,
                    lambda path: _mchp_driver(path),
                )
                self.assertTrue(video_future.result(timeout=3).completed)
                self.assertTrue(download_future.result(timeout=3).is_file())

        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        self.assertTrue(all(driver.closed for driver in created))

    def test_sequential_browser_workflows_receive_fresh_driver_after_cleanup(self):
        from brains.mchp.app.utility.base_driver import Singleton

        selenium = ModuleType("selenium")
        selenium_webdriver = ModuleType("selenium.webdriver")
        selenium.webdriver = selenium_webdriver
        selenium_firefox = ModuleType("selenium.webdriver.firefox")
        selenium_service = ModuleType("selenium.webdriver.firefox.service")
        selenium_service.Service = object
        modules = {
            "selenium": selenium,
            "selenium.webdriver": selenium_webdriver,
            "selenium.webdriver.firefox": selenium_firefox,
            "selenium.webdriver.firefox.service": selenium_service,
        }
        with patch.dict(sys.modules, modules):
            from brains.mchp.app.utility.webdriver_helper import WebDriverHelper

        created = []

        class Link:
            def get_attribute(self, _name):
                return "https://example.com/follow"

        class Driver:
            def __init__(self):
                self.urls = []
                self.closed = False

            def get(self, url):
                self.urls.append(url)

            def find_elements(self, *_args):
                return [Link()]

            def find_element(self, *_args):
                return object()

            def execute_script(self, *_args):
                pass

            def quit(self):
                self.closed = True

        def initialize(instance):
            instance._driver = Driver()
            created.append(instance._driver)

        plan = load_control_plan("mchp-cpu")
        registry = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp"))
        web = registry.resolve(plan.windows[0].sequence[0])
        video = registry.resolve(plan.windows[0].sequence[1])
        Singleton._instances.pop(WebDriverHelper, None)
        try:
            with patch.object(WebDriverHelper, "__init__", initialize):
                workflows = SeleniumResourceWorkflows(
                    WebDriverHelper, sleeper=lambda _seconds: None
                )
                self.assertTrue(workflows.web_research(web).completed)
                self.assertTrue(workflows.video_viewing(video).completed)
        finally:
            Singleton._instances.pop(WebDriverHelper, None)
        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        self.assertTrue(all(driver.closed for driver in created))


class LiteLLMCallbackRegistrationTests(unittest.TestCase):
    def test_repeated_smolagents_setup_emits_one_response_per_request(self):
        litellm = ModuleType("litellm")
        litellm.callbacks = []
        litellm.success_callback = []
        litellm.failure_callback = []
        litellm.set_verbose = False
        litellm.__version__ = "test"

        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))]
        )
        with tempfile.TemporaryDirectory() as temporary:
            logger = AgentLogger(
                "smolagents-gpu", log_dir=temporary, session_id="callbacks"
            )
            with patch.dict(sys.modules, {"litellm": litellm}):
                for index in range(6):
                    setup_litellm_callbacks(logger)
                    for callback in litellm.callbacks:
                        callback.log_pre_api_call(
                            "ollama/test", [{"content": f"request {index}"}], {}
                        )
                        callback.log_success_event(
                            {"model": "ollama/test"}, response, 0.0, 1.0
                        )
                    for callback in litellm.success_callback:
                        callback({"model": "ollama/test"}, response, 0.0, 1.0)
            logger.close()
            event_types = [
                json.loads(line)["event_type"]
                for line in logger.log_file.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(litellm.callbacks), 1)
        self.assertEqual(litellm.success_callback, [])
        self.assertEqual(litellm.failure_callback, [])
        self.assertEqual(event_types.count("llm_request"), 6)
        self.assertEqual(event_types.count("llm_response"), 6)


class VideoMechanicsTests(unittest.TestCase):
    def test_available_ytdlp_format_is_selected_without_fixed_format_guess(self):
        info = {
            "formats": [
                {"format_id": "storyboard", "url": None},
                {
                    "format_id": "audio",
                    "url": "https://media.example/audio",
                    "vcodec": "none",
                    "acodec": "opus",
                },
                {
                    "format_id": "progressive",
                    "url": "https://media.example/assigned.mp4",
                    "vcodec": "h264",
                    "acodec": "aac",
                },
            ]
        }
        self.assertEqual(
            _select_media_url(info), "https://media.example/assigned.mp4"
        )
        self.assertIsNone(_select_media_url({"formats": []}))


class LLMVideoRunnerTests(unittest.TestCase):
    @staticmethod
    def video_task(config_key):
        plan = load_control_plan(config_key)
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[1]
        )
        return plan, task

    @staticmethod
    def browser_api(invoke):
        state = {}

        class ActionResult:
            def __init__(self, **values):
                self.__dict__.update(values)

        class Actions:
            def __init__(self):
                self.actions = {"navigate": object(), "done": object()}

        class Registry:
            def __init__(self):
                self.registry = Actions()

        class Tools:
            def __init__(self):
                self.registry = Registry()

            def exclude_action(self, name):
                self.registry.registry.actions.pop(name, None)

            def action(self, _description, **_kwargs):
                def register(function):
                    self.registry.registry.actions[function.__name__] = function
                    return function
                return register

            async def act(self, *args, action_timeout=None, **kwargs):
                state["action_timeout"] = action_timeout
                action_name, action = next(
                    iter(self.registry.registry.actions.items())
                )
                state["action_name"] = action_name
                result = action()
                if inspect.isawaitable(result):
                    return await result
                return result

        class BrowserSession:
            def __init__(self, **values):
                self.values = values

        class History:
            def is_done(self):
                return True

            def is_successful(self):
                return True

        class Agent:
            def __init__(self, **values):
                state["agent"] = values

            async def run(self, max_steps):
                state["max_steps"] = max_steps
                actions = state["agent"]["tools"].registry.registry.actions
                state["actions"] = actions
                if invoke:
                    state["action_result"] = await state["agent"]["tools"].act()
                return History()

        return (Agent, BrowserSession, Tools, ActionResult), state

    @staticmethod
    def smol_api(invoke):
        state = {}

        class Tool:
            def __init__(self, *args, **kwargs):
                pass

            def __call__(self, *args, **kwargs):
                return self.forward(*args, **kwargs)

        class VisitWebpageTool(Tool):
            pass

        class LiteLLMModel:
            def __init__(self, **values):
                self.values = values

        class CodeAgent:
            def __init__(self, **values):
                state["agent"] = values

            def run(self, task):
                state["task"] = task
                if invoke:
                    try:
                        state["tool_result"] = state["agent"]["tools"][0]()
                    except Exception as exc:
                        state["tool_error"] = exc
                return "finished"

        return (CodeAgent, LiteLLMModel, Tool, VisitWebpageTool), state

    def run_browser(self, invoke, player):
        plan, task = self.video_task("browseruse-gpu")
        api, state = self.browser_api(invoke)
        with patch("phase_workflow.brains._require_distribution"):
            result = browseruse_runner(
                task,
                Path("/tmp/local-day"),
                plan.brain_profile,
                video_player=player,
                framework_api=api,
                llm_factory=lambda model, logger: (model, logger),
                step_logger=lambda logger, history: None,
                chromium_args=[],
            )
        return result, task, state

    def run_smol(self, invoke, player):
        plan, task = self.video_task("smolagents-gpu")
        api, state = self.smol_api(invoke)
        with patch("phase_workflow.brains._require_distribution"):
            result = smolagents_runner(
                task,
                Path("/tmp/local-day"),
                plan.brain_profile,
                video_player=player,
                framework_api=api,
            )
        return result, task, state

    def test_both_runners_receive_exact_task_and_invoke_one_playback(self):
        for name, runner in (("browseruse", self.run_browser), ("smolagents", self.run_smol)):
            calls = []
            result, task, state = runner(
                True, lambda assigned: calls.append(assigned) is None
            )
            self.assertTrue(result.completed, name)
            self.assertEqual(calls, [task], name)
            raw_task = state["agent"]["task"] if name == "browseruse" else state["task"]
            delivered = json.loads(raw_task)
            self.assertEqual(delivered["instruction"], task.instruction)
            self.assertEqual(delivered["resource_id"], task.resource_id)
            self.assertEqual(delivered["resource"], dict(task.resource))

        _browser_result, browser_task, browser_state = self.run_browser(
            True, lambda task: True
        )
        browser_actions = browser_state["actions"]
        self.assertEqual(set(browser_actions), {"play_assigned_video"})
        self.assertFalse(browser_state["agent"]["directly_open_url"])
        self.assertEqual(browser_state["agent"]["step_timeout"], 420)
        video_evidence = browser_state["action_result"].extracted_content
        self.assertIn(f"resource_id={browser_task.resource_id}", video_evidence)
        self.assertIn("assigned_url=https://www.youtube.com/watch?v=", video_evidence)
        self.assertIn("expected_seconds=300", video_evidence)
        self.assertIn("observed_seconds=300", video_evidence)
        smol_tools = self.run_smol(True, lambda task: True)[2]["agent"]["tools"]
        self.assertEqual(len(smol_tools), 1)
        self.assertEqual(smol_tools[0].name, "play_assigned_video")
        smol_state = self.run_smol(True, lambda task: True)[2]
        self.assertEqual(
            smol_state["agent"]["executor_kwargs"], {"timeout_seconds": 360}
        )

    def test_completion_without_playback_invocation_fails(self):
        for name, runner in (("browseruse", self.run_browser), ("smolagents", self.run_smol)):
            calls = []
            result, _task, _state = runner(
                False, lambda assigned: calls.append(assigned) is None
            )
            self.assertFalse(result.completed, name)
            self.assertEqual(calls, [], name)

    def test_browser_playback_uses_360_second_framework_action_timeout(self):
        with patch.dict(os.environ, {"BROWSER_USE_ACTION_TIMEOUT_S": "180"}):
            result, task, state = self.run_browser(True, lambda assigned: True)
        self.assertTrue(result.completed)
        self.assertEqual(task.resource["play_seconds"], 300)
        self.assertEqual(state["action_timeout"], 360)

    def test_concurrent_browseruse_sessions_share_one_owning_loop(self):
        plan = load_control_plan("browseruse-gpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[0]
        )
        owning_loops = []
        closed_loops = []

        class BrowserSession:
            def __init__(self, **_values):
                self.loop = asyncio.get_running_loop()
                owning_loops.append(self.loop)

        class History:
            def is_done(self):
                return True

            def is_successful(self):
                return True

        class Agent:
            def __init__(self, **values):
                self.session = values["browser_session"]

            async def run(self, max_steps):
                await asyncio.sleep(0.01)
                return History()

            async def close(self):
                current = asyncio.get_running_loop()
                self.assert_same_loop = current is self.session.loop
                closed_loops.append(current)

        class Tools:
            pass

        class ActionResult:
            pass

        event_loop = _BrowserUseEventLoop()
        api = Agent, BrowserSession, Tools, ActionResult

        def execute():
            return browseruse_runner(
                task,
                Path("/tmp/local-day"),
                plan.brain_profile,
                framework_api=api,
                llm_factory=lambda model, logger: model,
                step_logger=lambda logger, history: None,
                chromium_args=[],
                async_executor=event_loop.run,
            )

        try:
            with patch("phase_workflow.brains._require_distribution"), ThreadPoolExecutor(
                max_workers=2
            ) as pool:
                results = list(pool.map(lambda _index: execute(), range(2)))
        finally:
            event_loop.close()

        self.assertTrue(all(result.completed for result in results))
        self.assertEqual(len(owning_loops), 2)
        self.assertIs(owning_loops[0], owning_loops[1])
        self.assertEqual(closed_loops, owning_loops)

    def test_browseruse_workflow_deadline_cancels_and_cleans_up(self):
        plan = load_control_plan("browseruse-gpu")
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[0]
        )
        state = {"closed": False}

        class BrowserSession:
            def __init__(self, **_values):
                pass

        class Agent:
            def __init__(self, **_values):
                pass

            async def run(self, max_steps):
                await asyncio.Event().wait()

            async def close(self):
                state["closed"] = True

        class Tools:
            pass

        class ActionResult:
            pass

        with patch("phase_workflow.brains._require_distribution"), self.assertRaises(
            TimeoutError
        ):
            browseruse_runner(
                task,
                Path("/tmp/local-day"),
                plan.brain_profile,
                framework_api=(Agent, BrowserSession, Tools, ActionResult),
                llm_factory=lambda model, logger: model,
                step_logger=lambda logger, history: None,
                chromium_args=[],
                workflow_timeout=0.01,
            )
        self.assertTrue(state["closed"])

    def test_playback_failure_fails_both_runners_without_retry(self):
        def failing_player(calls):
            def fail(task):
                calls.append(task)
                raise RuntimeError("playback failed")
            return fail

        for name, runner in (("browseruse", self.run_browser), ("smolagents", self.run_smol)):
            calls = []
            result, task, _state = runner(True, failing_player(calls))
            self.assertFalse(result.completed, name)
            self.assertEqual(calls, [task], name)

    def test_playback_action_has_no_replaceable_video_or_duration_inputs(self):
        _result, task, browser_state = self.run_browser(True, lambda task: True)
        action = browser_state["actions"]["play_assigned_video"]
        with self.assertRaises(TypeError):
            action("replacement-video", 1)

        _result, _task, smol_state = self.run_smol(True, lambda task: True)
        tool = smol_state["agent"]["tools"][0]
        self.assertEqual(tool.inputs, {})
        with self.assertRaises(TypeError):
            tool.forward("replacement-video", 1)

        calls = []
        playback = AssignedVideoPlayback(
            task, lambda assigned: calls.append(assigned) is None
        )
        self.assertEqual(playback.invoke(), "assigned playback completed")
        self.assertIn("only once", playback.invoke())
        self.assertEqual(calls, [task])
        self.assertFalse(playback.completed)


class LLMDocumentRunnerTests(unittest.TestCase):
    workspace = Path("/tmp/current-local-day")

    class Writer:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.calls = []

        def create(self, task, workspace):
            self.calls.append((task, workspace))
            if self.fail:
                raise RuntimeError("document creation failed")
            return WorkflowResult(
                completed=True,
                artifact=str(workspace / task.resource["filename"]),
            )

    @staticmethod
    def document_task(config_key):
        plan = load_control_plan(config_key)
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[2]
        )
        return plan, task

    def run_browser(self, invoke, writer):
        plan, task = self.document_task("browseruse-gpu")
        api, state = LLMVideoRunnerTests.browser_api(invoke)
        with patch("phase_workflow.brains._require_distribution"):
            result = browseruse_runner(
                task,
                self.workspace,
                plan.brain_profile,
                document_writer=writer,
                framework_api=api,
                llm_factory=lambda model, logger: (model, logger),
                step_logger=lambda logger, history: None,
                chromium_args=[],
            )
        return result, task, state

    def run_smol(self, invoke, writer):
        plan, task = self.document_task("smolagents-gpu")
        api, state = LLMVideoRunnerTests.smol_api(invoke)
        with patch("phase_workflow.brains._require_distribution"):
            result = smolagents_runner(
                task,
                self.workspace,
                plan.brain_profile,
                document_writer=writer,
                framework_api=api,
            )
        return result, task, state

    def test_both_runners_receive_exact_task_and_create_in_local_day_workspace(self):
        for name, runner in (
            ("browseruse", self.run_browser),
            ("smolagents", self.run_smol),
        ):
            writer = self.Writer()
            result, task, state = runner(True, writer)
            self.assertTrue(result.completed, name)
            self.assertEqual(writer.calls, [(task, self.workspace)], name)
            self.assertEqual(
                result.artifact,
                str(self.workspace / task.resource["filename"]),
                name,
            )
            raw_task = (
                state["agent"]["task"] if name == "browseruse" else state["task"]
            )
            self.assertEqual(raw_task, structured_llm_task(task), name)
            delivered = json.loads(raw_task)
            self.assertEqual(delivered["instruction"], task.instruction, name)
            self.assertEqual(delivered["resource_id"], task.resource_id, name)
            self.assertEqual(
                delivered["resource"],
                json.loads(structured_llm_task(task))["resource"],
                name,
            )

        _browser_result, browser_task, browser_state = self.run_browser(
            True, self.Writer()
        )
        self.assertEqual(
            set(browser_state["actions"]), {"create_assigned_document"}
        )
        self.assertFalse(browser_state["agent"]["directly_open_url"])
        document_evidence = browser_state["action_result"].extracted_content
        self.assertIn(f"resource_id={browser_task.resource_id}", document_evidence)
        self.assertIn(
            f"artifact={self.workspace / browser_task.resource['filename']}",
            document_evidence,
        )
        self.assertIn("format=document", document_evidence)
        smol_tools = self.run_smol(True, self.Writer())[2]["agent"]["tools"]
        self.assertEqual(len(smol_tools), 1)
        self.assertEqual(smol_tools[0].name, "create_assigned_document")
        self.assertEqual(smol_tools[0].inputs, {})
        smol_agent = self.run_smol(True, self.Writer())[2]["agent"]
        self.assertTrue(smol_agent["use_structured_outputs_internally"])
        self.assertIsNone(smol_agent["instructions"])

    def test_completion_without_document_action_invocation_fails(self):
        for name, runner in (
            ("browseruse", self.run_browser),
            ("smolagents", self.run_smol),
        ):
            writer = self.Writer()
            result, _task, _state = runner(False, writer)
            self.assertFalse(result.completed, name)
            self.assertIsNone(result.artifact, name)
            self.assertEqual(writer.calls, [], name)

    def test_smol_prose_and_malformed_code_cannot_complete_document(self):
        plan, task = self.document_task("smolagents-gpu")

        for claimed_result in (
            "The document was created successfully.",
            "<code >create_assigned_document()",
        ):
            api, state = LLMVideoRunnerTests.smol_api(False)
            code_agent = api[0]
            original_run = code_agent.run

            def run_without_tool(instance, raw_task, result=claimed_result):
                original_run(instance, raw_task)
                return result

            code_agent.run = run_without_tool
            writer = self.Writer()
            with patch("phase_workflow.brains._require_distribution"):
                result = smolagents_runner(
                    task,
                    self.workspace,
                    plan.brain_profile,
                    document_writer=writer,
                    framework_api=api,
                )
            self.assertFalse(result.completed)
            self.assertIsNone(result.artifact)
            self.assertEqual(writer.calls, [])

    def test_smol_repeated_document_tool_call_fails(self):
        plan, task = self.document_task("smolagents-gpu")
        api, state = LLMVideoRunnerTests.smol_api(False)
        code_agent = api[0]

        def invoke_twice(instance, raw_task):
            state["task"] = raw_task
            tool = state["agent"]["tools"][0]
            state["first"] = tool()
            try:
                tool()
            except Exception as exc:
                state["second_error"] = str(exc)
            return "claimed completion"

        code_agent.run = invoke_twice
        writer = self.Writer()
        with patch("phase_workflow.brains._require_distribution"):
            result = smolagents_runner(
                task,
                self.workspace,
                plan.brain_profile,
                document_writer=writer,
                framework_api=api,
            )
        self.assertFalse(result.completed)
        self.assertIsNone(result.artifact)
        self.assertEqual(writer.calls, [(task, self.workspace)])
        self.assertIn("only once", state["second_error"])

    def test_document_writer_failure_fails_both_runners_without_retry(self):
        for name, runner in (
            ("browseruse", self.run_browser),
            ("smolagents", self.run_smol),
        ):
            writer = self.Writer(fail=True)
            result, task, _state = runner(True, writer)
            self.assertFalse(result.completed, name)
            self.assertIsNone(result.artifact, name)
            self.assertEqual(writer.calls, [(task, self.workspace)], name)

    def test_document_action_is_parameter_free_immutable_and_single_use(self):
        _result, task, browser_state = self.run_browser(True, self.Writer())
        action = browser_state["actions"]["create_assigned_document"]
        with self.assertRaises(TypeError):
            action("replacement.odt", "replacement content")

        _result, _task, smol_state = self.run_smol(True, self.Writer())
        tool = smol_state["agent"]["tools"][0]
        self.assertEqual(tool.inputs, {})
        with self.assertRaises(TypeError):
            tool.forward("replacement.odt", "replacement content")

        assigned_resource = json.loads(structured_llm_task(task))["resource"]
        writer = self.Writer()
        document = AssignedDocumentCreation(task, self.workspace, writer)
        self.assertEqual(document.invoke(), "assigned document created")
        self.assertIn("only once", document.invoke())
        self.assertEqual(writer.calls, [(task, self.workspace)])
        self.assertEqual(
            json.loads(structured_llm_task(writer.calls[0][0]))["resource"],
            assigned_resource,
        )
        self.assertFalse(document.completed)
        self.assertFalse(document.result.completed)
        self.assertIsNone(document.result.artifact)

    def test_document_action_uses_existing_framework_log_callbacks(self):
        browser_source = (
            REPOSITORY_ROOT / "decoys/brains/browseruse/agent.py"
        ).read_text(encoding="utf-8")
        browser_tree = ast.parse(browser_source)
        mapping = next(
            ast.literal_eval(node.value)
            for node in browser_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_BU_ACTION_MAP"
                for target in node.targets
            )
        )
        self.assertEqual(
            mapping["create_assigned_document"],
            ("create_assigned_document", "office"),
        )

        from common.logging.llm_callbacks import _parse_smol_action

        parsed = _parse_smol_action("create_assigned_document()")
        self.assertEqual(parsed[:2], ("create_assigned_document", "office"))


class RuntimeTests(unittest.TestCase):
    def test_runtime_gpu_tier_selects_model_without_mutating_contract_profile(self):
        gpu_plan = load_control_plan("browseruse-gpu")
        for tier, expected_model in (
            ("v100", "gemma4:26b"),
            ("rtx", "gemma4:e4b"),
        ):
            with self.subTest(tier=tier), patch.dict(
                os.environ,
                {
                    "RUSE_WORKFLOW_GPU_TIER": tier,
                    "OLLAMA_MODEL": expected_model,
                },
                clear=True,
            ):
                profile, selected_tier, model = _select_runtime_brain_profile(
                    gpu_plan
                )
            self.assertEqual(selected_tier, tier)
            self.assertEqual(model, expected_model)
            self.assertEqual(profile["model"]["ollama"], expected_model)
        self.assertEqual(
            gpu_plan.brain_profile["model"]["ollama"], "gemma4:26b"
        )

        cpu_plan = load_control_plan("scripted-cpu")
        with patch.dict(
            os.environ, {"RUSE_WORKFLOW_GPU_TIER": "rtx"}, clear=True
        ):
            profile, tier, model = _select_runtime_brain_profile(cpu_plan)
        self.assertIs(profile, cpu_plan.brain_profile)
        self.assertEqual((tier, model), ("rtx", None))

    def test_runtime_records_rtx_tier_and_model_in_session_start(self):
        plan = load_control_plan("smolagents-gpu")
        logger = Mock()
        executor = Mock()
        with (
            patch.dict(
                os.environ,
                {
                    "RUSE_WORKFLOW_GPU_TIER": "rtx",
                    "OLLAMA_MODEL": "gemma4:e4b",
                },
                clear=True,
            ),
            patch("phase_workflow.runtime.load_workflow_plan", return_value=plan),
            patch("phase_workflow.runtime.AgentLogger", return_value=logger),
            patch("phase_workflow.runtime.build_brain") as build,
            patch("phase_workflow.runtime.WorkflowRegistry"),
            patch("phase_workflow.runtime.DailyExecutor", return_value=executor),
        ):
            run_workflow_runtime("smolagents-gpu", "/tmp/assigned")

        session_config = logger.session_start.call_args.kwargs["config"]
        self.assertEqual(session_config["gpu_tier"], "rtx")
        self.assertEqual(session_config["ollama_model"], "gemma4:e4b")
        self.assertEqual(
            build.call_args.args[1]["model"]["ollama"], "gemma4:e4b"
        )

    def test_invalid_startup_plan_never_constructs_runtime_or_falls_back(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "phase_workflow.runtime.load_workflow_plan",
            side_effect=WorkflowPlanError("invalid startup plan"),
        ) as loader, patch("phase_workflow.runtime.AgentLogger") as logger, patch(
            "phase_workflow.runtime.build_brain"
        ) as brain, patch("phase_workflow.runtime.DailyExecutor") as executor:
            with self.assertRaisesRegex(WorkflowPlanError, "invalid startup plan"):
                run_workflow_runtime("scripted-cpu", td)
        loader.assert_called_once_with(Path(td) / "behavior.json", "scripted-cpu")
        logger.assert_not_called()
        brain.assert_not_called()
        executor.assert_not_called()


class InstallerTests(unittest.TestCase):
    def test_canonical_mchp_runs_libreoffice_under_owned_window_manager(self):
        installer = INSTALLER.read_text(encoding="utf-8")
        mchp_packages = next(
            line
            for line in installer.splitlines()
            if "libreoffice" in line and "apt-get install" in line
        )
        self.assertIn(" openbox ", mchp_packages)
        self.assertIn(
            "openbox >/dev/null 2>&1 & exec", installer
        )

    def test_canonical_rtx_installer_reuses_gemmar_model_mapping(self):
        for config_key, expected_alias, expected_model in (
            ("scripted-cpu", "none", ""),
            ("mchp-cpu", "none", ""),
            ("browseruse-gpu", "gemmar", "gemma4:e4b"),
            ("smolagents-gpu", "gemmar", "gemma4:e4b"),
        ):
            with self.subTest(config_key=config_key):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        (
                            f'source "{INSTALLER}"; '
                            f'parse_config_key "{config_key}"; '
                            "RUSE_WORKFLOW_GPU_TIER=rtx; "
                            "export RUSE_WORKFLOW_GPU_TIER; "
                            "apply_phase_workflow_gpu_tier; "
                            'printf "%s|%s" "$MODEL" "${MODEL_NAMES[$MODEL]}"'
                        ),
                    ],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(
                    result.stdout, f"{expected_alias}|{expected_model}"
                )

    def test_installer_lists_and_registers_all_canonical_ids(self):
        result = subprocess.run(
            [str(INSTALLER), "--list"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        for config_key in EXPECTED_CONFIGS:
            self.assertIn("--" + config_key, result.stdout)

    def test_smolagents_install_includes_visit_webpage_dependency_smoke(self):
        installer = INSTALLER.read_text(encoding="utf-8")
        install_line = next(
            line for line in installer.splitlines()
            if "smolagents==1.25.0" in line and "pip install" in line
        )
        self.assertIn(" requests ", install_line)
        self.assertIn(" markdownify ", install_line)
        self.assertIn(" ddgs ", install_line)
        self.assertIn(" yt-dlp", install_line)
        self.assertIn(
            "import markdownify, requests; from smolagents import "
            "VisitWebpageTool; VisitWebpageTool()",
            installer,
        )

    def test_canonical_service_recreates_private_runtime_directory(self):
        installer = INSTALLER.read_text(encoding="utf-8")
        service_function = installer[
            installer.index("create_systemd_service() {"):
            installer.index("# Runner Mode (Direct Execution)")
        ]
        self.assertIn('if is_phase_workflow_config "$CONFIG_KEY"; then', service_function)
        self.assertIn(
            "RuntimeDirectory=ruse\\nRuntimeDirectoryMode=0700",
            service_function,
        )
        self.assertIn("$runtime_directory_directives", service_function)
        self.assertLess(
            service_function.index("$runtime_directory_directives"),
            service_function.index("StandardOutput="),
        )

    def test_canonical_browseruse_extends_only_browser_start_event_timeout(self):
        installer = INSTALLER.read_text(encoding="utf-8")
        run_script_function = installer[
            installer.index("create_run_script() {"):
            installer.index("create_systemd_service() {")
        ]
        self.assertIn(
            'is_phase_workflow_config "$CONFIG_KEY" && '
            '[[ "$BRAIN" == "browseruse" ]]',
            run_script_function,
        )
        self.assertIn(
            'export TIMEOUT_BrowserStartEvent="75"', run_script_function
        )
        self.assertNotIn("TIMEOUT_BrowserConnectedEvent", run_script_function)

    def test_installed_tree_contains_runtime_contract_behavior_and_sup_command(self):
        for config_key in sorted(EXPECTED_CONFIGS):
            with self.subTest(config_key=config_key), tempfile.TemporaryDirectory() as td:
                destination = Path(td)
                command = (
                    f'source "{INSTALLER}"; '
                    f'parse_config_key "{config_key}"; '
                    f'RUSE_WORKFLOW_BEHAVIOR_PATH="{CONTROL_ROOT}/{PLAN_FILENAMES[config_key]}"; '
                    "export RUSE_WORKFLOW_BEHAVIOR_PATH; "
                    'copy_source_code "$1"; create_run_script "$1"'
                )
                subprocess.run(
                    ["bash", "-c", command, "installer-test", td],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertTrue((destination / "decoys/phase_workflow").is_dir())
                self.assertTrue(
                    (destination / "contracts/phase-workflow-plan-v1").is_dir()
                )
                self.assertFalse(
                    (destination / "contracts/phase-workflow-plan-v1/controls").exists()
                )
                installed_behavior = (
                    destination / "behavioral_configurations/behavior.json"
                )
                self.assertEqual(
                    installed_behavior.read_bytes(),
                    (CONTROL_ROOT / PLAN_FILENAMES[config_key]).read_bytes(),
                )
                run_script = (destination / "run_agent.sh").read_text()
                self.assertIn(f"python3 -m sup {config_key}", run_script)
                self.assertIn(
                    f"--behavior-config-dir={td}/behavioral_configurations",
                    run_script,
                )
                if config_key == "mchp-cpu":
                    self.assertIn(
                        "openbox >/dev/null 2>&1 & exec", run_script
                    )


if __name__ == "__main__":
    unittest.main()
