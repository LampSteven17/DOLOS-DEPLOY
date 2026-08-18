"""
MCHP Brain - Human behavior emulation agent.

This is a thin wrapper around the original human.py logic,
providing a class-based interface for the unified SUP runner.

Configurations (exp-3):
- M0: Upstream MITRE pyhuman (control - DO NOT MODIFY)
- M1: RUSE MCHP baseline (no timing)
- M2: MCHP + summer24 calibrated timing
- M3: MCHP + fall24 calibrated timing
- M4: MCHP + spring25 calibrated timing
"""
import os
from importlib import import_module
from typing import Optional, TYPE_CHECKING

from common.emulation_loop import BaseEmulationLoop

if TYPE_CHECKING:
    from common.logging.agent_logger import AgentLogger
    from common.timing.phase_timing import CalibratedTiming

# Default timing parameters (original MCHP)
DEFAULT_CLUSTER_SIZE = 5
DEFAULT_TASK_INTERVAL = 10
DEFAULT_GROUP_INTERVAL = 500

# Windows-only workflows (use os.startfile or other Windows-specific APIs)
# These are excluded for M2+ configs which run on Linux with LLM augmentation
# Note: open_office_calc.py and open_office_writer.py now support LibreOffice on Linux
WINDOWS_ONLY_WORKFLOWS = {
    'ms_paint.py',
}

# Behavior-driven workflows. Each is gated by its own behavior.json flag
# so PHASE dumb_baseline (enable_whois=false, enable_download=false) can
# disable both without skipping the rest of the workflow set.
#   - download_files.py: scripted xkcd/wiki/NIST downloader.
#   - whois_lookup.py:   TCP/43 lookup workflow.
BEHAVIOR_GATED_WORKFLOWS = {
    'download_files.py': 'enable_download',
    'whois_lookup.py':   'enable_whois',
}

# Canonical V2 registration IDs by module.  This is consulted only when an
# explicit V2 document supplies execution.enabled_workflows; v1 keeps the
# existing dynamic loader unchanged.
V2_WORKFLOW_IDS = {
    "browse_web.py": "BrowseWeb",
    "browse_youtube.py": "BrowseYouTube",
    "download_files.py": "DownloadFiles",
    "execute_command.py": "ExecuteCommand",
    "google_search.py": "WebSearch",
    "open_office_calc.py": "SpreadsheetEditor",
    "open_office_writer.py": "DocumentEditor",
    "spawn_shell.py": "ListFiles",
    "whois_lookup.py": "WhoisLookup",
    "ms_paint.py": "MicrosoftPaint",
}


