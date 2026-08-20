"""Create and close exact PHASE phase-run-v1 deployment records."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


UTC = timezone.utc
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}Z$")
EXPERIMENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts/phase-run-v1/phase-run-v1.schema.json"
)


class PhaseRunRegistryError(RuntimeError):
    """The deployment record could not be created, validated, or closed."""


def utc_deployment_start() -> datetime:
    """Return the one second-resolution UTC timestamp used for a new run."""
    return datetime.now(UTC).replace(microsecond=0)


def run_id_from_started_at(started_at: datetime) -> str:
    """Return the required readable UTC run ID for an aware start timestamp."""
    value = _aware_datetime(started_at, "started_at")
    return value.astimezone(UTC).strftime("%Y-%m-%d_%H%M%SZ")


def deployment_path(
    experiment_id: str,
    run_id: str,
    *,
    experiments_root: Path | None = None,
) -> Path:
    """Resolve one exact deployment record path after validating both IDs."""
    _validate_experiment_id(experiment_id)
    _validate_run_id(run_id)
    root = experiments_root or _default_experiments_root()
    return Path(root) / experiment_id / "runs" / run_id / "deployment.json"


def create_deployment(
    *,
    experiment_id: str,
    system: str,
    purpose: str,
    target: str | None,
    started_at: datetime,
    capture_interface: str,
    vms: Iterable[Mapping],
    experiments_root: Path | None = None,
) -> tuple[str, Path]:
    """Validate and atomically persist one new immutable deployment record."""
    started = _aware_datetime(started_at, "started_at")
    run_id = run_id_from_started_at(started)
    path = deployment_path(
        experiment_id, run_id, experiments_root=experiments_root
    )
    candidate = {
        "system": system,
        "purpose": purpose,
        "target": target,
        "started_at": _format_timestamp(started),
        "ended_at": None,
        "capture_interface": capture_interface,
        "vms": [dict(vm) for vm in vms],
    }
    _validate_record(candidate, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_create(path, _json_bytes(candidate))
    _validate_record(_read_record(path), run_id)
    return run_id, path


def close_deployment(
    experiment_id: str,
    run_id: str,
    *,
    ended_at: datetime,
    experiments_root: Path | None = None,
) -> Path:
    """Set ended_at on one exact run, preserving an existing close timestamp."""
    ended = _aware_datetime(ended_at, "ended_at")
    path = deployment_path(
        experiment_id, run_id, experiments_root=experiments_root
    )
    if not path.is_file():
        raise PhaseRunRegistryError(f"deployment record not found: {path}")

    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        current = _read_record(path)
        _validate_record(current, run_id)
        if current["ended_at"] is not None:
            return path
        started = _parse_timestamp(current["started_at"], "started_at")
        if ended < started:
            raise PhaseRunRegistryError("ended_at precedes started_at")
        candidate = dict(current)
        candidate["ended_at"] = _format_timestamp(ended)
        _validate_record(candidate, run_id)
        _atomic_replace(path, _json_bytes(candidate))
        _validate_record(_read_record(path), run_id)
        return path
    finally:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        finally:
            os.close(directory_fd)


def _default_experiments_root() -> Path:
    axes_root = Path(os.environ.get("PHASE_AXES_ROOT", "/data/axes-mirror"))
    return axes_root / "experiments"


def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise PhaseRunRegistryError(f"invalid phase-run-v1 contract: {exc}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_record(record: Mapping, run_id: str) -> None:
    try:
        _validator().validate(record)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "record"
        raise PhaseRunRegistryError(
            f"invalid deployment record at {location}: {exc.message}"
        ) from exc
    if record["system"] not in {"decoy", "rampart", "ghosts"}:
        raise PhaseRunRegistryError("RUSE system must be decoy, rampart, or ghosts")
    if record["purpose"] == "feedback" and record["target"] is None:
        raise PhaseRunRegistryError("feedback deployment target must not be null")
    if record["system"] in {"rampart", "ghosts"} and any(
        vm["sup_config"] is not None for vm in record["vms"]
    ):
        raise PhaseRunRegistryError(
            "Rampart and Ghosts VMs must have null sup_config"
        )
    started = _parse_timestamp(record["started_at"], "started_at")
    if run_id_from_started_at(started) != run_id:
        raise PhaseRunRegistryError("run_id does not match started_at")
    if record["ended_at"] is not None:
        ended = _parse_timestamp(record["ended_at"], "ended_at")
        if ended < started:
            raise PhaseRunRegistryError("ended_at precedes started_at")
    names = [vm["name"] for vm in record["vms"]]
    ips = [vm["ip"] for vm in record["vms"]]
    if len(names) != len(set(names)):
        raise PhaseRunRegistryError("deployment record contains duplicate VM names")
    if len(ips) != len(set(ips)):
        raise PhaseRunRegistryError("deployment record contains duplicate VM IPs")


def _validate_experiment_id(value: str) -> None:
    if not isinstance(value, str) or not EXPERIMENT_ID_RE.fullmatch(value):
        raise PhaseRunRegistryError(f"invalid experiment_id: {value!r}")


def _validate_run_id(value: str) -> None:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise PhaseRunRegistryError(f"invalid run_id: {value!r}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d_%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise PhaseRunRegistryError(f"invalid run_id: {value!r}") from exc
    if run_id_from_started_at(parsed) != value:
        raise PhaseRunRegistryError(f"invalid run_id: {value!r}")


def _aware_datetime(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PhaseRunRegistryError(f"{field} must be an offset-aware datetime")
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise PhaseRunRegistryError(f"{field} must be an offset-aware datetime") from exc
    if offset is None:
        raise PhaseRunRegistryError(f"{field} must be an offset-aware datetime")
    return value


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PhaseRunRegistryError(f"{field} must be an offset-aware timestamp") from exc
    return _aware_datetime(parsed, field)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(record: Mapping) -> bytes:
    return (json.dumps(record, indent=2) + "\n").encode("utf-8")


def _read_record(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseRunRegistryError(f"cannot read deployment record {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PhaseRunRegistryError("deployment record must be a JSON object")
    return value


def _atomic_create(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".deployment.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise PhaseRunRegistryError(f"deployment record already exists: {path}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _atomic_replace(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".deployment.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
