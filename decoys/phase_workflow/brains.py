"""Four exact Brain-profile adapters for the canonical workflow runtime."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit

from phase_workflow.registry import ResolvedTask, WorkflowResult
from phase_workflow.workflows import (
    DailyDocumentStore,
    HttpsDocumentSync,
    KerberosShareAccess,
    MCHPDocumentWorkflows,
    OpenDocumentWriter,
    SHARE_UNC,
    SeleniumResourceWorkflows,
    firefox_download,
    play_video_realtime,
    play_video_with_chromium,
    stream_https_download,
    structured_llm_task,
)


_SCRIPTED_CHROMIUM_ARGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-gpu",
    "--autoplay-policy=no-user-gesture-required",
)


def _chromium_driver():
    from selenium import webdriver

    options = webdriver.ChromeOptions()
    for argument in _SCRIPTED_CHROMIUM_ARGS:
        options.add_argument(argument)
    options.add_argument("--headless=new")
    return webdriver.Chrome(options=options)


def _mchp_driver(download_dir=None):
    from brains.mchp.app.utility.webdriver_helper import WebDriverHelper

    return WebDriverHelper.independent(download_dir=download_dir)


class ResourceBrain:
    """Canonical resource handlers used by Scripted and MCHP."""

    def __init__(
        self,
        web: SeleniumResourceWorkflows,
        documents=None,
        document_store=None,
        downloader=None,
        syncer=None,
        share=None,
    ):
        self._web = web
        self._documents = documents or OpenDocumentWriter()
        self._document_store = document_store or DailyDocumentStore()
        self._downloader = downloader or stream_https_download
        self._syncer = syncer or HttpsDocumentSync(self._document_store)
        self._share = share or KerberosShareAccess(self._document_store)
        self._handlers = {
            "WebResearch": self._web.web_research,
            "VideoViewing": self._web.video_viewing,
            "DocumentCreation": self._document,
            "FileDownload": self._download,
            "FileSyncUpload": self._syncer.execute,
            "NetworkShareAccess": self._share.execute,
        }

    def execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        if task.instruction is not None:
            raise RuntimeError(f"{task.brain} must not receive an instruction")
        if task.workflow in {
            "DocumentCreation", "FileDownload", "FileSyncUpload",
            "NetworkShareAccess",
        }:
            return self._handlers[task.workflow](task, workspace)
        return self._handlers[task.workflow](task)

    def _document(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        result = self._documents.create(task, workspace)
        if result.completed and result.artifact is not None:
            self._document_store.register(Path(workspace).name, Path(result.artifact))
        return result

    def _download(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        artifact = self._downloader(task, workspace)
        return WorkflowResult(completed=True, artifact=str(artifact))


class FrameworkBrain:
    """LLM Brain that receives one structured resolved task and exact instruction."""

    def __init__(
        self,
        runner: Callable[[ResolvedTask, Path], WorkflowResult],
        document_store=None,
        closer: Optional[Callable[[], None]] = None,
    ):
        self._runner = runner
        self._document_store = document_store
        self._closer = closer

    def execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        if task.instruction is None:
            raise RuntimeError(f"{task.brain} requires an instruction")
        result = self._runner(task, workspace)
        if (
            task.workflow == "DocumentCreation"
            and result.completed
            and result.artifact is not None
            and self._document_store is not None
        ):
            self._document_store.register(Path(workspace).name, Path(result.artifact))
        return result

    def close(self) -> None:
        if self._closer is not None:
            self._closer()


class AssignedVideoPlayback:
    """One parameter-free LLM action bound to one immutable assigned video."""

    def __init__(self, task: ResolvedTask, player: Callable[[ResolvedTask], bool]):
        if task.workflow != "VideoViewing":
            raise RuntimeError("assigned playback requires VideoViewing")
        if (
            task.resource["kind"] != "youtube_video"
            or task.resource["play_seconds"] != 300
        ):
            raise RuntimeError("assigned playback requires one 300-second video")
        self.task = task
        self._player = player
        self.call_count = 0
        self.succeeded = False
        self.error: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self.call_count == 1 and self.succeeded

    def invoke(self) -> str:
        self.call_count += 1
        if self.call_count != 1:
            self.error = "assigned playback action may be invoked only once"
            return self.error
        try:
            self.succeeded = bool(self._player(self.task))
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            return f"assigned playback failed: {self.error}"
        if not self.succeeded:
            self.error = "playback returned failure"
            return f"assigned playback failed: {self.error}"
        return "assigned playback completed"


class AssignedDocumentCreation:
    """One parameter-free LLM action bound to one assigned document and workspace."""

    def __init__(
        self,
        task: ResolvedTask,
        workspace: Path,
        writer: OpenDocumentWriter,
    ):
        if task.workflow != "DocumentCreation":
            raise RuntimeError("assigned document action requires DocumentCreation")
        if task.resource["kind"] not in {"document", "spreadsheet"}:
            raise RuntimeError("assigned document action requires a document resource")
        self.task = task
        self.workspace = Path(workspace)
        self._writer = writer
        self.call_count = 0
        self.succeeded = False
        self.artifact: Optional[str] = None
        self.error: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self.call_count == 1 and self.succeeded

    @property
    def result(self) -> WorkflowResult:
        return WorkflowResult(
            completed=self.completed,
            artifact=self.artifact if self.completed else None,
        )

    def invoke(self) -> str:
        self.call_count += 1
        if self.call_count != 1:
            self.error = "assigned document action may be invoked only once"
            return self.error
        try:
            result = self._writer.create(self.task, self.workspace)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            return f"assigned document creation failed: {self.error}"
        expected_artifact = self.workspace / self.task.resource["filename"]
        if not result.completed:
            self.error = "document writer returned failure"
        elif result.artifact is None or Path(result.artifact) != expected_artifact:
            self.error = "document writer returned an unexpected artifact"
        else:
            self.succeeded = True
            self.artifact = result.artifact
            return "assigned document created"
        return f"assigned document creation failed: {self.error}"


class AssignedFileDownload:
    """One immutable, exact-size assigned download action."""

    def __init__(self, task: ResolvedTask, workspace: Path, downloader):
        if task.workflow != "FileDownload" or task.resource["kind"] != "https_download":
            raise RuntimeError("assigned download requires FileDownload")
        self.task = task
        self.workspace = Path(workspace)
        self._downloader = downloader
        self.call_count = 0
        self.succeeded = False
        self.artifact: Optional[str] = None
        self.error: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self.call_count == 1 and self.succeeded

    @property
    def result(self) -> WorkflowResult:
        return WorkflowResult(
            completed=self.completed,
            artifact=self.artifact if self.completed else None,
        )

    def invoke(self) -> str:
        self.call_count += 1
        if self.call_count != 1:
            self.error = "assigned download action may be invoked only once"
            return self.error
        try:
            artifact = Path(self._downloader(self.task, self.workspace))
            if not artifact.is_file():
                raise RuntimeError("assigned download produced no local file")
            if artifact.stat().st_size != self.task.resource["expected_bytes"]:
                raise RuntimeError("assigned download produced the wrong byte size")
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            return f"assigned download failed: {self.error}"
        self.succeeded = True
        self.artifact = str(artifact)
        return "assigned download completed"


class AssignedBoundedTransfer:
    """Exactly-once wrapper for one immutable sync or share primitive."""

    def __init__(self, task: ResolvedTask, workspace: Path, executor):
        if task.workflow not in {"FileSyncUpload", "NetworkShareAccess"}:
            raise RuntimeError("assigned transfer requires a transfer workflow")
        self.task = task
        self.workspace = Path(workspace)
        self._executor = executor
        self.call_count = 0
        self.succeeded = False
        self.artifact: Optional[str] = None
        self.error: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self.call_count == 1 and self.succeeded

    @property
    def result(self) -> WorkflowResult:
        return WorkflowResult(
            completed=self.completed,
            artifact=self.artifact if self.completed else None,
        )

    def invoke(self) -> str:
        self.call_count += 1
        if self.call_count != 1:
            self.error = "assigned transfer action may be invoked only once"
            return self.error
        try:
            result = self._executor.execute(self.task, self.workspace)
            if not result.completed:
                raise RuntimeError("bounded transfer returned failure")
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            return f"assigned transfer failed: {self.error}"
        self.succeeded = True
        self.artifact = result.artifact
        return "assigned transfer completed"


class AssignedWebResearch:
    """Verify the two LLM-selected page visits required by WebResearch."""

    # markdownify preserves optional link titles, for example
    # ``[Main page](/wiki/Main_Page "Visit the main page")``. Capture the URL
    # token without requiring it to be immediately followed by ``)``.
    _MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)")

    def __init__(self, task: ResolvedTask, visitor: Callable[[str], str]):
        if task.workflow != "WebResearch":
            raise RuntimeError("assigned research requires WebResearch")
        self.task = task
        self._visitor = visitor
        self.call_count = 0
        self.succeeded = False
        self.failed = False
        self.error: Optional[str] = None
        self._followup_urls: set[str] = set()
        if task.resource["kind"] == "direct_url":
            initial_url = task.resource["url"]
        elif task.resource["kind"] == "search_query":
            initial_url = "https://www.google.com/search?q=" + quote_plus(
                task.resource["query"]
            )
        else:
            raise RuntimeError("assigned research has an unsupported resource")
        self._initial_url = self._normalize(initial_url)

    @property
    def completed(self) -> bool:
        return self.call_count == 2 and self.succeeded and not self.failed

    def invoke(self, url: str) -> str:
        self.call_count += 1
        if self.failed:
            self._fail("assigned research action already failed")
        normalized = self._normalize(url)
        if self.call_count == 1:
            if normalized != self._initial_url:
                self._fail("research opened an unassigned initial resource")
            content = self._visit(url)
            self._followup_urls = self._links_from(content, normalized)
            if not self._followup_urls:
                self._fail("assigned research resource has no readable result link")
            return content
        if self.call_count == 2:
            if normalized not in self._followup_urls:
                self._fail("research opened an unassigned follow-up resource")
            content = self._visit(url)
            self.succeeded = True
            return content
        self._fail("assigned research action was invoked repeatedly")

    def _visit(self, url: str) -> str:
        try:
            content = self._visitor(url)
        except Exception as exc:
            self._fail(str(exc) or type(exc).__name__)
        if not isinstance(content, str) or not content.strip():
            self._fail("assigned research action returned no readable content")
        return content

    def _fail(self, message: str):
        self.failed = True
        self.succeeded = False
        self.error = message
        raise RuntimeError(message)

    @classmethod
    def _links_from(cls, content: str, base_url: str) -> set[str]:
        links = set()
        for raw in cls._MARKDOWN_LINK.findall(content):
            candidate = urljoin(base_url, raw.strip("<>"))
            parts = urlsplit(candidate)
            if parts.netloc.endswith("google.com") and parts.path == "/url":
                redirected = parse_qs(parts.query).get("q", [])
                if redirected:
                    candidate = redirected[0]
            normalized = cls._normalize(candidate)
            if normalized != base_url:
                links.add(normalized)
        return links

    @staticmethod
    def _normalize(url: str) -> str:
        if not isinstance(url, str):
            raise RuntimeError("research URL must be a string")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise RuntimeError("research URL must be HTTP(S) with a host")
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _browseruse_completed(result) -> bool:
    """Require an explicit final and successful BrowserUse result."""
    try:
        return bool(
            result
            and callable(getattr(result, "is_done", None))
            and callable(getattr(result, "is_successful", None))
            and result.is_done() is True
            and result.is_successful() is True
        )
    except Exception:
        return False


def _browseruse_action_evidence(
    task: ResolvedTask, *, artifact: Optional[str] = None
) -> str:
    """Describe only evidence produced by the successful immutable action."""
    fields = [
        f"workflow={task.workflow}",
        f"resource_id={task.resource_id}",
    ]
    resource = task.resource
    if resource.get("url"):
        fields.append(f"assigned_url={resource['url']}")
    elif resource.get("video_id"):
        fields.append(
            "assigned_url=https://www.youtube.com/watch?v="
            + str(resource["video_id"])
        )
    if task.workflow == "NetworkShareAccess":
        fields.append(f"share={SHARE_UNC}/{resource['path']}")
    if artifact is not None:
        artifact_path = Path(artifact)
        fields.append(f"artifact={artifact_path}")
        if artifact_path.is_file():
            fields.append(f"observed_bytes={artifact_path.stat().st_size}")
    if resource.get("expected_bytes") is not None:
        fields.append(f"expected_bytes={resource['expected_bytes']}")
    if resource.get("play_seconds") is not None:
        fields.extend((
            f"expected_seconds={resource['play_seconds']}",
            f"observed_seconds={resource['play_seconds']}",
        ))
    if resource.get("kind") in {"document", "spreadsheet"}:
        fields.append(f"format={resource['kind']}")
    return "verified assigned operation: " + " ".join(fields)


async def _close_browseruse_resources(agent, browser_session) -> None:
    """Close BrowserUse resources before their owning event loop exits."""
    close = getattr(agent, "close", None)
    if close is None:
        close = getattr(browser_session, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


class _BrowserUseEventLoop:
    """Run all BrowserUse sessions on one process-owned asyncio loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop = None
        self._thread = None

    def run(self, coroutine):
        self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._serve,
                    args=(self._loop,),
                    name="browseruse-event-loop",
                    daemon=True,
                )
                self._thread.start()
        self._ready.wait()

    def _serve(self, loop) -> None:
        asyncio.set_event_loop(loop)
        self._ready.set()
        loop.run_forever()
        loop.close()

    def close(self) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
            self._ready.clear()
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join()


