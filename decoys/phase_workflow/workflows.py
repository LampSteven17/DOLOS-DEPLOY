"""Canonical fixed-resource workflow mechanics."""

from __future__ import annotations

import html
import json
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus
from xml.etree import ElementTree

from phase_workflow.registry import ResolvedTask, WorkflowResult


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
        try:
            artifact = workflow.create_assigned(
                task.resource, workspace, logger=self._logger
            )
        finally:
            workflow.cleanup()
        validate_open_document(task, workspace, artifact)
        return WorkflowResult(completed=True, artifact=str(artifact))

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
