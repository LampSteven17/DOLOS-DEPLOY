"""Canonical fixed-resource workflow mechanics."""

from __future__ import annotations

import filecmp
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote_plus, urlsplit
from xml.etree import ElementTree

from phase_workflow.registry import ResolvedTask, WorkflowResult


SHARE_FQDN = "share.ruse.test"
SHARE_UNC = "//share.ruse.test/shared"
SHARE_PRINCIPAL = "ruse-share@RUSE.TEST"
SHARE_SERVICE_PRINCIPAL = "cifs/share.ruse.test@RUSE.TEST"
BOUNDED_NETWORK_COMMAND = (
    "timeout", "--signal=TERM", "--kill-after=3s", "30s",
)
SHARE_SEEDS = {
    "share_team_notes": "Team/meeting-notes.odt",
    "share_inventory": "Operations/inventory.ods",
    "share_project_status": "Projects/project-status.odt",
}
PRIVATE_LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua.\n"
)


@dataclass
class _DocumentState:
    path: Path
    order: int
    https_synced: bool = False
    share_uploaded: bool = False
    reservations: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class DocumentReservation:
    local_day: str
    channel: str
    path: Path


class DailyDocumentStore:
    """In-memory per-local-day document ownership and transfer state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._days: dict[str, list[_DocumentState]] = {}
        self._order = 0

    def register(self, local_day: str, path: Path) -> None:
        path = Path(path)
        with self._lock:
            records = self._days.setdefault(local_day, [])
            if any(record.path == path for record in records):
                return
            self._order += 1
            records.append(_DocumentState(path=path, order=self._order))

    def reserve(
        self, local_day: str, workspace: Path, channel: str, occurrence_id: str
    ) -> DocumentReservation:
        if channel not in {"https", "share"}:
            raise RuntimeError(f"unsupported document transfer channel: {channel}")
        with self._lock:
            records = self._days.setdefault(local_day, [])
            flag = "https_synced" if channel == "https" else "share_uploaded"
            eligible = [
                record for record in records
                if not getattr(record, flag) and not record.reservations
            ]
            if eligible:
                record = min(eligible, key=lambda item: item.order)
            else:
                path = Path(workspace) / f"private-lorem-{occurrence_id}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(PRIVATE_LOREM, encoding="utf-8")
                path.chmod(0o600)
                self._order += 1
                record = _DocumentState(path=path, order=self._order)
                records.append(record)
            record.reservations.add(channel)
            return DocumentReservation(local_day, channel, record.path)

    def complete(self, reservation: DocumentReservation) -> None:
        with self._lock:
            record = self._find(reservation)
            record.reservations.discard(reservation.channel)
            if reservation.channel == "https":
                record.https_synced = True
            else:
                record.share_uploaded = True

    def release(self, reservation: DocumentReservation) -> None:
        with self._lock:
            self._find(reservation).reservations.discard(reservation.channel)

    def state(self, local_day: str, path: Path) -> tuple[bool, bool, set[str]]:
        with self._lock:
            record = next(
                item for item in self._days.get(local_day, [])
                if item.path == Path(path)
            )
            return record.https_synced, record.share_uploaded, set(record.reservations)

    def _find(self, reservation: DocumentReservation) -> _DocumentState:
        for record in self._days.get(reservation.local_day, []):
            if record.path == reservation.path:
                return record
        raise RuntimeError("reserved document is no longer in the local-day workspace")


def stream_https_download(task: ResolvedTask, workspace: Path, requests_api=None) -> Path:
    """Stream the one immutable HTTPS resource and require its exact size."""
    if task.resource.get("kind") != "https_download":
        raise RuntimeError("FileDownload requires an https_download resource")
    url = task.resource["url"]
    expected = task.resource["expected_bytes"]
    name = Path(urlsplit(url).path).name or "download.bin"
    artifact = Path(workspace) / f"{task.occurrence_id}-{name}"
    if requests_api is None:
        import requests as requests_api
    response = requests_api.get(url, stream=True, timeout=(20, 360))
    try:
        response.raise_for_status()
        received = 0
        with artifact.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                handle.write(chunk)
                received += len(chunk)
        if received != expected or artifact.stat().st_size != expected:
            raise RuntimeError(
                f"assigned download size mismatch: expected {expected}, got {received}"
            )
    except Exception:
        artifact.unlink(missing_ok=True)
        raise
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()
    return artifact


def firefox_download(
    task: ResolvedTask,
    workspace: Path,
    driver_factory: Callable[[Path], object],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: int = 360,
) -> Path:
    """Download one assigned URL inside an occurrence-owned Firefox directory."""
    if task.resource.get("kind") != "https_download":
        raise RuntimeError("FileDownload requires an https_download resource")
    workspace = Path(workspace)
    if Path(task.occurrence_id).name != task.occurrence_id:
        raise RuntimeError("invalid FileDownload occurrence identity")
    download_dir = workspace / ".mchp-downloads" / task.occurrence_id
    download_dir.mkdir(parents=True, exist_ok=False)
    started_at = monotonic()
    deadline = monotonic() + timeout_seconds
    owner = None
    completed = False
    try:
        owner = driver_factory(download_dir)
        driver = getattr(owner, "driver", owner)
        navigation_error = None
        try:
            driver.execute_script(
                """
                const link = document.createElement('a');
                link.href = arguments[0];
                link.download = '';
                link.style.display = 'none';
                document.body.appendChild(link);
                link.click();
                link.remove();
                """,
                task.resource["url"],
            )
        except Exception as exc:
            if not _is_navigation_timeout(exc):
                raise
            navigation_error = exc
        while monotonic() < deadline:
            created = [path for path in download_dir.iterdir() if path.is_file()]
            partials = [
                path for path in created
                if path.name.endswith((".part", ".tmp"))
            ]
            candidates = [
                path for path in created
                if not path.name.endswith((".part", ".tmp"))
                and _matches_download_name(path, task.resource["url"])
            ]
            if len(candidates) > 1:
                raise RuntimeError(
                    "multiple assigned-name artifacts appeared in the "
                    "occurrence-owned download directory"
                )
            if len(candidates) == 1 and not partials:
                artifact = candidates[0]
                if artifact.stat().st_size == task.resource["expected_bytes"]:
                    completed = True
                    return artifact
            sleeper(0.25)
        detail = f": {navigation_error}" if navigation_error else ""
        raise RuntimeError(
            "assigned Firefox download did not complete at the exact size"
            + detail
        )
    except Exception as exc:
        created, sizes, partials = _download_observation(download_dir)
        elapsed = monotonic() - started_at
        raise RuntimeError(
            "assigned Firefox download failed: "
            f"resource_id={task.resource_id} url={task.resource['url']} "
            f"expected_bytes={task.resource['expected_bytes']} "
            f"created_files={json.dumps(created)} "
            f"observed_sizes={json.dumps(sizes, sort_keys=True)} "
            f"partial_files={json.dumps(partials)} "
            f"elapsed_seconds={elapsed:.3f} reason={exc}"
        ) from exc
    finally:
        cleanup = None
        if owner is not None:
            cleanup = getattr(owner, "cleanup", None) or getattr(
                owner, "quit", None
            )
        if cleanup is not None:
            cleanup()
        if not completed:
            shutil.rmtree(download_dir, ignore_errors=True)


def _is_navigation_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutException"


def _matches_download_name(path: Path, url: str) -> bool:
    expected = Path(urlsplit(url).path).name
    if not expected:
        return False
    if path.name == expected:
        return True
    expected_path = Path(expected)
    return re.fullmatch(
        re.escape(expected_path.stem)
        + r" ?\([1-9][0-9]*\)"
        + re.escape(expected_path.suffix),
        path.name,
    ) is not None


def _download_observation(
    download_dir: Path,
) -> tuple[list[str], dict[str, int], list[str]]:
    files = sorted(
        (path for path in download_dir.iterdir() if path.is_file()),
        key=lambda path: path.name,
    )
    names = [path.name for path in files]
    sizes = {path.name: path.stat().st_size for path in files}
    partials = [
        path.name for path in files if path.name.endswith((".part", ".tmp"))
    ]
    return names, sizes, partials


class HttpsDocumentSync:
    def __init__(self, documents: DailyDocumentStore, requests_api=None):
        self.documents = documents
        self.requests_api = requests_api

    def execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        if (
            task.resource.get("kind") != "https_upload"
            or task.resource.get("url") != "https://speed.cloudflare.com/__up"
        ):
            raise RuntimeError("FileSyncUpload requires cloudflare_upload")
        local_day = Path(workspace).name
        reservation = self.documents.reserve(
            local_day, workspace, "https", task.occurrence_id
        )
        try:
            payload = reservation.path.read_bytes()
            requests_api = self.requests_api
            if requests_api is None:
                import requests as requests_api
            response = requests_api.post(
                task.resource["url"],
                params={"bytes": len(payload)},
                data=payload,
                timeout=(20, 360),
            )
            try:
                response.raise_for_status()
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
            self.documents.complete(reservation)
            return WorkflowResult(completed=True, artifact=str(reservation.path))
        except Exception:
            self.documents.release(reservation)
            raise


class KerberosShareAccess:
    """One Kerberos-required bidirectional SMB action for the assigned seed."""

    def __init__(
        self,
        documents: DailyDocumentStore,
        *,
        runner=subprocess.run,
        keytab: Optional[str] = None,
        ccache: Optional[str] = None,
    ):
        self.documents = documents
        self.runner = runner
        self.keytab = keytab or os.environ.get(
            "RUSE_SHARE_KEYTAB", "/etc/ruse/ruse-share.keytab"
        )
        self.ccache = ccache or os.environ.get(
            "RUSE_SHARE_CCACHE", "/run/ruse/krb5cc_ruse_share"
        )

    def execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        assigned = SHARE_SEEDS.get(task.resource_id)
        if (
            task.resource.get("kind") != "kerberos_smb_share"
            or assigned is None
            or task.resource.get("path") != assigned
        ):
            raise RuntimeError("NetworkShareAccess requires one fixed assigned seed")
        workspace = Path(workspace)
        local_day = workspace.name
        reservation = self.documents.reserve(
            local_day, workspace, "share", task.occurrence_id
        )
        downloaded = workspace / f"{task.occurrence_id}-{Path(assigned).name}"
        roundtrip = workspace / f".{task.occurrence_id}-share-upload-roundtrip"
        remote_name = (
            f"Incoming/{task.sup_config}/{local_day}/"
            f"{task.occurrence_id}-{reservation.path.name}"
        )
        env = {**os.environ, "KRB5CCNAME": f"FILE:{self.ccache}"}
        upload_attempted = False
        try:
            self._run([
                *BOUNDED_NETWORK_COMMAND,
                "kinit", "-c", self.ccache, "-kt", self.keytab,
                SHARE_PRINCIPAL,
            ], env)
            parent = str(Path(assigned).parent)
            self._smb(f'ls "{parent}"', env)
            self._smb(f'get "{assigned}" "{downloaded}"', env)
            if not downloaded.is_file() or downloaded.stat().st_size <= 0:
                raise RuntimeError("assigned share seed was not downloaded")
            remote_directory = str(Path(remote_name).parent)
            try:
                upload_attempted = True
                self._smb(
                    f'mkdir "{remote_directory}"; '
                    f'put "{reservation.path}" "{remote_name}"',
                    env,
                )
                self._smb(f'get "{remote_name}" "{roundtrip}"', env)
                if not roundtrip.is_file() or not filecmp.cmp(
                    reservation.path, roundtrip, shallow=False
                ):
                    raise RuntimeError("remote share upload round-trip mismatch")
            finally:
                roundtrip.unlink(missing_ok=True)
                if upload_attempted:
                    self._smb(f'del "{remote_name}"', env)
            self.documents.complete(reservation)
            return WorkflowResult(completed=True, artifact=str(downloaded))
        except Exception:
            self.documents.release(reservation)
            raise

    def _smb(self, command: str, env: dict[str, str]) -> str:
        return self._run(
            [
                *BOUNDED_NETWORK_COMMAND,
                "smbclient", SHARE_UNC,
                "--use-kerberos=required", "--no-pass", "-c", command,
            ],
            env,
        )

    def _run(self, argv: list[str], env: dict[str, str]) -> str:
        completed = self.runner(
            argv, check=True, capture_output=True, text=True, env=env
        )
        return completed.stdout or ""


class SeleniumResourceWorkflows:
    """Execute fixed web/video resources with an injected WebDriver factory."""

    def __init__(self, driver_factory: Callable[[], object], sleeper=time.sleep):
        self._driver_factory = driver_factory
        self._sleep = sleeper

    def web_research(self, task: ResolvedTask) -> WorkflowResult:
        driver, cleanup = self._open_driver()
        try:
            kind = task.resource["kind"]
            if kind == "direct_url":
                driver.get(task.resource["url"])
                links = driver.find_elements(
                    "css selector", "#mw-content-text a[href]"
                )
            elif kind == "search_query":
                url = "https://www.google.com/search?q=" + quote_plus(
                    task.resource["query"]
                )
                driver.get(url)
                links = driver.find_elements("xpath", "//a[@href][.//h3]")
            else:
                raise RuntimeError(f"unsupported WebResearch resource kind: {kind}")
            for link in links:
                href = link.get_attribute("href")
                if isinstance(href, str) and href.startswith(("http://", "https://")):
                    driver.get(href)
                    return WorkflowResult(completed=True)
            raise RuntimeError("assigned research resource has no readable result link")
        finally:
            cleanup()

    def video_viewing(self, task: ResolvedTask) -> WorkflowResult:
        if task.resource["kind"] != "youtube_video":
            raise RuntimeError("VideoViewing requires a youtube_video resource")
        driver, cleanup = self._open_driver()
        try:
            driver.get(
                "https://www.youtube.com/watch?v=" + task.resource["video_id"]
            )
            video = driver.find_element("tag name", "video")
            driver.execute_script(
                "arguments[0].play();", video
            )
            self._sleep(task.resource["play_seconds"])
            return WorkflowResult(completed=True)
        finally:
            cleanup()

    def _open_driver(self):
        owner = self._driver_factory()
        driver = getattr(owner, "driver", owner)

        def cleanup():
            method = getattr(owner, "cleanup", None) or getattr(owner, "quit", None)
            if method is not None:
                method()

        return driver, cleanup


class OpenDocumentWriter:
    """Write the exact supplied ODT/ODS resource without generated content."""

    _MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">
<manifest:file-entry manifest:full-path="/" manifest:media-type="{media_type}"/>
<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>"""

    def create(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        kind = task.resource["kind"]
        artifact = workspace / task.resource["filename"]
        if kind == "document":
            media_type = "application/vnd.oasis.opendocument.text"
            body = self._document_body(task)
        elif kind == "spreadsheet":
            media_type = "application/vnd.oasis.opendocument.spreadsheet"
            body = self._spreadsheet_body(task)
        else:
            raise RuntimeError(f"unsupported DocumentCreation resource kind: {kind}")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("mimetype", media_type, compress_type=zipfile.ZIP_STORED)
            archive.writestr("content.xml", body)
            archive.writestr(
                "META-INF/manifest.xml",
                self._MANIFEST.format(media_type=media_type),
            )
        validate_open_document(task, workspace, artifact)
        return WorkflowResult(completed=True, artifact=str(artifact))

    @staticmethod
    def _document_body(task: ResolvedTask) -> str:
        paragraphs = [f"<text:h>{html.escape(str(task.resource['title']))}</text:h>"]
        for heading, values in task.resource["sections"].items():
            paragraphs.append(f"<text:h>{html.escape(str(heading))}</text:h>")
            paragraphs.extend(
                f"<text:p>{html.escape(str(value))}</text:p>" for value in values
            )
        return OpenDocumentWriter._content_xml(
            "office:text", "".join(paragraphs)
        )

    @staticmethod
    def _spreadsheet_body(task: ResolvedTask) -> str:
        rows = [task.resource["columns"], *task.resource["rows"]]
        rendered_rows = []
        for row in rows:
            cells = "".join(
                "<table:table-cell office:value-type=\"string\">"
                f"<text:p>{html.escape(str(value))}</text:p>"
                "</table:table-cell>"
                for value in row
            )
            rendered_rows.append(f"<table:table-row>{cells}</table:table-row>")
        table = "<table:table table:name=\"Sheet1\">" + "".join(rendered_rows) + "</table:table>"
        return OpenDocumentWriter._content_xml("office:spreadsheet", table)

    @staticmethod
    def _content_xml(body_tag: str, body: str) -> str:
        return (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<office:document-content "
            "xmlns:office=\"urn:oasis:names:tc:opendocument:xmlns:office:1.0\" "
            "xmlns:text=\"urn:oasis:names:tc:opendocument:xmlns:text:1.0\" "
            "xmlns:table=\"urn:oasis:names:tc:opendocument:xmlns:table:1.0\" "
            "office:version=\"1.3\"><office:body>"
            f"<{body_tag}>{body}</{body_tag}>"
            "</office:body></office:document-content>"
        )


class MCHPDocumentWorkflows:
    """Use MCHP's LibreOffice UI mechanics with an exact assigned resource."""

    def __init__(self, writer_factory=None, calc_factory=None, logger=None):
        self._writer_factory = writer_factory or self._load_writer
        self._calc_factory = calc_factory or self._load_calc
        self._logger = logger

    def create(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        kind = task.resource["kind"]
        if kind == "document":
            workflow = self._writer_factory()
        elif kind == "spreadsheet":
            workflow = self._calc_factory()
        else:
            raise RuntimeError(f"unsupported DocumentCreation resource kind: {kind}")
        expected_artifact = Path(workspace) / task.resource["filename"]
        expected_artifact.unlink(missing_ok=True)
        failures: list[str] = []
        for attempt in range(2):
            if attempt:
                workflow = (
                    self._writer_factory()
                    if kind == "document"
                    else self._calc_factory()
                )
            try:
                artifact = workflow.create_assigned(
                    task.resource, workspace, logger=self._logger
                )
                validate_open_document(task, workspace, artifact)
                return WorkflowResult(completed=True, artifact=str(artifact))
            except Exception as exc:
                artifact_exists = expected_artifact.is_file()
                artifact_size = (
                    expected_artifact.stat().st_size if artifact_exists else 0
                )
                failures.append(
                    f"attempt={attempt + 1} error={exc} "
                    f"expected_artifact={expected_artifact} "
                    f"artifact_exists={str(artifact_exists).lower()} "
                    f"artifact_size={artifact_size}"
                )
                expected_artifact.unlink(missing_ok=True)
                if attempt:
                    raise RuntimeError(
                        "assigned LibreOffice creation failed after one bounded "
                        "corrective attempt: " + " | ".join(failures)
                    ) from exc
            finally:
                workflow.cleanup()
        raise RuntimeError("assigned LibreOffice artifact was not created")

    @staticmethod
    def _load_writer():
        from brains.mchp.app.workflows.open_office_writer import DocumentEditor

        return DocumentEditor()

    @staticmethod
    def _load_calc():
        from brains.mchp.app.workflows.open_office_calc import SpreadsheetEditor

        return SpreadsheetEditor()


def structured_llm_task(task: ResolvedTask) -> str:
    """Keep the approved instruction and resolved resource as separate fields."""
    return json.dumps(
        {
            "instruction": task.instruction,
            "resource_id": task.resource_id,
            "resource": _plain(task.resource),
            "workflow": task.workflow,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def play_video_with_chromium(task: ResolvedTask) -> bool:
    """Play only the assigned video for its fixed duration in Chromium."""
    if task.resource["kind"] != "youtube_video":
        raise RuntimeError("VideoViewing requires a youtube_video resource")
    from playwright.sync_api import sync_playwright
    from brains.browseruse.config import CHROMIUM_ARGS

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            page = browser.new_page()
            page.goto(
                "https://www.youtube.com/watch?v=" + task.resource["video_id"],
                wait_until="domcontentloaded",
            )
            page.wait_for_selector("video")
            page.evaluate(
                "document.querySelector('video').play()"
            )
            page.wait_for_timeout(task.resource["play_seconds"] * 1000)
        finally:
            browser.close()
    return True


def play_video_realtime(
    task: ResolvedTask,
    media_resolver=None,
    process_runner=subprocess.run,
) -> bool:
    """Consume one assigned media stream at playback pace for its fixed duration."""
    if task.resource["kind"] != "youtube_video":
        raise RuntimeError("VideoViewing requires a youtube_video resource")
    video_id = task.resource["video_id"]
    duration = task.resource["play_seconds"]
    resolver = media_resolver or _resolve_media_url
    media_url = resolver(video_id)
    process_runner(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel", "error",
            "-re",
            "-i", media_url,
            "-t", str(duration),
            "-f", "null",
            "-",
        ],
        check=True,
        timeout=duration + 60,
    )
    return True


def _resolve_media_url(video_id: str) -> str:
    import yt_dlp

    with yt_dlp.YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }) as downloader:
        info = downloader.extract_info(
            "https://www.youtube.com/watch?v=" + video_id,
            download=False,
        )
    media_url = _select_media_url(info)
    if not media_url:
        raise RuntimeError(f"no media URL for assigned video {video_id}")
    return media_url


def _select_media_url(info) -> str | None:
    """Select one actually advertised HTTP media stream, without a format guess."""
    candidates = []
    if isinstance(info, dict):
        candidates.append(info)
        for key in ("requested_downloads", "requested_formats", "formats"):
            values = info.get(key) or []
            if isinstance(values, list):
                candidates.extend(value for value in values if isinstance(value, dict))

    def usable(candidate, *, require_audio):
        url = candidate.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
        if candidate.get("vcodec") == "none":
            return None
        if require_audio and candidate.get("acodec") == "none":
            return None
        return url

    for require_audio in (True, False):
        for candidate in candidates:
            url = usable(candidate, require_audio=require_audio)
            if url:
                return url
    return None


_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_COLUMN_REPEAT = f"{{{_TABLE_NS}}}number-columns-repeated"
_ROW_REPEAT = f"{{{_TABLE_NS}}}number-rows-repeated"


def validate_open_document(task: ResolvedTask, workspace: Path, artifact) -> None:
    """Fail unless an assigned ODT/ODS contains the exact supplied content."""
    expected_path = Path(workspace) / task.resource["filename"]
    artifact_path = Path(artifact)
    if artifact_path != expected_path:
        raise RuntimeError(
            f"document writer returned unexpected artifact: {artifact_path}"
        )

    kind = task.resource["kind"]
    expected = {
        "document": (".odt", "application/vnd.oasis.opendocument.text"),
        "spreadsheet": (".ods", "application/vnd.oasis.opendocument.spreadsheet"),
    }.get(kind)
    if expected is None or artifact_path.suffix != expected[0]:
        raise RuntimeError("assigned artifact has the wrong OpenDocument format")

    try:
        with zipfile.ZipFile(artifact_path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("assigned OpenDocument artifact is corrupt")
            if archive.read("mimetype").decode("utf-8") != expected[1]:
                raise RuntimeError("assigned OpenDocument mimetype is incorrect")
            content = archive.read("content.xml")
        root = ElementTree.fromstring(content)
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise RuntimeError(f"invalid assigned OpenDocument artifact: {exc}") from exc

    if kind == "document":
        _validate_document_content(root, task.resource)
    else:
        _validate_spreadsheet_content(root, task.resource)


def _validate_document_content(root, resource) -> None:
    actual = []
    for tag in (f"{{{_TEXT_NS}}}h", f"{{{_TEXT_NS}}}p"):
        actual.extend("".join(node.itertext()).strip() for node in root.iter(tag))
    expected = [str(resource["title"])]
    for heading, values in resource["sections"].items():
        expected.append(str(heading))
        expected.extend(str(value) for value in values)
    missing = [value for value in expected if value not in actual]
    if missing:
        raise RuntimeError(f"assigned ODT is missing supplied content: {missing[0]}")


def _validate_spreadsheet_content(root, resource) -> None:
    expected_rows = [
        [str(value) for value in resource["columns"]],
        *[[str(value) for value in row] for row in resource["rows"]],
    ]
    column_count = len(expected_rows[0])
    tables = list(root.iter(f"{{{_TABLE_NS}}}table"))
    if not tables:
        raise RuntimeError("assigned ODS contains no table")
    actual_rows = []
    for row in tables[0].iter(f"{{{_TABLE_NS}}}table-row"):
        values = []
        for cell in row:
            if cell.tag not in {
                f"{{{_TABLE_NS}}}table-cell",
                f"{{{_TABLE_NS}}}covered-table-cell",
            }:
                continue
            value = "".join(cell.itertext()).strip()
            repeat = int(cell.attrib.get(_COLUMN_REPEAT, "1"))
            values.extend([value] * min(repeat, column_count - len(values)))
            if len(values) >= column_count:
                break
        row_repeat = int(row.attrib.get(_ROW_REPEAT, "1"))
        actual_rows.extend([values] * min(row_repeat, len(expected_rows) - len(actual_rows)))
        if len(actual_rows) >= len(expected_rows):
            break
    if actual_rows != expected_rows:
        raise RuntimeError("assigned ODS cells do not match supplied content")


def _plain(value):
    if hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
