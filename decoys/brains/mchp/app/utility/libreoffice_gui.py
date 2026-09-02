"""Small X11 helpers for reliable, focused LibreOffice GUI workflows."""

from __future__ import annotations

import json
import os
import signal
import shutil
import time
from pathlib import Path


WINDOW_TIMEOUT_S = 30.0
ARTIFACT_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.25


def focus_editor_canvas(pyautogui_module, *, sleeper=time.sleep) -> None:
    """Move keyboard focus from the top-level window into its document canvas."""
    width, height = pyautogui_module.size()
    if width <= 0 or height <= 0:
        raise RuntimeError("LibreOffice editor has no usable display geometry")
    pyautogui_module.click(width // 2, height // 2)
    sleeper(POLL_INTERVAL_S)


def remove_artifact_sidecars(
    artifact: Path | None,
    *,
    preexisting_temp_files: set[Path] | None = None,
) -> None:
    """Remove only lock/temp files owned by one assigned-document invocation."""
    if artifact is None:
        return
    artifact = Path(artifact)
    artifact.with_name(f".~lock.{artifact.name}#").unlink(missing_ok=True)
    before = preexisting_temp_files or set()
    for path in artifact.parent.glob("lu*.tmp"):
        if path not in before:
            path.unlink(missing_ok=True)


def terminate_owned_process_group(
    process,
    *,
    timeout_s: float = 5.0,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> None:
    """Stop an isolated LibreOffice process group before removing its state."""
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if pid is None:
        _terminate_single_process(process, timeout_s)
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        _terminate_single_process(process, timeout_s)
        return

    deadline = monotonic() + timeout_s
    wait = getattr(process, "wait", None)
    if wait is not None:
        try:
            wait(timeout=min(1.0, max(0.0, deadline - monotonic())))
        except Exception:
            pass
    while monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        sleeper(POLL_INTERVAL_S)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if wait is not None:
        try:
            wait(timeout=max(0.0, deadline - monotonic()))
        except Exception:
            pass


def _terminate_single_process(process, timeout_s: float) -> None:
    try:
        process.terminate()
        wait = getattr(process, "wait", None)
        if wait is not None:
            wait(timeout=timeout_s)
    except Exception:
        kill = getattr(process, "kill", None)
        if kill is not None:
            try:
                kill()
            except Exception:
                pass


def wait_for_focused_window(
    title: str,
    *,
    process=None,
    artifact: Path | None = None,
    timeout_s: float = WINDOW_TIMEOUT_S,
    sleeper=time.sleep,
    monotonic=time.monotonic,
    blocking_dialog_action=None,
) -> None:
    """Wait for the assigned LibreOffice window with bounded diagnostics."""
    from Xlib import X, display as xdisplay

    started_at = monotonic()
    deadline = monotonic() + timeout_s
    display = xdisplay.Display()
    observed_titles: set[str] = set()
    observed_classes: set[str] = set()
    blocking_dialog_used = False
    try:
        while monotonic() < deadline:
            exit_code = _process_exit_code(process)
            if exit_code is not None:
                raise RuntimeError(_readiness_error(
                    title,
                    process,
                    observed_titles,
                    observed_classes,
                    monotonic() - started_at,
                    artifact,
                ))
            windows = _window_tree(display.screen().root)
            observed_titles.update(
                name for _window, name, _classes in windows
                if "libreoffice" in name.lower()
            )
            observed_classes.update(
                value
                for _window, _name, classes in windows
                for value in classes
                if "libreoffice" in value.lower()
            )
            if blocking_dialog_action is not None and not blocking_dialog_used:
                blocking_dialog = next(
                    (
                        window
                        for window, name, _classes in windows
                        if name.startswith("Tip of the Day")
                    ),
                    None,
                )
                if (
                    blocking_dialog is not None
                    and _focus_window(display, blocking_dialog, X)
                ):
                    blocking_dialog_action()
                    blocking_dialog_used = True
                    sleeper(POLL_INTERVAL_S)
                    continue
            window = next(
                (
                    window
                    for window, name, classes in windows
                    if title in name or _matches_office_class(title, classes)
                ),
                None,
            )
            if window is not None:
                if _focus_window(display, window, X):
                    return
            sleeper(POLL_INTERVAL_S)
    finally:
        display.close()
    raise RuntimeError(_readiness_error(
        title,
        process,
        observed_titles,
        observed_classes,
        monotonic() - started_at,
        artifact,
    ))


def _window_tree(window) -> list[tuple[object, str, tuple[str, ...]]]:
    try:
        name = window.get_wm_name() or ""
        get_wm_class = getattr(window, "get_wm_class", None)
        classes = tuple(str(value) for value in (get_wm_class() or ())) \
            if get_wm_class is not None else ()
        children = window.query_tree().children
    except Exception:
        return []
    windows = [(window, str(name), classes)] if name or classes else []
    for child in children:
        windows.extend(_window_tree(child))
    return windows


def _matches_office_class(title: str, classes: tuple[str, ...]) -> bool:
    expected = {
        "LibreOffice Writer": ("libreoffice-writer", "swriter"),
        "LibreOffice Calc": ("libreoffice-calc", "scalc"),
    }.get(title, ())
    lowered = tuple(value.lower() for value in classes)
    return any(marker in value for marker in expected for value in lowered)


def _focus_window(display, window, xlib) -> bool:
    try:
        window.configure(stack_mode=xlib.Above)
        window.set_input_focus(xlib.RevertToParent, xlib.CurrentTime)
        display.sync()
        focused = display.get_input_focus().focus
        return getattr(focused, "id", None) == window.id
    except Exception:
        return False


def _process_exit_code(process):
    poll = getattr(process, "poll", None)
    return poll() if poll is not None else None


def _readiness_error(
    expected_title: str,
    process,
    observed_titles: set[str],
    observed_classes: set[str],
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
        f"observed_classes={json.dumps(sorted(observed_classes))} "
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
