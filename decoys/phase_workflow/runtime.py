"""Startup wiring for the canonical workflow runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from common.logging.agent_logger import AgentLogger
from phase_workflow.brains import build_brain
from phase_workflow.configurations import is_workflow_configuration
from phase_workflow.executor import DailyExecutor
from phase_workflow.loader import load_workflow_plan
from phase_workflow.registry import WorkflowRegistry


def resolve_behavior_path(config_key: str, override_dir: Optional[str]) -> Path:
    """Resolve one startup file without creating directories or fallbacks."""
    if override_dir is not None:
        root = Path(override_dir).expanduser()
    elif os.environ.get("RUSE_BEHAVIOR_CONFIG_DIR"):
        root = Path(os.environ["RUSE_BEHAVIOR_CONFIG_DIR"]).expanduser()
    elif Path("/opt/ruse/deployed_sups").is_dir():
        root = Path("/opt/ruse/deployed_sups") / config_key / "behavioral_configurations"
    else:
        root = (
            Path(__file__).resolve().parents[2]
            / "deployed_sups"
            / config_key
            / "behavioral_configurations"
        )
    return root / "behavior.json"


def run_workflow_runtime(config_key: str, behavior_config_dir: Optional[str]) -> None:
    if not is_workflow_configuration(config_key):
        raise RuntimeError(f"unsupported workflow configuration: {config_key}")
    behavior_path = resolve_behavior_path(config_key, behavior_config_dir)
    # The only read of behavior.json. Any exception escapes and prevents startup.
    plan = load_workflow_plan(behavior_path, config_key)
    logger = AgentLogger(agent_type=config_key)
    logger.session_start(config={
        "schema": plan.schema,
        "sup_config": plan.sup_config,
        "brain": plan.brain,
        "hardware": plan.hardware,
        "resource_profile": plan.resource_profile,
        "timezone": str(plan.timezone),
        "max_parallel": plan.max_parallel,
    })
    registry = WorkflowRegistry(
        plan,
        build_brain(plan.brain, plan.brain_profile, logger),
        behavior_path.parent / "workspace",
    )
    executor = DailyExecutor(plan, registry, logger.workflow_plan_terminal)
    try:
        executor.run_forever()
    except KeyboardInterrupt:
        logger.info("Workflow runtime stopped by user")
    except Exception as exc:
        logger.session_fail(message="Workflow runtime failed", exception=exc)
        raise
    finally:
        executor.close()
        logger.session_end()
