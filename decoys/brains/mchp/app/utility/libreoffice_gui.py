"""Small X11 helpers for reliable, focused LibreOffice GUI workflows."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


WINDOW_TIMEOUT_S = 30.0
ARTIFACT_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.25


def wait_for_focused_window(
    title: str,
    *,
    process=None,
    artifact: Path | None = None,
    timeout_s: float = WINDOW_TIMEOUT_S,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> None:
    """Wait for the assigned LibreOffice window with bounded diagnostics."""
    from Xlib import X, display as xdisplay

    started_at = monotonic()
    deadline = monotonic() + timeout_s
    display = xdisplay.Display()
    observed_titles: set[str] = set()
    try:
        while monotonic() < deadline:
            exit_code = _process_exit_code(process)
            if exit_code is not None:
                raise RuntimeError(_readiness_error(
                    title,
                    process,
                    observed_titles,
                    monotonic() - started_at,
                    artifact,
                ))
            windows = _window_tree(display.screen().root)
            observed_titles.update(
                name for _window, name in windows if "libreoffice" in name.lower()
            )
            window = next(
                (window for window, name in windows if title in name), None
            )
            if window is not None:
                try:
                    window.configure(stack_mode=X.Above)
                    window.set_input_focus(X.RevertToParent, X.CurrentTime)
                    display.sync()
                    focused = display.get_input_focus().focus
                    if getattr(focused, "id", None) == window.id:
                        return
                except Exception:
                    pass
            sleeper(POLL_INTERVAL_S)
    finally:
        display.close()
    raise RuntimeError(_readiness_error(
        title,
        process,
        observed_titles,
        monotonic() - started_at,
        artifact,
    ))


def _window_tree(window) -> list[tuple[object, str]]:
    try:
        name = window.get_wm_name() or ""
        children = window.query_tree().children
    except Exception:
        return []
    windows = [(window, str(name))] if name else []
    for child in children:
        windows.extend(_window_tree(child))
    return windows


def _process_exit_code(process):
    poll = getattr(process, "poll", None)
    return poll() if poll is not None else None


def _readiness_error(
    expected_title: str,
    process,
    observed_titles: set[str],
    elapsed_s: float,
    artifact: Path | None,
) -> str:
    exit_code = _process_exit_code(process)
    process_state = "exited" if exit_code is not None else "running"
    if artifact is None:
        artifact_path = "none"
        artifact_exists = False
        artifact_size = 0
    else:
        artifact = Path(artifact)
        artifact_path = str(artifact)
        artifact_exists = artifact.is_file()
        artifact_size = artifact.stat().st_size if artifact_exists else 0
    return (
        "LibreOffice window readiness failed: "
        f"process_state={process_state} exit_code={exit_code} "
        f"expected_window={expected_title!r} "
        f"observed_titles={json.dumps(sorted(observed_titles))} "
        f"elapsed_seconds={elapsed_s:.3f} "
        f"expected_artifact={artifact_path} "
        f"artifact_exists={str(artifact_exists).lower()} "
        f"artifact_size={artifact_size}"
    )


def wait_for_stable_artifact(
    artifact: Path,
    *,
    timeout_s: float = ARTIFACT_TIMEOUT_S,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> None:
    """Require a nonempty artifact with unchanged size/mtime across two polls."""
    artifact = Path(artifact)
    deadline = monotonic() + timeout_s
    previous = None
    while monotonic() < deadline:
        if artifact.is_file():
            stat = artifact.stat()
            current = (stat.st_size, stat.st_mtime_ns)
            if stat.st_size > 0 and current == previous:
                return
            previous = current
        sleeper(POLL_INTERVAL_S)
    raise RuntimeError(f"LibreOffice did not produce a stable artifact: {artifact}")


def remove_profile(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)
