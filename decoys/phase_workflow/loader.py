"""Startup-only loader for ``phase-workflow-plan-v1``.

This module deliberately has no reload, snapshot, hash, or fallback API.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


CONTRACT_ROOT = (
    Path(__file__).resolve().parents[2] / "contracts" / "phase-workflow-plan-v1"
)
SCHEMA_PATH = CONTRACT_ROOT / "phase-workflow-plan-v1.schema.json"
CAPABILITIES_PATH = CONTRACT_ROOT / "capabilities-v1.json"


class WorkflowPlanError(RuntimeError):
    """A complete startup plan or installed capability is invalid."""


@dataclass(frozen=True)
class PlanEntry:
    offset_minutes: int
    workflow: str
    resource_id: str
    resource: Mapping[str, Any]
    brain_profile: str
    instruction: Optional[str]


@dataclass(frozen=True)
class PlanWindow:
    start_minute: int
    end_minute: int
    sequence: tuple[PlanEntry, ...]


@dataclass(frozen=True)
class WorkflowPlan:
    schema: str
    sup_config: str
    brain: str
    hardware: str
    target_profile: str
    timezone: ZoneInfo
    max_parallel: int
    brain_profile: Mapping[str, Any]
    windows: tuple[PlanWindow, ...]


def _load_json_bytes(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WorkflowPlanError(f"cannot read {label} {path}: {exc}") from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowPlanError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowPlanError(f"{label} {path} must contain a JSON object")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_target_profile(profile: dict[str, Any], profile_id: str) -> None:
    if profile.get("schema") != "phase-target-profile-v1":
        raise WorkflowPlanError("unsupported target-profile schema")
    if profile.get("id") != profile_id:
        raise WorkflowPlanError(f"target profile ID mismatch: {profile_id}")
    resources = profile.get("resources")
    if not isinstance(resources, dict) or not resources:
        raise WorkflowPlanError("target profile must contain resources")
    for resource_id, resource in resources.items():
        if not isinstance(resource, dict):
            raise WorkflowPlanError(f"resource {resource_id} must be an object")
        workflow = resource.get("workflow")
        kind = resource.get("kind")
        if workflow == "WebResearch" and kind == "direct_url":
            valid = bool(resource.get("url"))
        elif workflow == "WebResearch" and kind == "search_query":
            valid = resource.get("provider") == "google" and bool(resource.get("query"))
        elif workflow == "VideoViewing" and kind == "youtube_video":
            valid = bool(resource.get("video_id")) and resource.get("play_seconds") == 300
        elif workflow == "DocumentCreation" and kind == "document":
            valid = (
                str(resource.get("filename", "")).endswith(".odt")
                and bool(resource.get("title"))
                and bool(resource.get("sections"))
            )
        elif workflow == "DocumentCreation" and kind == "spreadsheet":
            columns = resource.get("columns")
            rows = resource.get("rows")
            valid = (
                str(resource.get("filename", "")).endswith(".ods")
                and isinstance(columns, list)
                and bool(columns)
                and isinstance(rows, list)
                and bool(rows)
                and all(isinstance(row, list) and len(row) == len(columns) for row in rows)
            )
        else:
            valid = False
        if not valid:
            raise WorkflowPlanError(f"resource {resource_id} has an unsupported shape")


def load_workflow_plan(
    behavior_path: Path,
    expected_sup_config: str,
    *,
    contract_root: Path = CONTRACT_ROOT,
) -> WorkflowPlan:
    """Read and fully validate one immutable plan exactly once."""
    behavior_path = Path(behavior_path)
    schema = _load_json_bytes(contract_root / SCHEMA_PATH.name, "workflow schema")
    capabilities = _load_json_bytes(
        contract_root / CAPABILITIES_PATH.name, "workflow capabilities"
    )
    document = _load_json_bytes(behavior_path, "behavior plan")

    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
    except (SchemaError, ValidationError) as exc:
        raise WorkflowPlanError(f"invalid workflow plan: {exc.message}") from exc

    configurations = capabilities.get("sup_configurations")
    if not isinstance(configurations, dict) or expected_sup_config not in configurations:
        raise WorkflowPlanError(
            f"unsupported SUP configuration: {expected_sup_config}"
        )
    if document["sup_config"] != expected_sup_config:
        raise WorkflowPlanError(
            "plan SUP configuration mismatch: "
            f"expected {expected_sup_config}, got {document['sup_config']}"
        )
    configuration = configurations[expected_sup_config]
    try:
        timezone = ZoneInfo(document["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise WorkflowPlanError(
            f"unknown IANA timezone: {document['timezone']}"
        ) from exc

    profiles = capabilities.get("target_profiles")
    profile_id = document["target_profile"]
    if not isinstance(profiles, dict) or profile_id not in profiles:
        raise WorkflowPlanError(f"unsupported target profile: {profile_id}")
    profile_ref = profiles[profile_id]
    if not isinstance(profile_ref, dict) or set(profile_ref) != {"path"}:
        raise WorkflowPlanError(f"invalid target profile registration: {profile_id}")
    profile = _load_json_bytes(contract_root / profile_ref["path"], "target profile")
    _validate_target_profile(profile, profile_id)
    resources = profile["resources"]

    capability_parallel = capabilities.get("max_parallel_workflows")
    if not isinstance(capability_parallel, int):
        raise WorkflowPlanError("invalid installed concurrency capability")
    if document["max_parallel"] > capability_parallel:
        raise WorkflowPlanError(
            "plan max_parallel exceeds installed capability: "
            f"{document['max_parallel']} > {capability_parallel}"
        )
    maximum_parallel = document["max_parallel"]

    previous_end = -1
    windows: list[PlanWindow] = []
    for window_index, raw_window in enumerate(document["schedule"]):
        start, end = raw_window["window_local"]
        if start >= end:
            raise WorkflowPlanError(
                f"window {window_index} must have positive duration"
            )
        if start < previous_end:
            raise WorkflowPlanError(
                f"window {window_index} overlaps or is out of order"
            )
        previous_end = end
        offsets = [entry["offset_minutes"] for entry in raw_window["sequence"]]
        if offsets != sorted(offsets):
            raise WorkflowPlanError(
                f"window {window_index} offsets are not nondecreasing"
            )
        if any(offset >= end - start for offset in offsets):
            raise WorkflowPlanError(
                f"window {window_index} contains an offset outside its window"
            )
        if max(Counter(offsets).values()) > maximum_parallel:
            raise WorkflowPlanError(
                f"window {window_index} exceeds the concurrency ceiling"
            )

        entries: list[PlanEntry] = []
        supported_workflows = configuration.get("workflows")
        if not isinstance(supported_workflows, dict):
            raise WorkflowPlanError(
                f"invalid workflow capabilities for {expected_sup_config}"
            )
        for raw_entry in raw_window["sequence"]:
            workflow = raw_entry["workflow"]
            allowed_profiles = supported_workflows.get(workflow)
            if allowed_profiles is None:
                raise WorkflowPlanError(f"unsupported workflow for SUP: {workflow}")
            brain_profile = raw_entry["brain"]["profile"]
            if brain_profile not in allowed_profiles:
                raise WorkflowPlanError(
                    f"unsupported profile {brain_profile!r} for {workflow}"
                )
            resource_id = raw_entry["resource_id"]
            if resource_id not in resources:
                raise WorkflowPlanError(f"unknown resource: {resource_id}")
            resource = resources[resource_id]
            if resource["workflow"] != workflow:
                raise WorkflowPlanError(
                    f"resource {resource_id} does not support {workflow}"
                )
            instruction = raw_entry["brain"].get("instruction")
            policy = configuration.get("instruction")
            if policy == "required" and instruction is None:
                raise WorkflowPlanError(f"{workflow} requires an instruction")
            if policy == "forbidden" and instruction is not None:
                raise WorkflowPlanError(f"{workflow} forbids an instruction")
            if brain_profile == "control" and policy == "required":
                expected_instruction = capabilities.get(
                    "control_instructions", {}
                ).get(workflow)
                if instruction != expected_instruction:
                    raise WorkflowPlanError(
                        f"{workflow} has the wrong control instruction"
                    )
            entries.append(PlanEntry(
                offset_minutes=raw_entry["offset_minutes"],
                workflow=workflow,
                resource_id=resource_id,
                resource=_freeze(resource),
                brain_profile=brain_profile,
                instruction=instruction,
            ))
        windows.append(PlanWindow(start, end, tuple(entries)))

    brain = configuration.get("brain")
    hardware = configuration.get("hardware")
    raw_profiles = capabilities.get("brain_profiles", {}).get(expected_sup_config, {})
    raw_brain_profile = raw_profiles.get("control", {})
    if brain in {"browseruse", "smolagents", "mchp"} and not raw_brain_profile:
        raise WorkflowPlanError(
            f"missing control Brain profile for {expected_sup_config}"
        )
    if brain == "mchp":
        kind_rules = raw_brain_profile.get("workflows", {})
        for window in windows:
            for entry in window.sequence:
                allowed_kinds = kind_rules.get(entry.workflow, {}).get(
                    "resource_kinds", []
                )
                if entry.resource["kind"] not in allowed_kinds:
                    raise WorkflowPlanError(
                        f"MCHP control does not support {entry.workflow} resource "
                        f"kind {entry.resource['kind']}"
                    )

    return WorkflowPlan(
        schema=document["schema"],
        sup_config=expected_sup_config,
        brain=str(brain),
        hardware=str(hardware),
        target_profile=profile_id,
        timezone=timezone,
        max_parallel=document["max_parallel"],
        brain_profile=_freeze(raw_brain_profile),
        windows=tuple(windows),
    )
