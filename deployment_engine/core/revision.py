"""Resolve the immutable RUSE revision used by DECOY VM installs."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUNTIME_PATHS = (
    "INSTALL_SUP.sh",
    "decoys",
    "deployment_engine/core",
    "deployment_engine/decoy",
    "deployment_engine/playbooks/decoy",
)


class RevisionError(RuntimeError):
    """Raised when a reproducible deploy revision cannot be selected."""


def resolve_ruse_revision(repo_root: Path) -> str:
    """Return the exact commit DECOY VMs must install.

    ``RUSE_GIT_REF`` may explicitly select a full commit SHA. Otherwise HEAD is
    used, but only when VM runtime sources are clean: uncommitted files cannot
    be represented by a Git revision and therefore cannot be reproduced on a
    newly cloned VM.
    """
    explicit = os.environ.get("RUSE_GIT_REF", "").strip().lower()
    if explicit:
        if not _SHA_RE.fullmatch(explicit):
            raise RevisionError(
                "RUSE_GIT_REF must be a full 40-character hexadecimal commit SHA"
            )
        return explicit

    root = Path(repo_root).resolve()
    revision = _git(root, "rev-parse", "HEAD").strip().lower()
    if not _SHA_RE.fullmatch(revision):
        raise RevisionError(f"git rev-parse HEAD returned invalid revision: {revision!r}")

    dirty = _git(root, "status", "--porcelain", "--", *_RUNTIME_PATHS).strip()
    if dirty:
        paths = ", ".join(line[3:] for line in dirty.splitlines()[:8])
        raise RevisionError(
            "VM runtime sources have uncommitted changes and cannot be pinned "
            f"to {revision[:12]} ({paths}). Commit them first, or explicitly "
            "select a published commit with RUSE_GIT_REF=<40-char-sha>."
        )
    return revision


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RevisionError(f"unable to query Git revision: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown git error").strip()
        raise RevisionError(detail)
    return result.stdout
