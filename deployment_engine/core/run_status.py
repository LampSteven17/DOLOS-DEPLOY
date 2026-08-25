"""Per-run deploy outcome stamp.

Written by spinup on its exit paths so `./teardown --failed` can target
broken runs deterministically, instead of guessing from which artifact
files happen to exist in the run dir. The guess-from-artifacts approach
misreads a run that provisioned all its VMs but died at a later step
(e.g. a RAMPART deploy that failed AD DC promotion) as "healthy" — it
has deploy-output.json but no post-deploy-output.json — which is exactly
the kind of silent misclassification the fail-loud contract forbids for
a destructive operation.

Contract: spinup stamps FAILED the moment the run dir is created, then
flips it to OK only if it reaches its final return. Any early return,
exception, or hard kill therefore leaves the run stamped FAILED — the
safe default for a teardown filter. A run with no stamp at all (deploys
predating this instrumentation, or a type whose spinup isn't yet wired)
reads as UNKNOWN and is NOT matched by --failed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STATUS_FILE = "deploy_status.json"

OK = "ok"
FAILED = "failed"
CLEANED = "cleaned"
UNKNOWN = "unknown"


def write_run_status(run_dir: Path, status: str, detail: str = "") -> None:
    """Stamp {run_dir}/deploy_status.json. No-op if run_dir doesn't exist.

    Atomic via temp-file rename so a crash mid-write never leaves a
    truncated stamp that read_run_status would treat as UNKNOWN.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return
    payload = {
        "status": status,
        "detail": detail,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = run_dir / STATUS_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def read_run_status(run_dir: Path) -> str:
    """Return OK / FAILED / CLEANED / UNKNOWN for a run dir.

    UNKNOWN covers both "no stamp" and "unreadable stamp" — either way the
    run's outcome is undetermined and --failed leaves it alone.
    """
    path = Path(run_dir) / STATUS_FILE
    if not path.exists():
        return UNKNOWN
    try:
        return json.loads(path.read_text()).get("status", UNKNOWN)
    except (OSError, json.JSONDecodeError):
        return UNKNOWN
