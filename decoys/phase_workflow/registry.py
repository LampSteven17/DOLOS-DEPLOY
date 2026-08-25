"""Canonical workflow registry and resolved task model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from phase_workflow.loader import PlanEntry, WorkflowPlan


CANONICAL_HANDLERS = frozenset({
    "WebResearch",
    "VideoViewing",
    "FileDownload",
    "DocumentCreation",
    "FileSyncUpload",
    "NetworkShareAccess",
})


@dataclass(frozen=True)
class ResolvedTask:
    workflow: str
    resource_profile: str
    sup_config: str
    brain: str
    brain_profile: str
    resource_id: str
    resource: Mapping[str, Any]
    instruction: Optional[str]
    occurrence_id: str = "manual"


@dataclass(frozen=True)
class WorkflowResult:
    completed: bool
    artifact: Optional[str] = None


class Brain(Protocol):
    def execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        ...


class WorkflowRegistry:
    """Dispatch exactly the six installed canonical workflows."""

    def __init__(self, plan: WorkflowPlan, brain: Brain, workspace_root: Path):
        self._plan = plan
        self._brain = brain
        self._workspace_root = Path(workspace_root)
        self._handlers = {name: self._execute for name in CANONICAL_HANDLERS}

    @property
    def workflows(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def resolve(
        self, entry: PlanEntry, *, occurrence_id: str = "manual"
    ) -> ResolvedTask:
        if entry.workflow not in self._handlers:
            raise RuntimeError(f"unregistered canonical workflow: {entry.workflow}")
        return ResolvedTask(
            workflow=entry.workflow,
            resource_profile=self._plan.resource_profile,
            sup_config=self._plan.sup_config,
            brain=self._plan.brain,
            brain_profile=self._plan.brain_profile_id,
            resource_id=entry.resource_id,
            resource=entry.resource,
            instruction=entry.instruction,
            occurrence_id=occurrence_id,
        )

    def execute(self, task: ResolvedTask, local_day: str) -> WorkflowResult:
        handler = self._handlers.get(task.workflow)
        if handler is None:
            raise RuntimeError(f"unregistered canonical workflow: {task.workflow}")
        workspace = self._workspace_root / local_day
        workspace.mkdir(parents=True, exist_ok=True)
        return handler(task, workspace)

    def _execute(self, task: ResolvedTask, workspace: Path) -> WorkflowResult:
        return self._brain.execute(task, workspace)
