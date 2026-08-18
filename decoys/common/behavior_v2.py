"""Strict loader and immutable snapshot for the DECOY behavior V2 contract.

R1 validates and normalizes the accepted PHASE document.  It intentionally
does not connect V2 budgets to emitters; scheduling, ledgers, and unified
gating remain R2/R3 work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from jsonschema import Draft202012Validator, FormatChecker, ValidationError


BEHAVIOR_CONTRACT_V2 = "ruse.decoy.behavior/v2"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "decoy" / "behavior-v2.schema.json"
CAPABILITY_PATH = SCHEMA_PATH.with_name("capabilities-v2.json")

SHARED_WORKFLOWS = frozenset(
    {"BrowseWeb", "BrowseYouTube", "WebSearch", "DownloadFiles", "WhoisLookup"}
)
MCHP_WORKFLOWS = frozenset(
    {
        *SHARED_WORKFLOWS,
        "DocumentEditor",
        "SpreadsheetEditor",
        "ExecuteCommand",
        "ListFiles",
    }
)
WORKFLOWS_BY_BRAIN = MappingProxyType(
    {
        "browseruse": SHARED_WORKFLOWS,
        "smolagents": SHARED_WORKFLOWS,
        "mchp": MCHP_WORKFLOWS,
    }
)

SIDECAR_LEGACY_KEYS = MappingProxyType(
    {
        "smb": "inbound_smb_per_hour",
        "ldap": "inbound_ldap_per_hour",
        "wsus": "inbound_wsus_per_hour",
        "printer": "inbound_printer_per_hour",
        "ipmi": "inbound_ipmi_per_hour",
        "winrm": "inbound_winrm_per_hour",
        "ntp_receive": "inbound_ntp_receive_per_hour",
        "mdns": "inbound_mdns_per_hour",
        "ssdp": "inbound_ssdp_per_hour",
        "scan": "inbound_scan_per_hour",
    }
)


class BehaviorV2Error(RuntimeError):
    """A V2 document is malformed, incompatible, or semantically invalid."""


class BehaviorV2StaticChangeError(BehaviorV2Error):
    """A hot replacement attempted to change startup/deployment identity."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _json_pointer(error: ValidationError) -> str:
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path]
    return "/" + "/".join(parts) if parts else "/"


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BehaviorV2Error(f"cannot load frozen V2 schema {SCHEMA_PATH}: {exc}") from exc
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@lru_cache(maxsize=1)
def _capabilities_by_sup() -> Mapping[str, tuple[str, str]]:
    try:
        registry = json.loads(CAPABILITY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BehaviorV2Error(
            f"cannot load pinned V2 capability registry {CAPABILITY_PATH}: {exc}"
        ) from exc
    capabilities: dict[str, tuple[str, str]] = {}
    for group in registry.get("sup_capabilities", []):
        capability = (group.get("brain"), group.get("hardware_class"))
        for config_key in group.get("config_keys", []):
            if config_key in capabilities:
                raise BehaviorV2Error(
                    f"duplicate SUP key in V2 capability registry: {config_key!r}"
                )
            capabilities[config_key] = capability
    if not capabilities:
        raise BehaviorV2Error("pinned V2 capability registry is empty")
    return MappingProxyType(capabilities)


def infer_runtime_capability(config_key: str) -> tuple[str, str]:
    """Return the exact capability pinned for an installed SUP key."""
    try:
        return _capabilities_by_sup()[config_key]
    except KeyError as exc:
        raise BehaviorV2Error(
            f"unsupported V2 SUP config key in pinned capability registry: {config_key!r}"
        ) from exc


def _eligible_pre_fence_seconds(document: dict) -> list[int]:
    eligible = [0] * 24
    fence = document["activity"]["start_fence_seconds"]
    for start_minute, end_minute in document["activity"]["windows_utc"]:
        start_second = start_minute * 60
        end_second = end_minute * 60 - fence
        for hour in range(24):
            hour_start = hour * 3600
            hour_end = (hour + 1) * 3600
            eligible[hour] += max(
                0,
                min(end_second, hour_end) - max(start_second, hour_start),
            )
    return eligible


def _validate_budget(
    path: str,
    series: list,
    eligible_seconds: list[int],
    *,
    required: bool,
) -> None:
    positive_hours = {hour for hour, rate in enumerate(series) if Decimal(str(rate)) > 0}
    eligible_hours = {hour for hour, seconds in enumerate(eligible_seconds) if seconds > 0}
    outside = sorted(positive_hours - eligible_hours)
    if outside:
        raise BehaviorV2Error(
            f"{path} has positive budgets outside eligible pre-fence UTC hours: {outside}"
        )
    if required and not positive_hours:
        raise BehaviorV2Error(f"{path} must contain an eligible positive budget")
    if required or positive_hours:
        credit = sum(
            (
                Decimal(str(rate))
                * Decimal(eligible_seconds[hour])
                / Decimal(3600)
                for hour, rate in enumerate(series)
            ),
            Decimal(0),
        )
        if credit < 1:
            raise BehaviorV2Error(
                f"{path} accrues only {credit} eligible tokens per UTC day; minimum is 1"
            )


def _require_monotonic(path: str, distribution: dict, keys: tuple[str, ...]) -> None:
    values = [Decimal(str(distribution[key])) for key in keys]
    if values != sorted(values):
        raise BehaviorV2Error(f"{path} must be monotonic across {keys}")


def _validate_semantics(document: dict) -> None:
    execution = document["execution"]
    channels = document["channels"]
    enabled = set(execution["enabled_workflows"])
    budgeted = set(channels["workflows"]["starts_per_utc_hour"])
    if enabled != budgeted:
        raise BehaviorV2Error(
            "execution.enabled_workflows must exactly equal workflow budget keys"
        )

    windows = document["activity"]["windows_utc"]
    fence = document["activity"]["start_fence_seconds"]
    if windows != sorted(windows):
        raise BehaviorV2Error("activity.windows_utc must be sorted")
    previous_end = -1
    for start, end in windows:
        if start < previous_end:
            raise BehaviorV2Error("activity.windows_utc must not overlap")
        if (end - start) * 60 <= fence:
            raise BehaviorV2Error(
                "every activity window must contain time before start_fence_seconds"
            )
        previous_end = end

    eligible_seconds = _eligible_pre_fence_seconds(document)
    if not any(eligible_seconds):
        raise BehaviorV2Error("activity has no eligible pre-fence time")

    for workflow, series in channels["workflows"]["starts_per_utc_hour"].items():
        _validate_budget(
            f"channels.workflows.starts_per_utc_hour.{workflow}",
            series,
            eligible_seconds,
            required=True,
        )

    cluster = channels["workflows"]["cluster"]
    percentile_keys = ("p05", "p25", "p50", "p75", "p95", "max")
    for name, distribution in cluster.items():
        _require_monotonic(f"channels.workflows.cluster.{name}", distribution, percentile_keys)

    for channel_name in ("background", "scripted"):
        channel = channels[channel_name]
        for action, series in channel["actions_per_utc_hour"].items():
            _validate_budget(
                f"channels.{channel_name}.actions_per_utc_hour.{action}",
                series,
                eligible_seconds,
                required=channel["enabled"],
            )

    persistent = channels["persistent_sessions"]
    _validate_budget(
        "channels.persistent_sessions.opens_per_utc_hour",
        persistent["opens_per_utc_hour"],
        eligible_seconds,
        required=persistent["enabled"],
    )
    _validate_budget(
        "channels.persistent_sessions.keepalives_per_utc_hour",
        persistent["keepalives_per_utc_hour"],
        eligible_seconds,
        required=False,
    )

    floor = channels["shape_floor"]
    _validate_budget(
        "channels.shape_floor.opens_per_utc_hour",
        floor["opens_per_utc_hour"],
        eligible_seconds,
        required=floor["enabled"],
    )

    shape = document["shape"]
    if shape["enabled"]:
        if not (persistent["enabled"] or floor["enabled"]):
            raise BehaviorV2Error(
                "shape.enabled requires persistent_sessions or shape_floor"
            )
        shape_keys = ("p25", "p50", "p75", "p90", "max")
        _require_monotonic("shape.orig_bytes", shape["orig_bytes"], shape_keys)
        _require_monotonic("shape.duration_seconds", shape["duration_seconds"], shape_keys)

    brain = document["brain"]
    if "page_dwell_seconds" in brain:
        dwell = brain["page_dwell_seconds"]
        if Decimal(str(dwell["min"])) > Decimal(str(dwell["max"])):
            raise BehaviorV2Error("brain.page_dwell_seconds.min must be <= max")
    if "download_outcome_weights" in brain:
        total = sum(
            (Decimal(str(value)) for value in brain["download_outcome_weights"].values()),
            Decimal(0),
        )
        if abs(total - Decimal(1)) > Decimal("1e-9"):
            raise BehaviorV2Error("brain.download_outcome_weights must sum to 1")
    per_workflow = (brain.get("max_steps") or {}).get("per_workflow", {})
    unknown_steps = sorted(set(per_workflow) - enabled)
    if unknown_steps:
        raise BehaviorV2Error(
            f"brain.max_steps.per_workflow names disabled workflows: {unknown_steps}"
        )


def _validate_capability(document: dict, config_key: str) -> None:
    metadata = document["_metadata"]
    execution = document["execution"]
    if metadata["sup_config"] != config_key:
        raise BehaviorV2Error(
            f"_metadata.sup_config={metadata['sup_config']!r} does not match installed {config_key!r}"
        )
    expected_brain, expected_hardware = infer_runtime_capability(config_key)
    if execution["brain"] != expected_brain:
        raise BehaviorV2Error(
            f"execution.brain={execution['brain']!r} does not match installed {expected_brain!r}"
        )
    if execution["hardware_class"] != expected_hardware:
        raise BehaviorV2Error(
            "execution.hardware_class="
            f"{execution['hardware_class']!r} does not match installed {expected_hardware!r}"
        )
    unsupported = sorted(
        set(execution["enabled_workflows"]) - WORKFLOWS_BY_BRAIN[expected_brain]
    )
    if unsupported:
        raise BehaviorV2Error(
            f"execution.enabled_workflows unsupported by {expected_brain}: {unsupported}"
        )


def _static_identity(document: dict) -> tuple[tuple[str, str], ...]:
    metadata = document["_metadata"]
    execution = document["execution"]
    values = {
        "_metadata.contract_version": metadata["contract_version"],
        "_metadata.sup_config": metadata["sup_config"],
        "_metadata.seed": metadata["seed"],
        "_metadata.producer": metadata["producer"],
        "_metadata.target_dataset": metadata["target_dataset"],
        "execution.driver": execution["driver"],
        "execution.brain": execution["brain"],
        "execution.hardware_class": execution["hardware_class"],
        "execution.enabled_workflows": execution["enabled_workflows"],
        "sidecar": document["sidecar"],
    }
    return tuple(
        (path, json.dumps(value, sort_keys=True, separators=(",", ":")))
        for path, value in values.items()
    )


@dataclass(frozen=True)
class BehaviorV2Snapshot:
    """Fully validated, recursively immutable V2 runtime candidate."""

    raw_sha256: str
    document: Mapping[str, Any]
    static_identity: tuple[tuple[str, str], ...]
    sidecar_legacy_rates: Mapping[str, Any]

    @property
    def contract_version(self) -> str:
        return BEHAVIOR_CONTRACT_V2

    @property
    def seed(self) -> int:
        return int(self.document["_metadata"]["seed"])

    @property
    def enabled_workflows(self) -> tuple[str, ...]:
        return tuple(self.document["execution"]["enabled_workflows"])

    @property
    def brain(self) -> str:
        return str(self.document["execution"]["brain"])


def load_behavior_v2_bytes(
    raw_bytes: bytes,
    config_key: str,
    *,
    previous: Optional[BehaviorV2Snapshot] = None,
) -> BehaviorV2Snapshot:
    """Validate bytes completely and return a new immutable candidate."""
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BehaviorV2Error(f"V2 behavior.json is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise BehaviorV2Error("V2 behavior.json root must be an object")
    version = (document.get("_metadata") or {}).get("contract_version")
    if version != BEHAVIOR_CONTRACT_V2:
        raise BehaviorV2Error(
            f"expected contract {BEHAVIOR_CONTRACT_V2!r}, got {version!r}"
        )
    try:
        _validator().validate(document)
    except ValidationError as exc:
        raise BehaviorV2Error(
            f"V2 schema validation failed at {_json_pointer(exc)}: {exc.message}"
        ) from exc
    _validate_semantics(document)
    _validate_capability(document, config_key)
    static_identity = _static_identity(document)
    if previous is not None and static_identity != previous.static_identity:
        old = dict(previous.static_identity)
        new = dict(static_identity)
        changed = sorted(path for path in new if new[path] != old.get(path))
        raise BehaviorV2StaticChangeError(
            "V2 hot reload changes startup/deployment field(s); restart/redeploy required: "
            + ", ".join(changed)
        )
    topology = document["sidecar"]["topology_inbound"]
    sidecar_legacy_rates = {
        SIDECAR_LEGACY_KEYS[key]: value
        for key, value in topology["probes_per_hour"].items()
    }
    return BehaviorV2Snapshot(
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        document=_freeze(document),
        static_identity=static_identity,
        sidecar_legacy_rates=MappingProxyType(sidecar_legacy_rates),
    )


class BehaviorV2ReloadManager:
    """Own the atomic V2 snapshot pointer and authoritative raw-byte digest."""

    def __init__(self, config_key: str):
        self.config_key = config_key
        self.current: Optional[BehaviorV2Snapshot] = None

    @property
    def raw_sha256(self) -> Optional[str]:
        return self.current.raw_sha256 if self.current is not None else None

    def activate_bytes(self, raw_bytes: bytes) -> bool:
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest == self.raw_sha256:
            return False
        candidate = load_behavior_v2_bytes(
            raw_bytes,
            self.config_key,
            previous=self.current,
        )
        self.current = candidate
        return True

    def reload(self, path: Path) -> bool:
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise BehaviorV2Error(f"cannot read V2 behavior.json at {path}: {exc}") from exc
        return self.activate_bytes(raw_bytes)
