"""Four exact Brain-profile adapters for the canonical workflow runtime."""

from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Mapping, Optional

from phase_workflow.registry import ResolvedTask, WorkflowResult
from phase_workflow.workflows import (
    MCHPDocumentWorkflows,
    OpenDocumentWriter,
    SeleniumResourceWorkflows,
    play_video_realtime,
    play_video_with_chromium,
    structured_llm_task,
)


def _chromium_driver():
    from selenium import webdriver
    from brains.browseruse.config import CHROMIUM_ARGS

    options = webdriver.ChromeOptions()
    for argument in CHROMIUM_ARGS:
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


def _require_distribution(name: str, expected: str) -> None:
    try:
        installed = version(name)
    except PackageNotFoundError as exc:
        raise RuntimeError(f"required framework {name}=={expected} is not installed") from exc
    if installed != expected:
        raise RuntimeError(
            f"required framework {name}=={expected}, found {installed}"
        )


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
        tools = Tools()
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
        if playback is not None:
            return WorkflowResult(completed=playback.completed)
        if document is not None:
            return document.result
        return WorkflowResult(completed=bool(result and result.is_done()))

    return asyncio.run(run())


def smolagents_runner(
    task: ResolvedTask,
    workspace: Path,
    profile: Mapping,
    logger=None,
    *,
    video_player: Callable[[ResolvedTask], bool] = play_video_realtime,
    document_writer: Optional[OpenDocumentWriter] = None,
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
                return playback.invoke()

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
                return document.invoke()

        tools = [CreateAssignedDocumentTool()]
    else:
        tools = [VisitWebpageTool()]

    agent = CodeAgent(
        tools=tools,
        model=LiteLLMModel(model_id=f"ollama/{profile['model']['ollama']}"),
        instructions=profile["system_guidance"],
        max_steps=profile["max_steps"],
        step_callbacks=callbacks,
    )
    result = agent.run(structured_llm_task(task))
    if playback is not None:
        return WorkflowResult(completed=playback.completed)
    if document is not None:
        return document.result
    return WorkflowResult(
        completed=result is not None and bool(str(result).strip())
    )


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
