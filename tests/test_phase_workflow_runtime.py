from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from common.logging.agent_logger import AgentLogger
from phase_workflow.brains import (
    AssignedDocumentCreation,
    AssignedWebResearch,
    AssignedVideoPlayback,
    FrameworkBrain,
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
from phase_workflow.runtime import run_workflow_runtime
from phase_workflow.workflows import (
    MCHPDocumentWorkflows,
    OpenDocumentWriter,
    SeleniumResourceWorkflows,
    _select_media_url,
    play_video_realtime,
    structured_llm_task,
    validate_open_document,
)


CONTROL_ROOT = CONTRACT_ROOT / "controls"
REPOSITORY_ROOT = CONTRACT_ROOT.parents[1]
INSTALLER = REPOSITORY_ROOT / "INSTALL_SUP.sh"
EXPECTED_CONFIGS = {
    "scripted-cpu",
    "mchp-cpu",
    "browseruse-gpu",
    "smolagents-gpu",
}
EXPECTED_RESOURCES = (
    "wikipedia_compiler", "video_cpp_course", "document_team_meeting_notes",
    "google_climate_change_news", "video_ai_mistake",
    "spreadsheet_expense_tracker", "wikipedia_geometry", "video_vpn_dragnet",
    "document_project_status", "google_weather_today", "video_cronie_jobs",
    "spreadsheet_inventory_tracker", "wikipedia_deep_learning",
    "video_wrote_book", "document_training_outline",
    "google_recipes_for_dinner", "video_ps5_linux",
    "spreadsheet_work_schedule", "wikipedia_solar_power",
    "video_python_beginner", "document_incident_summary",
    "google_renewable_energy", "video_embedded_player",
    "spreadsheet_task_tracker", "wikipedia_python", "video_epic_sax",
    "document_weekly_planning", "google_running_shoes_review",
)


def control_document(config_key="scripted-cpu"):
    return json.loads(
        (CONTROL_ROOT / config_key / "behavior.json").read_text(encoding="utf-8")
    )


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
    def test_four_controls_load_with_exact_shape_order_and_policy(self):
        self.assertEqual(set(CONFIGURATIONS), EXPECTED_CONFIGS)
        plans = {
            key: load_workflow_plan(CONTROL_ROOT / key / "behavior.json", key)
            for key in EXPECTED_CONFIGS
        }
        for key, plan in plans.items():
            self.assertEqual(plan.sup_config, key)
            self.assertEqual(str(plan.timezone), "America/New_York")
            self.assertEqual(plan.max_parallel, 1)
            self.assertEqual(
                [(window.start_minute, window.end_minute) for window in plan.windows],
                [(540, 720), (780, 1020)],
            )
            entries = [entry for window in plan.windows for entry in window.sequence]
            self.assertEqual(tuple(entry.resource_id for entry in entries), EXPECTED_RESOURCES)
            self.assertEqual(
                [entry.workflow for entry in entries],
                [["WebResearch", "VideoViewing", "DocumentCreation"][i % 3]
                 for i in range(28)],
            )
            self.assertTrue(all(entry.brain_profile == "control" for entry in entries))
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
        profile["schedule"][0]["sequence"][0]["brain"]["profile"] = "unknown"
        mutations.append((profile, "scripted-cpu"))
        resource = control_document()
        resource["schedule"][0]["sequence"][0]["resource_id"] = "unknown"
        mutations.append((resource, "scripted-cpu"))
        for contribution in ("FileDownload", "FileSyncUpload", "NetworkShareAccess"):
            document = control_document()
            document["schedule"][0]["sequence"][0]["workflow"] = contribution
            mutations.append((document, "scripted-cpu"))
        for document, expected in mutations:
            with self.subTest(expected=expected, value=document["schedule"][0]["sequence"][0]["workflow"]):
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "behavior.json"
                    path.write_text(json.dumps(document))
                    with self.assertRaises(WorkflowPlanError):
                        load_workflow_plan(path, expected)

    def test_behavior_is_read_once_and_invalid_plan_has_no_fallback(self):
        path = CONTROL_ROOT / "scripted-cpu" / "behavior.json"
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
            (root / "target-profiles").mkdir()
            schema = json.loads(
                (CONTRACT_ROOT / "phase-workflow-plan-v1.schema.json").read_text()
            )
            capabilities = json.loads(
                (CONTRACT_ROOT / "capabilities-v1.json").read_text()
            )
            capabilities["max_parallel_workflows"] = 1
            target = json.loads(
                (CONTRACT_ROOT / "target-profiles/control-default.json").read_text()
            )
            (root / "phase-workflow-plan-v1.schema.json").write_text(
                json.dumps(schema)
            )
            (root / "capabilities-v1.json").write_text(json.dumps(capabilities))
            (root / "target-profiles/control-default.json").write_text(
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
                "window_index", "sequence_index", "workflow", "target_profile",
                "brain_profile", "scheduled_local", "scheduled_utc",
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
            document["schedule"][0]["sequence"][0]["brain"]["instruction"],
        )
        self.assertEqual(
            set(event),
            {
                "window_index", "sequence_index", "workflow", "target_profile",
                "brain_profile", "resolved_instruction", "scheduled_local",
                "scheduled_utc", "actual_start", "actual_end", "status",
            },
        )


class RegistryAndBrainTests(unittest.TestCase):
    def test_exact_canonical_registry_and_same_resolved_task(self):
        self.assertEqual(
            CANONICAL_HANDLERS,
            {"WebResearch", "VideoViewing", "DocumentCreation"},
        )
        resolved = []
        for key in ("scripted-cpu", "mchp-cpu", "browseruse-gpu", "smolagents-gpu"):
            plan = load_workflow_plan(CONTROL_ROOT / key / "behavior.json", key)
            brain = RecordingBrain()
            registry = WorkflowRegistry(plan, brain, Path("/tmp/workspace"))
            self.assertEqual(registry.workflows, CANONICAL_HANDLERS)
            resolved.append(registry.resolve(plan.windows[0].sequence[0]))
        self.assertTrue(all(task.resource_id == "wikipedia_compiler" for task in resolved))
        self.assertTrue(all(dict(task.resource) == dict(resolved[0].resource) for task in resolved))
        self.assertIsNone(resolved[0].instruction)
        self.assertIsNone(resolved[1].instruction)
        self.assertEqual(resolved[2].instruction, resolved[3].instruction)

    def test_llm_brain_receives_exact_instruction_and_resource(self):
        plan = load_workflow_plan(
            CONTROL_ROOT / "browseruse-gpu" / "behavior.json", "browseruse-gpu"
        )
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
        self.assertEqual(received[0][0].resource_id, "wikipedia_compiler")

    def test_llm_video_is_dispatched_through_the_framework_runner(self):
        plan = load_workflow_plan(
            CONTROL_ROOT / "browseruse-gpu" / "behavior.json", "browseruse-gpu"
        )
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

        plan = load_workflow_plan(
            CONTROL_ROOT / "scripted-cpu" / "behavior.json", "scripted-cpu"
        )
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
        plan = load_workflow_plan(
            CONTROL_ROOT / "scripted-cpu" / "behavior.json", "scripted-cpu"
        )
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
            plan = load_workflow_plan(
                CONTROL_ROOT / config_key / "behavior.json", config_key
            )
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
            runner.assert_called_once_with(
                task,
                Path("/tmp"),
                plan.brain_profile,
                None,
                video_player=player,
            )
            player.assert_not_called()

    def test_smol_video_consumes_one_stream_at_real_time_for_300_seconds(self):
        plan = load_workflow_plan(
            CONTROL_ROOT / "smolagents-gpu" / "behavior.json", "smolagents-gpu"
        )
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

        plan = load_workflow_plan(
            CONTROL_ROOT / "mchp-cpu" / "behavior.json", "mchp-cpu"
        )
        entries = [plan.windows[0].sequence[2], plan.windows[0].sequence[5]]
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

        plan = load_workflow_plan(
            CONTROL_ROOT / "mchp-cpu" / "behavior.json", "mchp-cpu"
        )
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

    def test_terminal_event_uses_ordinary_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            logger = AgentLogger("scripted-cpu", log_dir=td, session_id="plan")
            logger.workflow_plan_terminal({
                "window_index": 0,
                "sequence_index": 0,
                "workflow": "WebResearch",
                "target_profile": "control-default",
                "brain_profile": "control",
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


class TruthPropagationTests(unittest.TestCase):
    @staticmethod
    def browser_task():
        plan = load_workflow_plan(
            CONTROL_ROOT / "browseruse-gpu" / "behavior.json", "browseruse-gpu"
        )
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[1].sequence[6]
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

    @staticmethod
    def smol_task():
        plan = load_workflow_plan(
            CONTROL_ROOT / "smolagents-gpu" / "behavior.json", "smolagents-gpu"
        )
        task = WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[1].sequence[6]
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
        with patch("phase_workflow.brains._require_distribution"):
            result = smolagents_runner(
                task, Path("/tmp/day"), plan.brain_profile, framework_api=api
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
                return f"Solar power [Photovoltaics]({followup})"
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
    def task(sequence_index):
        plan = load_workflow_plan(
            CONTROL_ROOT / "scripted-cpu" / "behavior.json", "scripted-cpu"
        )
        return WorkflowRegistry(plan, RecordingBrain(), Path("/tmp")).resolve(
            plan.windows[0].sequence[sequence_index]
        )

    def test_exact_assigned_odt_and_ods_validate(self):
        for index in (2, 5):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as td:
                task = self.task(index)
                result = OpenDocumentWriter().create(task, Path(td))
                validate_open_document(task, Path(td), result.artifact)

    def test_corrupt_incomplete_and_wrong_format_artifacts_fail(self):
        task = self.task(5)
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

        document_task = self.task(2)
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


class MCHPDriverLifecycleTests(unittest.TestCase):
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

        plan = load_workflow_plan(
            CONTROL_ROOT / "mchp-cpu" / "behavior.json", "mchp-cpu"
        )
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
        plan = load_workflow_plan(
            CONTROL_ROOT / config_key / "behavior.json", config_key
        )
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
                state["action_timeout"] = os.environ.get(
                    "BROWSER_USE_ACTION_TIMEOUT_S"
                )

            async def run(self, max_steps):
                state["max_steps"] = max_steps
                actions = state["agent"]["tools"].registry.registry.actions
                state["actions"] = actions
                if invoke:
                    action_name, action = next(iter(actions.items()))
                    state["action_name"] = action_name
                    state["action_result"] = action()
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

        browser_state = self.run_browser(True, lambda task: True)[2]
        browser_actions = browser_state["actions"]
        self.assertEqual(set(browser_actions), {"play_assigned_video"})
        self.assertFalse(browser_state["agent"]["directly_open_url"])
        self.assertEqual(browser_state["agent"]["step_timeout"], 360)
        self.assertEqual(os.environ["BROWSER_USE_ACTION_TIMEOUT_S"], "360")
        smol_tools = self.run_smol(True, lambda task: True)[2]["agent"]["tools"]
        self.assertEqual(len(smol_tools), 1)
        self.assertEqual(smol_tools[0].name, "play_assigned_video")

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
        self.assertEqual(state["action_timeout"], "360")

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
        plan = load_workflow_plan(
            CONTROL_ROOT / config_key / "behavior.json", config_key
        )
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
            delivered = json.loads(raw_task)
            self.assertEqual(delivered["instruction"], task.instruction, name)
            self.assertEqual(delivered["resource_id"], task.resource_id, name)
            self.assertEqual(
                delivered["resource"],
                json.loads(structured_llm_task(task))["resource"],
                name,
            )

        browser_state = self.run_browser(True, self.Writer())[2]
        self.assertEqual(
            set(browser_state["actions"]), {"create_assigned_document"}
        )
        self.assertFalse(browser_state["agent"]["directly_open_url"])
        smol_tools = self.run_smol(True, self.Writer())[2]["agent"]["tools"]
        self.assertEqual(len(smol_tools), 1)
        self.assertEqual(smol_tools[0].name, "create_assigned_document")

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

    def test_installed_tree_contains_runtime_contract_behavior_and_sup_command(self):
        for config_key in sorted(EXPECTED_CONFIGS):
            with self.subTest(config_key=config_key), tempfile.TemporaryDirectory() as td:
                destination = Path(td)
                command = (
                    f'source "{INSTALLER}"; '
                    f'parse_config_key "{config_key}"; '
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
                installed_behavior = (
                    destination / "behavioral_configurations/behavior.json"
                )
                self.assertEqual(
                    installed_behavior.read_bytes(),
                    (CONTROL_ROOT / config_key / "behavior.json").read_bytes(),
                )
                run_script = (destination / "run_agent.sh").read_text()
                self.assertIn(f"python3 -m sup {config_key}", run_script)
                self.assertIn(
                    f"--behavior-config-dir={td}/behavioral_configurations",
                    run_script,
                )


if __name__ == "__main__":
    unittest.main()
