"""Four exact Brain-profile adapters for the canonical workflow runtime."""

from __future__ import annotations

import asyncio
import os
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit

from phase_workflow.registry import ResolvedTask, WorkflowResult
from phase_workflow.workflows import (
    MCHPDocumentWorkflows,
    OpenDocumentWriter,
    SeleniumResourceWorkflows,
    play_video_realtime,
    play_video_with_chromium,
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


def _mchp_driver():
    from brains.mchp.app.utility.webdriver_helper import WebDriverHelper

    return WebDriverHelper()


class ResourceBrain:
    """Canonical resource handlers used by Scripted and MCHP."""

    def __init__(self, web: SeleniumResourceWorkflows, documents=None):
        self._web = web
        self._documents = documents or OpenDocumentWriter()
        self._handlers = {
            "WebResearch": self._web.web_research,
            "VideoViewing": self._web.video_viewing,
            "DocumentCreation": self._document,
        }

    def execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        if task.instruction is not None:
            raise RuntimeError(f"{task.brain} must not receive an instruction")
        if task.workflow == "DocumentCreation":
            return self._document(task, workspace)
        return self._handlers[task.workflow](task)

    def _document(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        return self._documents.create(task, workspace)


class FrameworkBrain:
    """LLM Brain that receives one structured resolved task and exact instruction."""

    def __init__(
        self,
        runner: Callable[[ResolvedTask, Path], WorkflowResult],
    ):
        self._runner = runner

    def execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        if task.instruction is None:
            raise RuntimeError(f"{task.brain} requires an instruction")
        return self._runner(task, workspace)


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
    framework_api=None,
    llm_factory=None,
    step_logger=None,
    chromium_args=None,
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
        def play_assigned_video():
            message = playback.invoke()
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
        def create_assigned_document():
            message = document.invoke()
            return ActionResult(
                is_done=True,
                success=document.completed,
                extracted_content=message,
                error=None if document.completed else message,
            )

    async def run():
        agent_kwargs = dict(
            task=structured_llm_task(task),
            llm=llm_factory(profile["model"]["ollama"], logger),
            browser_session=BrowserSession(
                headless=True, channel="chromium", args=chromium_args
            ),
            directly_open_url=False,
        )
        if tools is not None:
            agent_kwargs["tools"] = tools
        if playback is not None:
            agent_kwargs["step_timeout"] = task.resource["play_seconds"] + 60
        agent = Agent(**agent_kwargs)
        result = await agent.run(max_steps=profile["max_steps"])
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
        return WorkflowResult(completed=framework_completed)

    return asyncio.run(run())


def smolagents_runner(
    task: ResolvedTask,
    workspace: Path,
    profile: Mapping,
    logger=None,
    *,
    video_player: Callable[[ResolvedTask], bool] = play_video_realtime,
    document_writer: Optional[OpenDocumentWriter] = None,
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
    else:
        research = AssignedWebResearch(task, webpage_reader)

        class VerifiedVisitWebpageTool(VisitWebpageTool):
            def forward(self, url):
                return research.invoke(url)

        tools = [VerifiedVisitWebpageTool()]

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
    if document is not None:
        agent_kwargs["use_structured_outputs_internally"] = True
    agent = CodeAgent(**agent_kwargs)
    result = agent.run(structured_llm_task(task))
    if playback is not None:
        return WorkflowResult(completed=playback.completed)
    if document is not None:
        return document.result
    return WorkflowResult(completed=research.completed)


def build_brain(brain: str, profile: Mapping, logger=None):
    if brain == "scripted":
        return ResourceBrain(SeleniumResourceWorkflows(_chromium_driver))
    if brain == "mchp":
        return ResourceBrain(
            SeleniumResourceWorkflows(_mchp_driver),
            documents=MCHPDocumentWorkflows(logger=logger),
        )
    if brain == "browseruse":
        return FrameworkBrain(
            lambda task, workspace: browseruse_runner(
                task,
                workspace,
                profile,
                logger,
                video_player=play_video_with_chromium,
            ),
        )
    if brain == "smolagents":
        return FrameworkBrain(
            lambda task, workspace: smolagents_runner(
                task,
                workspace,
                profile,
                logger,
                video_player=play_video_realtime,
            ),
        )
    raise RuntimeError(f"unsupported canonical Brain: {brain}")
