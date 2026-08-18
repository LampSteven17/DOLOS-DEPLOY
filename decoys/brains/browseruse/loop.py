"""
BrowserUseLoop - Continuous execution for BrowserUse.

Runs BrowserUse-native workflows (browse_web, web_search, browse_youtube)
in clusters with configurable timing.
"""
from typing import Optional, TYPE_CHECKING

from common.emulation_loop import BaseEmulationLoop

if TYPE_CHECKING:
    from common.logging.agent_logger import AgentLogger

from brains.browseruse.prompts import BUPrompts
from brains.browseruse.agent import _log_bu_steps

# Default timing parameters (matching MCHP defaults)
DEFAULT_CLUSTER_SIZE = 5
DEFAULT_TASK_INTERVAL = 10
DEFAULT_GROUP_INTERVAL = 500


class BrowserUseLoop(BaseEmulationLoop):
    """
    BrowserUse agent with continuous execution.

    Runs native BrowserUse workflows in random clusters with configurable timing.
    """

    def __init__(
        self,
        model: str = None,
        prompts: BUPrompts = None,
        headless: bool = False,
        max_steps: int = 10,
        cluster_size: int = DEFAULT_CLUSTER_SIZE,
        task_interval: int = DEFAULT_TASK_INTERVAL,
        group_interval: int = DEFAULT_GROUP_INTERVAL,
        logger: Optional["AgentLogger"] = None,
        calibration_profile: Optional[str] = None,
        seed: int = 42,
        behavior_config_dir: Optional[str] = None,
        config_key: Optional[str] = None,
    ):
        self.model = model
        self.prompts = prompts
        self.headless = headless
        self.max_steps = max_steps

        super().__init__(
            cluster_size=cluster_size,
            task_interval=task_interval,
            group_interval=group_interval,
            logger=logger,
            calibration_profile=calibration_profile,
            seed=seed,
            behavior_config_dir=behavior_config_dir,
            config_key=config_key,
        )

    # ── Brain-specific implementations ───────────────────────────────

    def _agent_type_label(self) -> str:
        return "browseruse_loop"

    def _load_workflows(self) -> list:
        """Load all workflows for the loop.

        whois_lookup / download_files registration is gated per-flag from
        behavior.json (behavior.enable_whois, behavior.enable_download).
        PHASE's dumb_baseline writes both as false; PHASE feedback proper
        writes true. _reload_behavioral_config will raise downstream if
        the file is missing.
        """
        from pathlib import Path
        from brains.browseruse.workflows.loader import load_workflows
        from common.behavioral_config import load_workflow_gates

        gates = (load_workflow_gates(Path(self._behavior_config_dir))
                 if self._behavior_config_dir
                 else {"enable_whois": True, "enable_download": True})
        print(f"Loading workflows (gates={gates})...")
        if self.logger:
            self.logger.info("Loading workflows", details=gates)
        workflows = load_workflows(
            model=self.model,
            prompts=self.prompts,
            headless=self.headless,
            max_steps=self.max_steps,
            enable_whois=gates["enable_whois"],
            enable_download=gates["enable_download"],
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
        """Execute a single BrowserUse workflow."""
        try:
            action_result = workflow.action(logger=self.logger)
            if isinstance(action_result, tuple):
                result, success = action_result
            else:
                result, success = action_result, True
            # Browser-Agent workflows return a browser_use AgentHistoryList;
            # emit per-action step events with real outcomes from it. The
            # dedicated whois/download workflows return other types and log
            # their own steps, so duck-type on the history API.
            if hasattr(result, "history") and hasattr(result, "model_actions"):
                _log_bu_steps(self.logger, result)
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
        """Apply BrowserUse-specific behavioral config: max_steps, page_dwell,
        prompt augmentation, plus per-target pools for feedback-only workflows.
        """
        # PHASE per-target content pools — propagate to dedicated workflows
        # when present. Workflows fall back to module-level FALLBACK_* lists
        # when None.
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
            elif wname == "BrowseYoutube" and hasattr(w, "video_pool"):
                w.video_pool = fc.youtube_video_pool
            elif wname == "WebSearch" and hasattr(w, "query_pool"):
                w.query_pool = fc.google_search_pool

        # Behavior modifiers replace the previous overlay. Missing keys restore
        # the workflow's post-load defaults.
        modifiers = fc.behavior_modifiers or {}
        max_steps_global = modifiers.get("max_steps")
        per_workflow = modifiers.get("per_workflow", {})
        for w in self.workflows:
            wname = getattr(w, 'name', '') or w.__class__.__name__
            if hasattr(w, 'max_steps'):
                base_max = self._baseline_attr(w, "max_steps")
                new_max = per_workflow.get(wname, max_steps_global)
                w.max_steps = int(new_max) if new_max is not None else base_max

        # page_dwell: min/max sampled fresh per step. A removed or malformed
        # override restores the native workflow value.
        pd = modifiers.get("page_dwell")
        pd_tuple = None
        if isinstance(pd, dict):
            try:
                pd_tuple = (
                    float(pd.get("min_seconds", 0.0)),
                    float(pd.get("max_seconds", 0.0)),
                )
            except (TypeError, ValueError):
                pd_tuple = None
        for w in self.workflows:
            if hasattr(w, "page_dwell"):
                base_dwell = self._baseline_attr(w, "page_dwell")
                w.page_dwell = (pd_tuple if pd_tuple and pd_tuple[1] > 0
                                else base_dwell)

        if modifiers:
            if self.logger:
                self.logger.info("[behavior] Applied behavior_modifiers",
                                 details=modifiers)

        # G1: replacement overlay; omission removes the prior PHASE guidance.
        augmentation = (fc.prompt_augmentation or {}).get("prompt_content", "")
        applied, changed = self._apply_prompt_overlay(
            BUPrompts, "Complete the browsing task.", augmentation,
            reset_agent=False,
        )
        if augmentation and self.logger:
            self.logger.info("[behavior] Applied prompt_augmentation",
                             details={"length": len(augmentation),
                                      "workflows": applied, "changed": changed})