def _require_distribution(name: str, expected: str) -> None:
    try:
        installed = version(name)
    except PackageNotFoundError as exc:
        raise RuntimeError(f"required framework {name}=={expected} is not installed") from exc
    if installed != expected:
        raise RuntimeError(
            f"required framework {name}=={expected}, found {installed}"
        )


def _read_webpage(url: str) -> str:
    """Return one assigned page as Markdown using one identifiable request."""
    import requests
    from markdownify import markdownify

    response = requests.get(
        url,
        headers={"User-Agent": "RUSE phase-workflow control/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    content = markdownify(response.text).strip()
    content = re.sub(r"\n{3,}", "\n\n", content)
    if not content:
        raise RuntimeError("assigned research resource returned no readable content")
    if len(content) > 40000:
        content = content[:40000] + (
            "\n..._This content has been truncated to stay below 40000 characters_...\n"
        )
    return content


def browseruse_runner(
    task: ResolvedTask,
    workspace: Path,
    profile: Mapping,
    logger=None,
    *,
    video_player: Callable[[ResolvedTask], bool] = play_video_with_chromium,
    document_writer: Optional[OpenDocumentWriter] = None,
    downloader=stream_https_download,
    syncer=None,
    share=None,
    framework_api=None,
    llm_factory=None,
    step_logger=None,
    chromium_args=None,
    async_executor=None,
    workflow_timeout=600,
) -> WorkflowResult:
    framework = profile["framework"]
    _require_distribution(framework["name"], framework["version"])
    if framework_api is None:
        from browser_use import ActionResult, Agent, Tools
        from browser_use.browser.session import BrowserSession
        framework_api = Agent, BrowserSession, Tools, ActionResult
    Agent, BrowserSession, Tools, ActionResult = framework_api
    if llm_factory is None or step_logger is None:
        from brains.browseruse.agent import create_logged_chat_ollama, _log_bu_steps
        llm_factory = llm_factory or create_logged_chat_ollama
        step_logger = step_logger or _log_bu_steps
    if chromium_args is None:
        from brains.browseruse.config import CHROMIUM_ARGS
        chromium_args = CHROMIUM_ARGS

    playback = None
    document = None
    assigned_download = None
    assigned_transfer = None
    tools = None
    if task.workflow == "VideoViewing":
        playback = AssignedVideoPlayback(task, video_player)

        class BoundedVideoTools(Tools):
            async def act(self, *args, **kwargs):
                kwargs["action_timeout"] = task.resource["play_seconds"] + 60
                return await super().act(*args, **kwargs)

        tools = BoundedVideoTools()
        for action_name in tuple(tools.registry.registry.actions):
            tools.exclude_action(action_name)

        @tools.action(
            "Play the assigned video exactly once for its fixed duration. "
            "This action accepts no URL, video ID, or duration parameters.",
            terminates_sequence=True,
        )
        async def play_assigned_video():
            message = await asyncio.to_thread(playback.invoke)
            if playback.completed:
                message = _browseruse_action_evidence(task)
            return ActionResult(
                is_done=True,
                success=playback.completed,
                extracted_content=message,
                error=None if playback.completed else message,
            )
    elif task.workflow == "DocumentCreation":
        document = AssignedDocumentCreation(
            task, workspace, document_writer or OpenDocumentWriter()
        )
        tools = Tools()
        for action_name in tuple(tools.registry.registry.actions):
            tools.exclude_action(action_name)

        @tools.action(
            "Create the assigned document exactly once in its fixed workspace. "
            "This action accepts no filename, format, template, or content parameters.",
            terminates_sequence=True,
        )
        async def create_assigned_document():
            message = await asyncio.to_thread(document.invoke)
            if document.completed:
                message = _browseruse_action_evidence(
                    task, artifact=document.artifact
                )
            return ActionResult(
                is_done=True,
                success=document.completed,
                extracted_content=message,
                error=None if document.completed else message,
            )
    elif task.workflow == "FileDownload":
        assigned_download = AssignedFileDownload(task, workspace, downloader)
        tools = Tools()
        for action_name in tuple(tools.registry.registry.actions):
            tools.exclude_action(action_name)

        @tools.action(
            "Download the assigned file exactly once. This action accepts no URL, "
            "filename, expected-size, or alternate-resource parameters.",
            terminates_sequence=True,
        )
        async def download_assigned_file():
            message = await asyncio.to_thread(assigned_download.invoke)
            if assigned_download.completed:
                message = _browseruse_action_evidence(
                    task, artifact=assigned_download.artifact
                )
            return ActionResult(
                is_done=True,
                success=assigned_download.completed,
                extracted_content=message,
                error=None if assigned_download.completed else message,
            )
    elif task.workflow in {"FileSyncUpload", "NetworkShareAccess"}:
        executor = syncer if task.workflow == "FileSyncUpload" else share
        if executor is None:
            raise RuntimeError(f"missing bounded executor for {task.workflow}")
        assigned_transfer = AssignedBoundedTransfer(task, workspace, executor)
        tools = Tools()
        for action_name in tuple(tools.registry.registry.actions):
            tools.exclude_action(action_name)

        if task.workflow == "FileSyncUpload":
            @tools.action(
                "Upload the assigned local document exactly once. This action "
                "accepts no file, endpoint, bytes, or request parameters.",
                terminates_sequence=True,
            )
            async def sync_assigned_document():
                message = await asyncio.to_thread(assigned_transfer.invoke)
                if assigned_transfer.completed:
                    message = _browseruse_action_evidence(
                        task, artifact=assigned_transfer.artifact
                    )
                return ActionResult(
                    is_done=True,
                    success=assigned_transfer.completed,
                    extracted_content=message,
                    error=None if assigned_transfer.completed else message,
                )
        else:
            @tools.action(
                "Access the assigned network share exactly once. This action "
                "accepts no share, path, credential, or command parameters.",
                terminates_sequence=True,
            )
            async def access_assigned_share():
                message = await asyncio.to_thread(assigned_transfer.invoke)
                if assigned_transfer.completed:
                    message = _browseruse_action_evidence(
                        task, artifact=assigned_transfer.artifact
                    )
                return ActionResult(
                    is_done=True,
                    success=assigned_transfer.completed,
                    extracted_content=message,
                    error=None if assigned_transfer.completed else message,
                )

    async def run():
        browser_session = BrowserSession(
            headless=True, channel="chromium", args=chromium_args
        )
        agent_kwargs = dict(
            task=structured_llm_task(task),
            llm=llm_factory(profile["model"]["ollama"], logger),
            browser_session=browser_session,
            directly_open_url=False,
        )
        if tools is not None:
            agent_kwargs["tools"] = tools
        if playback is not None:
            # Keep the immutable action's own 360-second limit, but leave the
            # outer step enough room for the preceding LLM response and the
            # confirmed 300-second playback action.
            agent_kwargs["step_timeout"] = task.resource["play_seconds"] + 120
        elif assigned_download is not None or assigned_transfer is not None:
            agent_kwargs["step_timeout"] = 360
        agent = Agent(**agent_kwargs)
        try:
            result = await asyncio.wait_for(
                agent.run(max_steps=profile["max_steps"]),
                timeout=workflow_timeout,
            )
            step_logger(logger, result)
            framework_completed = _browseruse_completed(result)
            if playback is not None:
                return WorkflowResult(
                    completed=playback.completed and framework_completed
                )
            if document is not None:
                return WorkflowResult(
                    completed=document.completed and framework_completed,
                    artifact=document.artifact
                    if document.completed and framework_completed else None,
                )
            if assigned_download is not None:
                result = assigned_download.result
                return WorkflowResult(
                    completed=result.completed and framework_completed,
                    artifact=result.artifact if framework_completed else None,
                )
            if assigned_transfer is not None:
                result = assigned_transfer.result
                return WorkflowResult(
                    completed=result.completed and framework_completed,
                    artifact=result.artifact if framework_completed else None,
                )
            return WorkflowResult(completed=framework_completed)
        finally:
            await _close_browseruse_resources(agent, browser_session)

    if async_executor is not None:
        return async_executor(run())
    return asyncio.run(run())


def smolagents_runner(
    task: ResolvedTask,
    workspace: Path,
    profile: Mapping,
    logger=None,
    *,
    video_player: Callable[[ResolvedTask], bool] = play_video_realtime,
    document_writer: Optional[OpenDocumentWriter] = None,
    downloader=stream_https_download,
    syncer=None,
    share=None,
    webpage_reader: Callable[[str], str] = _read_webpage,
    framework_api=None,
) -> WorkflowResult:
    framework = profile["framework"]
    _require_distribution(framework["name"], framework["version"])
    if framework_api is None:
        from smolagents import CodeAgent, LiteLLMModel, Tool, VisitWebpageTool
        framework_api = CodeAgent, LiteLLMModel, Tool, VisitWebpageTool
    CodeAgent, LiteLLMModel, Tool, VisitWebpageTool = framework_api

    callbacks = None
    if logger is not None:
        from common.logging.llm_callbacks import (
            make_smol_step_callback,
            setup_litellm_callbacks,
        )
        setup_litellm_callbacks(logger)
        callbacks = [make_smol_step_callback(logger)]
    playback = None
    document = None
    research = None
    assigned_download = None
    assigned_transfer = None
    if task.workflow == "VideoViewing":
        playback = AssignedVideoPlayback(task, video_player)

        class PlayAssignedVideoTool(Tool):
            name = "play_assigned_video"
            description = (
                "Play the assigned video exactly once for its fixed duration. "
                "The assigned video and duration are fixed; this tool accepts no inputs."
            )
            inputs = {}
            output_type = "string"

            def forward(self):
                message = playback.invoke()
                if not playback.completed:
                    raise RuntimeError(message)
                return message

        tools = [PlayAssignedVideoTool()]
    elif task.workflow == "DocumentCreation":
        document = AssignedDocumentCreation(
            task, workspace, document_writer or OpenDocumentWriter()
        )

        class CreateAssignedDocumentTool(Tool):
            name = "create_assigned_document"
            description = (
                "Create the assigned document exactly once in its fixed workspace. "
                "The filename, format, template, and content are fixed; this tool "
                "accepts no inputs."
            )
            inputs = {}
            output_type = "string"

            def forward(self):
                message = document.invoke()
                if not document.completed:
                    raise RuntimeError(message)
                return message

        tools = [CreateAssignedDocumentTool()]
    elif task.workflow == "FileDownload":
        assigned_download = AssignedFileDownload(task, workspace, downloader)

        class DownloadAssignedFileTool(Tool):
            name = "download_assigned_file"
            description = (
                "Download the assigned file exactly once. The URL and expected "
                "size are fixed; this tool accepts no inputs."
            )
            inputs = {}
            output_type = "string"

            def forward(self):
                message = assigned_download.invoke()
                if not assigned_download.completed:
                    raise RuntimeError(message)
                return message

        tools = [DownloadAssignedFileTool()]
    elif task.workflow in {"FileSyncUpload", "NetworkShareAccess"}:
        executor = syncer if task.workflow == "FileSyncUpload" else share
        if executor is None:
            raise RuntimeError(f"missing bounded executor for {task.workflow}")
        assigned_transfer = AssignedBoundedTransfer(task, workspace, executor)

        if task.workflow == "FileSyncUpload":
            class AssignedTransferTool(Tool):
                name = "sync_assigned_document"
                description = (
                    "Upload the assigned local document exactly once. The file, "
                    "endpoint, and request are fixed; this tool accepts no inputs."
                )
                inputs = {}
                output_type = "string"

                def forward(self):
                    message = assigned_transfer.invoke()
                    if not assigned_transfer.completed:
                        raise RuntimeError(message)
                    return message
        else:
            class AssignedTransferTool(Tool):
                name = "access_assigned_share"
                description = (
                    "Access the assigned network share exactly once. The share, "
                    "seed, credentials, and transfer are fixed; this tool accepts "
                    "no inputs."
                )
                inputs = {}
                output_type = "string"

                def forward(self):
                    message = assigned_transfer.invoke()
                    if not assigned_transfer.completed:
                        raise RuntimeError(message)
                    return message

        tools = [AssignedTransferTool()]
    elif task.workflow == "WebResearch":
        research = AssignedWebResearch(task, webpage_reader)

        class VerifiedVisitWebpageTool(VisitWebpageTool):
            def forward(self, url):
                return research.invoke(url)

        tools = [VerifiedVisitWebpageTool()]
    else:
        raise RuntimeError(f"unsupported SmolAgents workflow: {task.workflow}")

    agent_kwargs = dict(
        tools=tools,
        model=LiteLLMModel(model_id=f"ollama/{profile['model']['ollama']}"),
        instructions=profile["system_guidance"],
        max_steps=profile["max_steps"],
        step_callbacks=callbacks,
    )
    if playback is not None:
        agent_kwargs["executor_kwargs"] = {
            "timeout_seconds": task.resource["play_seconds"] + 60,
        }
    elif assigned_download is not None or assigned_transfer is not None:
        agent_kwargs["executor_kwargs"] = {"timeout_seconds": 360}
    if document is not None:
        agent_kwargs["use_structured_outputs_internally"] = True
    agent = CodeAgent(**agent_kwargs)
    result = agent.run(structured_llm_task(task))
    if playback is not None:
        return WorkflowResult(completed=playback.completed)
    if document is not None:
        return document.result
    if assigned_download is not None:
        return assigned_download.result
    if assigned_transfer is not None:
        return assigned_transfer.result
    return WorkflowResult(completed=bool(research and research.completed))


def build_brain(
    brain: str,
    profile: Mapping,
    logger=None,
    *,
    document_store: Optional[DailyDocumentStore] = None,
):
    documents = document_store or DailyDocumentStore()
    syncer = HttpsDocumentSync(documents)
    share = KerberosShareAccess(documents)
    if brain == "scripted":
        return ResourceBrain(
            SeleniumResourceWorkflows(_chromium_driver),
            document_store=documents,
            downloader=stream_https_download,
            syncer=syncer,
            share=share,
        )
    if brain == "mchp":
        return ResourceBrain(
            SeleniumResourceWorkflows(_mchp_driver),
            documents=MCHPDocumentWorkflows(logger=logger),
            document_store=documents,
            downloader=lambda task, workspace: firefox_download(
                task, workspace, lambda path: _mchp_driver(path)
            ),
            syncer=syncer,
            share=share,
        )
    if brain == "browseruse":
        event_loop = _BrowserUseEventLoop()
        return FrameworkBrain(
            lambda task, workspace: browseruse_runner(
                task,
                workspace,
                profile,
                logger,
                video_player=play_video_with_chromium,
                downloader=stream_https_download,
                syncer=syncer,
                share=share,
                async_executor=event_loop.run,
            ),
            document_store=documents,
            closer=event_loop.close,
        )
    if brain == "smolagents":
        return FrameworkBrain(
            lambda task, workspace: smolagents_runner(
                task,
                workspace,
                profile,
                logger,
                video_player=play_video_realtime,
                downloader=stream_https_download,
                syncer=syncer,
                share=share,
            ),
            document_store=documents,
        )
    raise RuntimeError(f"unsupported canonical Brain: {brain}")
