"""Small X11 helpers for reliable, focused LibreOffice GUI workflows."""

from __future__ import annotations

import shutil
import time
from pathlib import Path


WINDOW_TIMEOUT_S = 30.0
ARTIFACT_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.25


def wait_for_focused_window(
    title: str,
    *,
    timeout_s: float = WINDOW_TIMEOUT_S,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> None:
    """Wait for an exact LibreOffice application window and focus it via X11."""
    from Xlib import X, display as xdisplay

    deadline = monotonic() + timeout_s
    display = xdisplay.Display()
    try:
        while monotonic() < deadline:
            window = _find_window(display.screen().root, title)
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
    raise RuntimeError(f"LibreOffice window did not become ready and focused: {title}")


def _find_window(window, title: str):
    try:
        name = window.get_wm_name() or ""
        if title in str(name):
            return window
        children = window.query_tree().children
    except Exception:
        return None
    for child in children:
        match = _find_window(child, title)
        if match is not None:
            return match
    return None


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
