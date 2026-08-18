"""
SmolAgentLoop - Continuous execution for SmolAgents.

Runs SmolAgents-native workflows (browse_web, web_search, browse_youtube)
in clusters with configurable timing.
"""
from typing import Optional, TYPE_CHECKING

from common.emulation_loop import BaseEmulationLoop

if TYPE_CHECKING:
    from common.logging.agent_logger import AgentLogger

# Default timing parameters (matching MCHP defaults)
DEFAULT_CLUSTER_SIZE = 5
DEFAULT_TASK_INTERVAL = 10
DEFAULT_GROUP_INTERVAL = 500


class SmolAgentLoop(BaseEmulationLoop):
    """
    SmolAgents agent with continuous execution.

    Runs native SmolAgents workflows in random clusters with configurable timing.
    """

    def __init__(
        self,
        model: str = None,
        prompts=None,
        cluster_size: int = DEFAULT_CLUSTER_SIZE,
        task_interval: int = DEFAULT_TASK_INTERVAL,
        group_interval: int = DEFAULT_GROUP_INTERVAL,
        logger: Optional["AgentLogger"] = None,
        calibration_profile: Optional[str] = None,
        seed: int = 42,
        behavior_config_dir: Optional[str] = None,
        config_key: Optional[str] = None,
        initial_behavior_snapshot=None,
    ):
        self.model = model
        self.prompts = prompts

        super().__init__(
            cluster_size=cluster_size,
            task_interval=task_interval,
            group_interval=group_interval,
            logger=logger,
            calibration_profile=calibration_profile,
            seed=seed,
            behavior_config_dir=behavior_config_dir,
            config_key=config_key,
            initial_behavior_snapshot=initial_behavior_snapshot,
        )

    # ── Brain-specific implementations ───────────────────────────────

    def _agent_type_label(self) -> str:
        return "smolagents_loop"

    def _load_workflows(self) -> list:
        """Load all workflows for the loop.

        whois_lookup / download_files registration is gated per-flag from
        behavior.json (behavior.enable_whois, behavior.enable_download).
        PHASE's dumb_baseline writes both as false; PHASE feedback proper
        writes true. _reload_behavioral_config will raise downstream if
        the file is missing.
        """
        from pathlib import Path
        from brains.smolagents.workflows.loader import load_workflows
        from common.behavioral_config import (
            load_workflow_gates,
            load_workflow_registration,
        )

        config_dir = Path(self._behavior_config_dir) if self._behavior_config_dir else None
        enabled_workflows = (
            load_workflow_registration(config_dir, self._config_key)
            if config_dir and self._config_key
            else None
        )
        gates = (load_workflow_gates(config_dir)
                 if self._behavior_config_dir
                 else {"enable_whois": True, "enable_download": True})
        print(f"Loading workflows (gates={gates})...")
        if self.logger:
            self.logger.info("Loading workflows", details=gates)
        workflows = load_workflows(
            model=self.model,
            prompts=self.prompts,
            enable_whois=gates["enable_whois"],
            enable_download=gates["enable_download"],
            enabled_workflows=enabled_workflows,
        )
        print(f"Loaded {len(workflows)} workflows")

        # Log workflow distribution
        categories = {}
        for w in workflows:
            cat = getattr(w, 'category', 'Unknown')
            categories[cat] = categories.get(cat, 0) + 1
        print(f"Workflow distribution: {categories}")

        if self.logger:
            self.logger.info("Workflows loaded", details={
                "count": len(workflows),
                "distribution": categories
            })

        return workflows

    def _execute_workflow(self, workflow) -> bool:
        """Execute a single SmolAgents workflow."""
        try:
            action_result = workflow.action(logger=self.logger)
            if isinstance(action_result, tuple):
                result, success = action_result
            else:
                result, success = action_result, True
            if self.logger:
                self.logger.workflow_end(workflow.name, success=success, result=result)
            return success
        except Exception as e:
            print(f"Workflow error: {e}")
            if self.logger:
                self.logger.workflow_end(workflow.name, success=False, error=str(e))
                self.logger.error(f"Workflow '{workflow.description}' failed", exception=e)
            return False

    def _apply_brain_specific_config(self, fc) -> None:
        """Apply SmolAgents-specific behavioral config: max_steps, prompt
        augmentation, site_config, and feedback-only pool propagation.
        """
        # PHASE per-target content pools. Propagate to the dedicated
        # whois_lookup / download_files workflows when present; workflows
        # fall back to module-level FALLBACK_* lists when None.
        for w in self.workflows:
            wname = getattr(w, "name", "")
            if wname == "WhoisLookup" and hasattr(w, "domain_pool"):
                w.domain_pool = fc.whois_domain_pool
            elif wname == "DownloadFiles" and hasattr(w, "url_pool"):
                w.url_pool = fc.download_url_pool
                # Phase 4: size_mix + outcome_mix pass-through
                if hasattr(w, "size_mix"):
                    w.size_mix = fc.download_size_mix
                if hasattr(w, "outcome_mix"):
                    w.outcome_mix = fc.download_outcome_mix
            elif wname == "BrowseWeb" and hasattr(w, "url_pool"):
                w.url_pool = fc.browse_url_pool
            elif wname == "BrowseYouTube" and hasattr(w, "video_pool"):
                w.video_pool = fc.youtube_video_pool
            elif wname == "WebSearch" and hasattr(w, "query_pool"):
                w.query_pool = fc.google_search_pool

        # W3 site_config — propagate content.site_categories to BrowseWebWorkflow.
        # Only BrowseWebWorkflow consumes site_weights; WebSearch + YouTube
        # ignore the field by design (search-result diversity comes from the
        # search engine, not category steering; YouTube is uniformly heavy).
        applied = 0
        for w in self.workflows:
            if getattr(w, "name", "") == "BrowseWeb" and hasattr(w, "site_weights"):
                base_weights = self._baseline_attr(w, "site_weights")
                w.site_weights = (dict(fc.site_config)
                                  if fc.site_config is not None else base_weights)
                applied += 1
        if fc.site_config:
            if self.logger:
                self.logger.info("[behavior] Applied site_config",
                                 details={"weights": fc.site_config, "workflows": applied})

        # Behavior modifiers replace earlier values; omission restores defaults.
        modifiers = fc.behavior_modifiers or {}
        max_steps_global = modifiers.get("max_steps")
        per_workflow = modifiers.get("per_workflow", {})
        for w in self.workflows:
            wname = getattr(w, 'name', '') or w.__class__.__name__
            if hasattr(w, 'max_steps'):
                base_max = self._baseline_attr(w, "max_steps")
                new_max = per_workflow.get(wname, max_steps_global)
                desired = int(new_max) if new_max is not None else base_max
                if w.max_steps != desired:
                    w.max_steps = desired
                    if hasattr(w, "_agent"):
                        w._agent = None
        if modifiers:
            if self.logger:
                self.logger.info("[behavior] Applied behavior_modifiers",
                                 details=modifiers)

        # G1: replacement overlay; rebuild the agent only when prompt bytes change.
        augmentation = (fc.prompt_augmentation or {}).get("prompt_content", "")
        from brains.smolagents.prompts import SMOLPrompts
        applied, changed = self._apply_prompt_overlay(
            SMOLPrompts, "Research and answer the question.", augmentation,
            reset_agent=True,
        )
        if augmentation and self.logger:
            self.logger.info("[behavior] Applied prompt_augmentation",
                             details={"length": len(augmentation),
                                      "workflows": applied, "changed": changed})