class MCHPAgent(BaseEmulationLoop):
    """
    MCHP (Human Emulation) Agent.

    Runs workflows in random clusters with configurable timing.

    Timing Modes:
    - No timing: Original random timing (M1 baseline)
    - calibration_profile: Calibrated timing from empirical profile (M2-M4)
    """

    def __init__(
        self,
        cluster_size: int = DEFAULT_CLUSTER_SIZE,
        task_interval: int = DEFAULT_TASK_INTERVAL,
        group_interval: int = DEFAULT_GROUP_INTERVAL,
        extra: list = None,
        logger: Optional["AgentLogger"] = None,
        calibration_profile: Optional[str] = None,
        exclude_windows_workflows: bool = False,
        seed: int = 42,
        behavior_config_dir: Optional[str] = None,
        config_key: Optional[str] = None,
        initial_behavior_snapshot=None,
    ):
        self.extra = extra or []
        self.exclude_windows_workflows = exclude_windows_workflows

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
        return "mchp"

    def _load_workflows(self) -> list:
        """Dynamically load all workflow modules.

        Filters:
          - WINDOWS_ONLY_WORKFLOWS    excluded when exclude_windows_workflows=True
          - BEHAVIOR_GATED_WORKFLOWS  excluded when their behavior.json flag
                                      (enable_whois, enable_download) is false
        """
        from pathlib import Path
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
        enabled = set(enabled_workflows) if enabled_workflows is not None else None
        gates = (load_workflow_gates(config_dir)
                 if self._behavior_config_dir
                 else {"enable_whois": True, "enable_download": True})
        print(f"MCHP: loading workflows (gates={gates})")
        extensions = []
        workflows_dir = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), 'app', 'workflows'
        )

        for root, dirs, files in os.walk(workflows_dir):
            files = [f for f in files if not f[0] == '.' and not f[0] == "_"]
            dirs[:] = [d for d in dirs if not d[0] == '.' and not d[0] == "_"]

            for file in files:
                if enabled is not None and V2_WORKFLOW_IDS.get(file) not in enabled:
                    continue
                if self.exclude_windows_workflows and file in WINDOWS_ONLY_WORKFLOWS:
                    print(f"Skipping Windows-only workflow: {file}")
                    continue
                gate_key = BEHAVIOR_GATED_WORKFLOWS.get(file)
                if gate_key and not gates.get(gate_key, True):
                    print(f"Skipping {file} (behavior.{gate_key}=false)")
                    continue

                try:
                    extensions.append(self._load_module('app.workflows', file))
                except Exception as e:
                    print(f'Error could not load workflow: {e}')

        return extensions

    def _load_module(self, root: str, file: str):
        """Load a single workflow module."""
        module_name = file.split('.')[0]
        full_module = f"brains.mchp.{root}.{module_name}"
        workflow_module = import_module(full_module)
        return getattr(workflow_module, 'load')()

    def _execute_workflow(self, workflow) -> bool:
        """Execute a single MCHP workflow."""
        try:
            workflow.action(self.extra, logger=self.logger)
            if self.logger:
                self.logger.workflow_end(workflow.name, success=True)
            return True
        except Exception as e:
            if self.logger:
                self.logger.workflow_end(workflow.name, success=False, error=str(e))
            return False

    def _apply_brain_specific_config(self, fc) -> None:
        """Apply MCHP-specific behavioral config: page_dwell, nav_clicks,
        keep_alive, plus per-target pools for feedback-only workflows.
        """
        # PHASE per-target content pools — propagate to MCHP whois_lookup
        # workflow when present. download_files.py is the existing
        # xkcd/wiki/NIST scripted workflow and uses its own helpers, not
        # the shared download_url_pool.
        for w in self.workflows:
            wname = getattr(w, "name", "")
            if wname == "WhoisLookup" and hasattr(w, "domain_pool"):
                w.domain_pool = fc.whois_domain_pool
            # Phase 1: PHASE pools replace MCHP's file-loaded lists when shipped.
            # MCHP consumes URLs/queries directly (no LLM task wrapping).
            elif wname == "BrowseWeb" and hasattr(w, "website_list"):
                baseline = self._baseline_attr(w, "website_list")
                w.website_list = (list(fc.browse_url_pool)
                                  if fc.browse_url_pool is not None else baseline)
            elif wname == "WebSearch" and hasattr(w, "search_list"):
                baseline = self._baseline_attr(w, "search_list")
                w.search_list = (list(fc.google_search_pool)
                                 if fc.google_search_pool is not None else baseline)
            elif wname == "BrowseYouTube" and hasattr(w, "video_pool"):
                # MCHP YouTube switches from search→click to direct
                # /watch?v={id} navigation when the pool is set; watch +
                # suggested-video clicks still run downstream.
                baseline = self._baseline_attr(w, "video_pool")
                w.video_pool = (list(fc.youtube_video_pool)
                                if fc.youtube_video_pool is not None else baseline)

        # Ablation-gated omissions get INFO-level logs; real gaps stay WARNING
        gated = fc.is_ablation_gated() if fc and hasattr(fc, "is_ablation_gated") else False
        tag = "[INFO]" if gated else "[WARNING]"
        suffix = " (ablation-gated)" if gated else ""
        modifiers = fc.behavior_modifiers or {}
        if not modifiers:
            # Whole section missing — one message instead of three below
            print(f"{tag} behavior_modifiers DISABLED — "
                  f"no behavior section in behavior.json, "
                  f"using MCHP defaults for page_dwell, navigation_clicks, keep_alive_probability"
                  f"{suffix}")

        for w in self.workflows:
            if getattr(w, 'name', '') != 'BrowseWeb':
                continue
            base_min_sleep = self._baseline_attr(w, "min_sleep_time")
            base_max_sleep = self._baseline_attr(w, "max_sleep_time")
            base_nav_clicks = self._baseline_attr(w, "max_navigation_clicks")
            base_keep_alive = self._baseline_attr(w, "keep_alive_probability")
            # PHASE baseline emits page_dwell / navigation_clicks as
            # integers (degenerate single-value mode); only the calibrated
            # PHASE feedback shape uses {min, max} dicts. Coerce non-dict
            # values to {} so the downstream `in` checks don't crash.
            page_dwell = modifiers.get("page_dwell", {})
            if not isinstance(page_dwell, dict):
                page_dwell = {}
            w.max_sleep_time = (int(page_dwell["max_seconds"])
                                if "max_seconds" in page_dwell else base_max_sleep)
            w.min_sleep_time = (int(page_dwell["min_seconds"])
                                if "min_seconds" in page_dwell else base_min_sleep)
            if "max_seconds" not in page_dwell and "min_seconds" not in page_dwell:
                print(f"{tag} B1 page_dwell DISABLED — "
                      f"no behavior.page_dwell.{{min,max}}_seconds, "
                      f"using defaults min={w.min_sleep_time} max={w.max_sleep_time}"
                      f"{suffix}")
            nav_clicks = modifiers.get("navigation_clicks", {})
            if not isinstance(nav_clicks, dict):
                nav_clicks = {}
            if "max" in nav_clicks:
                w.max_navigation_clicks = int(nav_clicks["max"])
            else:
                w.max_navigation_clicks = base_nav_clicks
                print(f"{tag} B2 navigation_clicks DISABLED — "
                      f"no behavior.navigation_clicks.max, "
                      f"using default {w.max_navigation_clicks}"
                      f"{suffix}")
            # G2: Connection reuse probability for tab management
            if "keep_alive_probability" in modifiers:
                w.keep_alive_probability = float(modifiers["keep_alive_probability"])
            else:
                w.keep_alive_probability = base_keep_alive
                print(f"{tag} G2 keep_alive_probability DISABLED — "
                      f"no behavior.keep_alive_probability, "
                      f"using default {w.keep_alive_probability}"
                      f"{suffix}")
            if self.logger:
                self.logger.info("[behavior] Applied behavior_modifiers to BrowseWeb",
                                 details=modifiers)
            break


def run(cluster_size=DEFAULT_CLUSTER_SIZE, task_interval=DEFAULT_TASK_INTERVAL,
        group_interval=DEFAULT_GROUP_INTERVAL, extra=None):
    """Convenience function to run MCHP agent."""
    agent = MCHPAgent(
        cluster_size=cluster_size,
        task_interval=task_interval,
        group_interval=group_interval,
        extra=extra,
    )
    agent.run()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='MCHP Human Emulation Agent')
    parser.add_argument('--clustersize', type=int, default=DEFAULT_CLUSTER_SIZE)
    parser.add_argument('--taskinterval', type=int, default=DEFAULT_TASK_INTERVAL)
    parser.add_argument('--taskgroupinterval', type=int, default=DEFAULT_GROUP_INTERVAL)
    parser.add_argument('--extra', nargs='*', default=[])
    args = parser.parse_args()

    run(
        cluster_size=args.clustersize,
        task_interval=args.taskinterval,
        group_interval=args.taskgroupinterval,
        extra=args.extra,
    )
