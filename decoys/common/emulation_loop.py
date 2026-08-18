"""
Base emulation loop for all RUSE brain agents.

Provides the shared cluster-based execution pattern:
  cluster → inter-task delays → workflow selection → inter-cluster delays

Subclasses implement brain-specific behavior:
  _load_workflows()              — load workflow objects
  _execute_workflow(workflow)     — run a single workflow (brain-specific API)
  _apply_brain_specific_config() — apply brain-specific behavioral config
  _agent_type_label()            — "mchp", "browseruse_loop", "smolagents_loop"
"""

import copy
import hashlib
import random
import signal
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from time import sleep, monotonic
from typing import Optional

from common.behavioral_config import MODE_FEEDBACK, MODE_CONTROLS


class BaseEmulationLoop(ABC):
    """Abstract base class for RUSE brain emulation loops."""

    def __init__(
        self,
        cluster_size: int = 5,
        task_interval: int = 30,
        group_interval: int = 600,
        logger=None,
        calibration_profile: Optional[str] = None,
        seed: int = 42,
        behavior_config_dir: Optional[str] = None,
        config_key: Optional[str] = None,
        initial_behavior_snapshot=None,
    ):
        self.seed = seed
        self.cluster_size = cluster_size
        self.task_interval = task_interval
        self.group_interval = group_interval
        self.logger = logger
        self.calibration_profile = calibration_profile
        self._phase_timing = None  # CalibratedTiming instance, or None for baselines
        self._tasks_completed = 0
        self._behavior_config_dir = behavior_config_dir
        self._config_key = config_key
        # SHA-256 of the last behavior.json that was successfully applied.
        # Cluster boundaries can occur frequently; identical bytes are a no-op
        # so reloads do not repeatedly mutate workflows or restart agents.
        self._behavior_config_digest = None
        # R1-only immutable V2 candidate.  V2 actuator/gate consumption is
        # deliberately deferred to R3; this pointer is swapped only after a
        # complete schema, semantic, capability, and static-reload validation.
        self._behavior_v2_snapshot = initial_behavior_snapshot
        self._loaded_contract_version = (
            getattr(initial_behavior_snapshot, "contract_version", None)
        )
        # Immutable, post-load workflow defaults. Every hot-reload is rendered
        # from these values so removing a PHASE override restores the original
        # behavior instead of leaving stale state behind.
        self._config_attr_baselines = {}
        self._prompt_baselines = {}
        self._workflow_weights = None
        # Phase 2 — PHASE-emitted content.schedule parsed into 24-element
        # array of per-hour workflow_weights lists (parallel to self.workflows).
        # None when PHASE shipped no schedule; flat self._workflow_weights then
        # governs selection.
        self._schedule_by_hour = None
        self._diversity_config = None
        self._background_svc = None
        # Phase 3 — scripted protocol probes (smb/ldap/imap/doh/mdns/failed_conn).
        # Created lazily on first feedback reload; toggled via per-service
        # *_enabled booleans under diversity.background_services.
        self._scripted_svc = None
        # PersistentSession daemon (diversity.persistent_sessions). Unlike D4 /
        # scripted-svc (inline, window-gated), this runs in its OWN thread so it
        # can hold TLS sessions open + open new ones during the loop's inter-
        # window sleeps. Created once on first feedback reload when enabled.
        self._persistent_svc = None
        # Closed-loop connection-shape controller (Phase 1, 2026-06-16). Owns
        # per-connection orig_bytes/duration sampling for the persistent-session
        # channel and the conn_state_mix failed_conn rate for scripted_services.
        # Created on first feedback reload when PHASE ships connection_shape
        # (enabled) or conn_state_mix; None otherwise (and always for controls).
        self._shape_controller = None
        # Universal shape-floor channel (Build #5, 2026-06-25). Own-thread twin of
        # the persistent-session daemon: opens coverage-driven synthetic shaped
        # connections (count from the controller) so the SHAPED conns become a
        # super-majority of the per-conn mass and drag the aggregate orig_bytes/
        # duration median up to the human target. Created on first feedback reload
        # when connection_shape is enabled; reuses the persistent_sessions
        # endpoint_pool. None for controls / when not shaping.
        self._floor_svc = None
        # Per-workflow execution-rate governor (PHASE workflow_budget, 2026-08-14).
        # Paces workflow executions to content.schedule[*].workflow_budget.
        # target_execs_per_hour so total connection spend lands on the emitted
        # connection budget — weights say the MIX, execs/hour say HOW OFTEN.
        # Inactive (no-op) until PHASE ships a workflow_budget block.
        from common.exec_governor import ExecGovernor
        self._exec_governor = ExecGovernor(logger)
        # 24-elem per-hour workflow_budget blocks, parsed from content.schedule
        # alongside _schedule_by_hour. None when PHASE hasn't shipped budgets.
        self._budget_by_hour = None
        self._recent_workflows = []
        self._cluster_distinct = set()
        self._cluster_remaining = 0
        # Window-mode contract state (PHASE 2026-05-08).
        # Mirrors BehavioralConfig fields so the emulation loop can gate
        # cluster execution without re-walking behavior.json each tick.
        self._mode = None  # MODE_FEEDBACK / MODE_CONTROLS — set on first reload
        self._volume_target = None  # target_conn_per_minute_during_active
        # Soft fence deadline: if set, the cluster's inner loop must not
        # spawn a new workflow once monotonic() exceeds this. Reset every
        # cluster boundary; None outside windows.
        self._cluster_deadline_ts = None

        self.workflows = []
        self._running = False

        # Defer CalibratedTiming init if behavioral configs will provide variance/activity
        # — otherwise we'd emit transient startup warnings before _reload_behavioral_config()
        # re-creates it with the proper variance_config and activity_config dicts.
        if self.calibration_profile and not self._behavior_config_dir:
            self._init_calibrated_timing()

    # ── Abstract methods (subclasses must implement) ─────────────────

    @abstractmethod
    def _load_workflows(self) -> list:
        """Load and return workflow objects for this brain."""
        ...

    @abstractmethod
    def _execute_workflow(self, workflow) -> bool:
        """Execute a single workflow. Return True on success, False on failure."""
        ...

    @abstractmethod
    def _apply_brain_specific_config(self, fc) -> None:
        """Apply brain-specific parts of behavioral config (e.g., page_dwell, task_weights)."""
        ...

    def _baseline_attr(self, workflow, attr: str):
        """Return a workflow attribute's immutable pre-feedback value."""
        key = (id(workflow), attr)
        if key not in self._config_attr_baselines:
            self._config_attr_baselines[key] = copy.deepcopy(getattr(workflow, attr))
        return copy.deepcopy(self._config_attr_baselines[key])

    def _apply_prompt_overlay(
        self, prompt_type, default_task: str, augmentation: str, *, reset_agent: bool
    ) -> tuple[int, int]:
        """Replace PHASE guidance without accumulating it across reloads.

        Returns ``(workflows_with_prompts, workflows_changed)``. An omitted
        augmentation restores each workflow's original task/content pair.
        """
        applied = 0
        changed = 0
        for workflow in self.workflows:
            if not hasattr(workflow, "prompts"):
                continue
            key = id(workflow)
            current = workflow.prompts
            if key not in self._prompt_baselines:
                self._prompt_baselines[key] = (
                    getattr(current, "task", default_task) if current else default_task,
                    (getattr(current, "content", "") or "") if current else "",
                )
            base_task, base_content = self._prompt_baselines[key]
            content = base_content
            if augmentation:
                overlay = f"[PHASE Behavioral Guidance]\n{augmentation}"
                content = f"{base_content}\n\n{overlay}" if base_content else overlay
            current_pair = (
                getattr(current, "task", None) if current else None,
                getattr(current, "content", None) if current else None,
            )
            desired_pair = (base_task, content)
            if current_pair != desired_pair:
                workflow.prompts = prompt_type(task=base_task, content=content)
                if reset_agent and hasattr(workflow, "_agent"):
                    workflow._agent = None
                changed += 1
            applied += 1
        return applied, changed

    @abstractmethod
    def _agent_type_label(self) -> str:
        """Return agent type string for logging (e.g., 'mchp', 'browseruse_loop')."""
        ...

    # ── Timing initialization ────────────────────────────────────────

    def _init_calibrated_timing(self):
        """Initialize calibrated timing from an empirical profile."""
        from common.timing.phase_timing import CalibratedTiming, load_calibration_profile
        config = load_calibration_profile(self.calibration_profile)
        self._phase_timing = CalibratedTiming(config)
        print(f"Calibrated timing ({self.calibration_profile}) - activity level: {self._phase_timing.get_activity_level()}")

    # ── Timing helpers ───────────────────────────────────────────────

    def _get_cluster_size(self) -> int:
        if self._phase_timing:
            return self._phase_timing.get_cluster_size()
        return random.randint(1, self.cluster_size)

    def _get_task_delay(self) -> float:
        if self._phase_timing:
            return self._phase_timing.get_task_delay()
        return random.randrange(self.task_interval)

    def _get_cluster_delay(self) -> float:
        if self._phase_timing:
            if self._phase_timing.should_take_break(self._tasks_completed):
                self._tasks_completed = 0
                return self._phase_timing.get_break_duration()
            return self._phase_timing.get_cluster_delay()
        return random.randrange(self.group_interval)

    # ── Behavioral config reload ─────────────────────────────────────

    def _reload_behavioral_config(self):
        """Reload behavioral config from disk (hot-swap support)."""
        if not self._behavior_config_dir or not self._config_key:
            self._workflow_weights = None
            return

        from pathlib import Path
        from common.behavioral_config import (
            load_behavioral_config, build_workflow_weights,
            build_calibrated_timing_config,
        )

        config_path = Path(self._behavior_config_dir) / "behavior.json"
        try:
            digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
        except OSError:
            # Preserve load_behavioral_config's detailed fail-loud diagnostic.
            digest = None
        if digest is not None and digest == self._behavior_config_digest:
            return False

        # load_behavioral_config raises RuntimeError if behavior.json is
        # missing — service crash-loops, audit surfaces it. No legacy
        # baseline path: every SUP must have a config.
        fc = load_behavioral_config(
            Path(self._behavior_config_dir),
            self._config_key,
            previous_v2=self._behavior_v2_snapshot,
        )

        from common.behavior_v2 import BehaviorV2Snapshot
        if isinstance(fc, BehaviorV2Snapshot):
            if (
                self._loaded_contract_version is not None
                and self._loaded_contract_version != fc.contract_version
            ):
                raise RuntimeError(
                    "hot reload cannot change behavior contract version; restart required"
                )
            # R1 activation is one immutable pointer assignment.  No V2 field
            # is projected into the legacy mutable consumers here: budget
            # schedulers, ledgers, and unified gating are separately gated
            # R2/R3 work.
            self._behavior_v2_snapshot = fc
            self._loaded_contract_version = fc.contract_version
            self._behavior_config_digest = fc.raw_sha256
            if self.logger:
                self.logger.info(
                    "[behavior] Validated V2 snapshot",
                    details={
                        "config_key": self._config_key,
                        "contract_version": fc.contract_version,
                        "raw_sha256": fc.raw_sha256,
                        "brain": fc.brain,
                        "enabled_workflows": list(fc.enabled_workflows),
                    },
                )
            return True

        if self._behavior_v2_snapshot is not None:
            raise RuntimeError(
                "V2 hot reload cannot change contract version; restart required"
            )

        # Stash mode for the gate + cluster loop.
        self._mode = fc.mode
        self._volume_target = fc.target_conn_per_minute_during_active
        n_windows = len(fc.active_minute_windows or [])
        on_minutes = sum(e - s for s, e in (fc.active_minute_windows or []))

        # Summary log
        if self.logger:
            self.logger.info(
                f"[behavior] Config reload mode={fc.mode}",
                details={
                    "config_key": self._config_key,
                    "mode": fc.mode,
                    "contract_version": fc.contract_version or "legacy-unversioned",
                    "n_windows": n_windows,
                    "on_minutes": on_minutes,
                    "target_conn_per_min": fc.target_conn_per_minute_during_active,
                    "hard_fence_seconds": fc.hard_fence_seconds,
                },
            )

        # Workflow weights + brain-specific config (feedback only — controls
        # has its own content schema and bypasses the workflow-weights path).
        if fc.mode == MODE_FEEDBACK:
            self._workflow_weights = build_workflow_weights(self.workflows, fc)
            if self._workflow_weights and self.logger:
                self.logger.info(
                    f"[behavior] Loaded workflow_weights for {self._config_key}",
                    details={"weights": fc.workflow_weights})
            # Phase 2 — content.schedule replaces flat workflow_weights when
            # present. Parsed into a 24-elem per-hour weights list; fail-loud
            # if any hour 0..23 is not covered.
            self._schedule_by_hour = self._build_schedule_by_hour(
                fc.schedule, strict=bool(fc.contract_version))
            if self._schedule_by_hour and self.logger:
                self.logger.info(
                    f"[behavior] Loaded content.schedule for {self._config_key}",
                    details={"n_blocks": len(fc.schedule)})
            # PHASE task-value engine (2026-08-14) — per-block workflow_budget.
            # Additive: absent budget leaves the governor inactive and selection
            # behaves exactly as it did before.
            self._budget_by_hour = self._build_budget_by_hour(fc.schedule)
            if self._budget_by_hour and self.logger:
                self.logger.info(
                    f"[behavior] Loaded content.schedule workflow_budget for "
                    f"{self._config_key}",
                    details={"hours_governed":
                             sum(1 for b in self._budget_by_hour if b)})
        else:
            self._workflow_weights = None
            self._schedule_by_hour = None
            self._budget_by_hour = None
        self._exec_governor.update_budget(self._current_workflow_budget())
        self._apply_brain_specific_config(fc)

        # CalibratedTiming setup. Both modes carry burst_percentiles +
        # variance — controls' is hardcoded floor, feedback's is PHASE-tuned.
        # Build it for both so the gate has access to current_window/fence.
        if fc.timing_profile:
            from common.timing.phase_timing import CalibratedTiming
            old_last_activity = (self._phase_timing._last_activity_time
                                 if self._phase_timing else None)
            # No try/except. Earlier this swallowed KeyError/TypeError and
            # set _phase_timing=None — which silently disabled the window
            # gate AND D4 deficit-burst because both depend on _phase_timing.
            # Result: every feedback SUP ran 24/7 ungated and bg-counter
            # logged in_window=0 forever. If the schema is malformed, the
            # build call must fail loud so the deploy/audit catches it.
            config = build_calibrated_timing_config(fc.timing_profile)
            self._phase_timing = CalibratedTiming(
                config,
                variance_config=fc.variance_injection,
            )
            self._phase_timing._last_activity_time = old_last_activity
            if self.logger:
                self.logger.info("[behavior] Hot-swapped timing_profile",
                                 details={"dataset": config.dataset})
        elif self.calibration_profile and self._phase_timing is None:
            from common.timing.phase_timing import CalibratedTiming, load_calibration_profile
            config = load_calibration_profile(self.calibration_profile)
            self._phase_timing = CalibratedTiming(config)
            print(f"Calibrated timing ({self.calibration_profile}) - activity level: {self._phase_timing.get_activity_level()}")
        elif self._phase_timing and fc.variance_injection:
            self._phase_timing.update_variance_config(fc.variance_injection)

        # Diversity is replacement-based. Empty or removed blocks explicitly
        # disable previously active channels instead of leaving daemon state
        # from an older behavior.json running indefinitely.
        self._diversity_config = fc.diversity_injection or {}
        bg_config = self._diversity_config.get("background_services", {})
        if bg_config or self._background_svc is not None:
            if self._background_svc is None:
                from common.background_services import BackgroundServiceGenerator
                self._background_svc = BackgroundServiceGenerator(bg_config, self.logger)
            else:
                self._background_svc.update_config(bg_config)
        # Phase 3 — same bg_config dict carries the *_enabled toggles.
        if bg_config or self._scripted_svc is not None:
            if self._scripted_svc is None:
                from common.network.scripted_services import ScriptedServiceScheduler
                self._scripted_svc = ScriptedServiceScheduler(bg_config, self.logger)
            else:
                self._scripted_svc.update_config(bg_config)

        # PersistentSession daemon — stop on omission/disable and restart if a
        # later contract re-enables it.
        ps_config = self._diversity_config.get("persistent_sessions") or {}
        ps_enabled = bool(ps_config.get("enabled"))
        if ps_enabled:
            if self._persistent_svc is None:
                from common.network.persistent_session import PersistentSessionDaemon
                self._persistent_svc = PersistentSessionDaemon(
                    ps_config, self.logger, seed=self.seed)
            else:
                self._persistent_svc.update_config(ps_config, seed=self.seed)
            self._persistent_svc.start()
        elif self._persistent_svc is not None:
            self._persistent_svc.update_config({}, seed=self.seed)
            self._persistent_svc.stop()

        # Keep a disabled controller object across reloads so channel references
        # remain stable, but reset all targets when its contract disappears.
        shape_cfg = fc.connection_shape
        csm_cfg = fc.conn_state_mix
        shape_on = bool(shape_cfg and shape_cfg.get("enabled")) or bool(csm_cfg)
        if shape_on or self._shape_controller is not None:
            if self._shape_controller is None:
                from common.network.shape_controller import ShapeController
                self._shape_controller = ShapeController(
                    shape_cfg, csm_cfg, self.logger, seed=self.seed)
            else:
                self._shape_controller.update_config(shape_cfg, csm_cfg)
            self._shape_controller.set_conn_budget_per_min(
                self._budget_conns_per_min())
        if self._persistent_svc is not None:
            self._persistent_svc.set_controller(self._shape_controller)
        if self._scripted_svc is not None:
            self._scripted_svc.set_controller(self._shape_controller)

        # Shape floor follows connection_shape.enabled exactly.
        shape_enabled = bool(shape_cfg and shape_cfg.get("enabled"))
        if shape_enabled and self._shape_controller is not None:
            floor_cfg = {
                "enabled": True,
                "endpoint_pool": ps_config.get("endpoint_pool", []),
                "keepalive_interval_seconds": ps_config.get(
                    "keepalive_interval_seconds"),
                "active_minute_windows": fc.active_minute_windows or [],
            }
            if self._floor_svc is None:
                from common.network.shape_floor import ShapeFloorDaemon
                self._floor_svc = ShapeFloorDaemon(
                    floor_cfg, self.logger, seed=self.seed)
            else:
                self._floor_svc.update_config(floor_cfg, seed=self.seed)
            self._floor_svc.set_controller(self._shape_controller)
            self._floor_svc.start()
        elif self._floor_svc is not None:
            self._floor_svc.update_config({}, seed=self.seed)
            self._floor_svc.set_controller(None)
            self._floor_svc.stop()

        # Push window contract — both modes consume it identically.
        if self._phase_timing is not None:
            self._phase_timing.update_window_contract(
                windows=fc.active_minute_windows,
                hard_fence_seconds=fc.hard_fence_seconds,
                min_window_minutes=fc.min_window_minutes,
                window_mode=fc.mode,
            )

        # Feature status report — always print so you know what's active
        rotation = (self._diversity_config or {}).get("workflow_rotation", {})
        min_distinct = rotation.get("min_distinct_per_cluster", 0)
        max_consec = rotation.get("max_consecutive_same", 0)
        has_bg_svc = bool(bg_config)
        has_prompt_aug = bool(fc.prompt_augmentation and fc.prompt_augmentation.get("prompt_content"))

        # Section-absent status lines. Under the two-shapes contract (2026-05-08,
        # commit 8f91240a) PHASE deleted _metadata.ablation_gate along with the
        # 3-gate pipeline, so these sections (workflow_rotation, background_services,
        # prompt_augmentation, workflow_weights) are OPTIONAL by design — PHASE
        # legitimately omits them in the feedback/controls schema. Their absence
        # is canonical, NOT an anomaly: a genuinely broken feedback doc is caught
        # earlier with a hard fail (missing behavior.json / wrong mode / bad JSON).
        # So report [INFO], not [WARNING]. Tagging [WARNING] here keyed on the now-
        # deleted ablation_gate contract and flooded the audit with false failures.
        tag = "[INFO]"
        reason_suffix = " (optional — omitted by PHASE per two-shapes contract)"

        if min_distinct == 0:
            print(f"{tag} D2 min_distinct_per_cluster DISABLED — "
                  f"no diversity_injection.workflow_rotation.min_distinct_per_cluster"
                  f"{reason_suffix}")
        if max_consec == 0:
            print(f"{tag} D2 max_consecutive_same DISABLED — "
                  f"no diversity_injection.workflow_rotation.max_consecutive_same"
                  f"{reason_suffix}")
        if not has_bg_svc:
            print(f"{tag} D4 background services DISABLED — "
                  f"no diversity_injection.background_services"
                  f"{reason_suffix}")
        if not has_prompt_aug:
            print(f"{tag} G1 prompt_augmentation DISABLED — "
                  f"no prompt_augmentation.prompt_content"
                  f"{reason_suffix}")
        # W4: workflow_weights absent on a non-empty feedback config = partial
        # PHASE output. PHASE v2 (2026-05-08) moved weights into per-hour
        # content.schedule[*].workflow_weights blocks; the legacy top-level
        # content.workflow_weights is no longer emitted. Warn only when BOTH
        # paths are absent — that's a truly stale / pre-v2 config. Per-hour
        # OFF blocks ({}) are intentional night-idle and handled by the
        # schedule OFF gate downstream, not flagged here.
        if not fc.workflow_weights and not fc.schedule:
            print(f"{tag} W4 workflow_weights DISABLED — "
                  f"no content.workflow_weights or content.schedule, "
                  f"using uniform random selection"
                  f"{reason_suffix}")
        # W3 site_config: consumer wired 2026-04-27 (SmolAgents BrowseWebWorkflow
        # filters its task pool by category using content.site_categories
        # weights — see SmolAgentLoop._apply_brain_specific_config). Previous
        # "UNUSED" INFO line removed. BrowserUse + MCHP do not consume
        # site_config; if wired later, the [INFO] guard belongs in their
        # respective _apply_brain_specific_config paths, not here.

        # Commit only after every consumer accepted the new contract. A failed
        # apply is retried on the next boundary instead of being mistaken for a
        # successfully consumed version.
        self._behavior_config_digest = digest
        self._loaded_contract_version = (
            fc.contract_version or "legacy-unversioned"
        )
        return True

    # ── Workflow selection ────────────────────────────────────────────

    def _build_schedule_by_hour(self, schedule, *, strict: bool = False):
        """Parse content.schedule into 24-element per-hour weights list.

        schedule is a list of {"hour_range": [a, b], "workflow_weights": {...}}.
        Hour ranges are half-open [a, b). Per-block workflow_weights map
        workflow names to floats; missing workflows weight 0.

        Returns None when input is None/empty. Raises RuntimeError fail-loud
        if any hour 0..23 is not covered, hour_range is malformed, or every
        block's weights sum to 0.
        """
        if not schedule:
            return None
        by_hour = [None] * 24
        for block in schedule:
            try:
                lo, hi = block["hour_range"]
                ww = block["workflow_weights"]
                lo_i = int(lo)
                hi_i = int(hi)
            except (KeyError, TypeError, ValueError) as e:
                msg = (
                    f"[FATAL] content.schedule block malformed "
                    f"(missing/bad hour_range or workflow_weights): {block!r} ({e})"
                )
                print(msg, flush=True)
                raise RuntimeError(msg)
            if not 0 <= lo_i < hi_i <= 24:
                msg = (
                    f"[FATAL] content.schedule hour_range must satisfy "
                    f"0 <= start < end <= 24, got {[lo_i, hi_i]}"
                )
                print(msg, flush=True)
                raise RuntimeError(msg)
            if strict:
                known = {
                    getattr(w, 'name', '') or w.__class__.__name__
                    for w in self.workflows
                }
                unknown = sorted(set(ww) - known)
                if unknown:
                    msg = (
                        f"[FATAL] content.schedule uses unsupported workflow "
                        f"name(s): {unknown}; supported by this SUP: {sorted(known)}"
                    )
                    print(msg, flush=True)
                    raise RuntimeError(msg)
            block_weights = [
                float(ww.get(getattr(w, 'name', '') or w.__class__.__name__, 0.0))
                for w in self.workflows
            ]
            # PHASE convention (2026-05-20): empty workflow_weights {} OR a
            # dict whose values all sum to 0 means OFF for this hour — the
            # SUP is allowed by active_minute_windows but should not pick
            # any workflow this tick. Store an empty list as the sentinel
            # (distinct from None=unassigned) so _select_workflow can
            # detect OFF and the loop skips workflow execution. NOT a
            # schema error.
            if sum(block_weights) <= 0:
                block_weights = []  # OFF sentinel
            for h in range(lo_i, hi_i):
                if by_hour[h] is not None:
                    msg = (
                        f"[FATAL] content.schedule assigns UTC hour {h} more "
                        f"than once; ranges must not overlap"
                    )
                    print(msg, flush=True)
                    raise RuntimeError(msg)
                by_hour[h] = block_weights
        missing = [h for h, w in enumerate(by_hour) if w is None]
        if missing:
            msg = (
                f"[FATAL] content.schedule does not cover all 24 hours — "
                f"missing hours (UTC): {missing}. Every hour must be assigned."
            )
            print(msg, flush=True)
            raise RuntimeError(msg)
        return by_hour

    def _budget_conns_per_min(self):
        """Total connection budget for the current hour, in conn/min, or None.

        Prefers the per-block workflow_budget.target_conns_per_hour (PHASE's
        solved budget) and falls back to timing.target_conn_per_minute_during_
        active. None means "no budget expressed" — channels keep their prior
        uncapped behavior.
        """
        budget = self._current_workflow_budget()
        if budget:
            per_hour = budget.get("target_conns_per_hour")
            try:
                if per_hour is not None and float(per_hour) > 0:
                    return float(per_hour) / 60.0
            except (TypeError, ValueError):
                pass
        try:
            if self._volume_target is not None and float(self._volume_target) > 0:
                return float(self._volume_target)
        except (TypeError, ValueError):
            pass
        return None

    def _build_budget_by_hour(self, schedule):
        """Parse content.schedule[*].workflow_budget into a 24-element per-hour
        list (PHASE task-value engine, 2026-08-14).

        Unlike _build_schedule_by_hour this is ADDITIVE and NON-FATAL: PHASE
        emits workflow_budget only on ACTIVE blocks and only on lineages that
        carry it, so an absent budget (or an uncovered hour) simply leaves that
        hour ungoverned rather than failing the deploy. Malformed hour_range is
        already fail-loud in _build_schedule_by_hour, which runs first.

        Returns None when no block carries a budget.
        """
        if not schedule:
            return None
        by_hour = [None] * 24
        found = False
        for block in schedule:
            if not isinstance(block, dict):
                continue
            budget = block.get("workflow_budget")
            if not isinstance(budget, dict) or not budget:
                continue
            try:
                lo_i, hi_i = (int(v) for v in block["hour_range"])
            except (KeyError, TypeError, ValueError):
                continue
            for h in range(lo_i, hi_i):
                if 0 <= h < 24:
                    by_hour[h] = budget
                    found = True
        return by_hour if found else None

    def _current_workflow_budget(self):
        """workflow_budget block for the current UTC hour, or None."""
        if not self._budget_by_hour:
            return None
        return self._budget_by_hour[datetime.now(timezone.utc).hour]

    def _current_workflow_weights(self):
        """Return the weights list to use right now (parallel to self.workflows).

        Schedule (Phase 2) wins when present — looks up current UTC hour.
        Returns the empty list [] as the OFF sentinel when the schedule has
        an empty workflow_weights block for this hour (PHASE OFF semantic).
        Otherwise falls back to the flat self._workflow_weights. None when
        neither is configured (caller picks uniform).

        When the exec governor is active (PHASE shipped target_execs_per_hour),
        workflows that have spent their budget are masked to weight 0 here —
        the single choke point both _select_workflow and
        _select_workflow_with_rotation already read, so rotation penalties
        continue to apply to whatever remains eligible. An all-zero result is
        the "budget exhausted" case, which _exec_budget_blocked() catches
        before selection is ever attempted.
        """
        if self._schedule_by_hour:
            weights = self._schedule_by_hour[datetime.now(timezone.utc).hour]
        else:
            weights = self._workflow_weights
        if self._exec_governor.active and weights != []:
            # weights == [] is the schedule OFF sentinel — leave it alone.
            # Synthesize uniform weights when none are configured so the mask
            # still applies (otherwise selection would fall back to uniform
            # over ALL workflows and bypass the budget entirely).
            if not weights:
                weights = [1.0] * len(self.workflows)
            eligible = self._exec_governor.eligible(
                getattr(w, 'name', '') for w in self.workflows)
            weights = [
                wt if getattr(w, 'name', '') in eligible else 0.0
                for w, wt in zip(self.workflows, weights)
            ]
        return weights

    def _exec_budget_blocked(self) -> bool:
        """True when the exec governor is active and no workflow currently holds
        budget — the rate-limit analogue of _schedule_off_for_now().

        The loop skips workflow execution for this tick (background channels
        still fire on their own budgets) and comes back after the inter-task
        sleep, by which time credits have accrued. This is what turns PHASE's
        target_execs_per_hour into an actual pace: a 0.001/hr workflow simply
        never wins a tick, instead of firing every time weights pick it.
        """
        if not self._exec_governor.active:
            return False
        if self._schedule_off_for_now():
            return False  # already handled by the schedule OFF gate
        eligible = self._exec_governor.eligible(
            getattr(w, 'name', '') for w in self.workflows)
        return not eligible

    def _schedule_off_for_now(self):
        """True iff content.schedule is configured AND the current UTC hour's
        workflow_weights sums to zero (an intentional OFF block per PHASE).

        Main loop gates workflow execution on this — D4 background services
        and Phase 3 scripted services still fire (they have their own
        scheduling), but no Workflow is selected this tick. The SUP idles
        through the off-hour even with active_minute_windows open.
        """
        if not self._schedule_by_hour:
            return False
        hour_weights = self._schedule_by_hour[datetime.now(timezone.utc).hour]
        return hour_weights == []  # OFF sentinel

    def _select_workflow(self):
        """Select next workflow using diversity rotation, weights, or uniform random."""
        if self._diversity_config:
            return self._select_workflow_with_rotation()
        weights = self._current_workflow_weights()
        if weights:
            return random.choices(self.workflows, weights=weights, k=1)[0]
        return self.workflows[random.randrange(len(self.workflows))]

    def _select_workflow_with_rotation(self):
        """Select workflow with diversity-aware rotation.

        Enforces two constraints from diversity_injection.workflow_rotation:
        - max_consecutive_same: penalizes N identical picks in a row
        - min_distinct_per_cluster (D2): near cluster end, penalizes already-seen
          workflows to force diversity within the cluster
        """
        rotation = (self._diversity_config or {}).get("workflow_rotation", {})
        max_consec = rotation.get("max_consecutive_same", 99)
        min_distinct = rotation.get("min_distinct_per_cluster", 0)

        current = self._current_workflow_weights()
        weights = list(current) if current else [1.0] * len(self.workflows)

        # Penalize consecutive same workflow
        if len(self._recent_workflows) >= max_consec:
            last_name = self._recent_workflows[-1]
            if all(w == last_name for w in self._recent_workflows[-max_consec:]):
                for i, w in enumerate(self.workflows):
                    if getattr(w, 'name', '') == last_name:
                        weights[i] *= 0.1

        # D2: Near cluster end, force diversity if below minimum distinct count
        if min_distinct > 0 and self._cluster_remaining > 0:
            needed = min_distinct - len(self._cluster_distinct)
            if needed > 0 and self._cluster_remaining <= needed:
                for i, w in enumerate(self.workflows):
                    if getattr(w, 'name', '') in self._cluster_distinct:
                        weights[i] *= 0.01  # strong penalty, not zero (graceful)

        workflow = random.choices(self.workflows, weights=weights, k=1)[0]
        name = getattr(workflow, 'name', '')
        self._recent_workflows.append(name)
        if len(self._recent_workflows) > 10:
            self._recent_workflows.pop(0)
        self._cluster_distinct.add(name)
        self._cluster_remaining -= 1
        return workflow

    # ── Main emulation loop ──────────────────────────────────────────

    # Cap on a single sleep-until-next-window. Shorter than the longest
    # gap so the reload tick fires (and PHASE re-rolls / hot-patches land)
    # at least every CAP_S even during long idle stretches.
    _WINDOW_GATE_SLEEP_CAP_S = 30 * 60  # 30 minutes
    # Minimum remaining-in-window required before starting a cluster.
    # Below this we sleep through the window end rather than spawn a
    # workflow that can't complete inside the active period (gemma's slow
    # path takes 60-120s; 90s is the floor that keeps us inside the
    # window with margin).
    _START_ONLY_FLOOR_S = 90
    # Cap how often the IDLE_ALL_DAY loop wakes to re-check config.
    _IDLE_ALL_DAY_TICK_S = 30 * 60

    def _window_gate_sleep_then_continue(self) -> bool:
        """Window-mode gate. Both feedback and controls modes consume the
        gate identically — the only difference is the windows themselves
        (feedback emits 5–15 narrow windows; controls emits a single
        60-min slot). Returns True if the loop should `continue` (sleep
        happened); False if execution should proceed to run a cluster.

        States:
          outside any window → sleep until next start (capped)  → True
          inside, remaining < 90s+fence → sleep through end     → True
          inside, runway OK → set cluster deadline              → False
          no _phase_timing or no windows → fall through         → False
        """
        if self._phase_timing is None or not self._phase_timing.has_windows():
            self._cluster_deadline_ts = None
            return False

        if self._phase_timing.current_window() is None:
            # Outside any window — sleep until the next one starts.
            wait = self._phase_timing.time_until_next_window_start()
            wait = min(wait, self._WINDOW_GATE_SLEEP_CAP_S)
            wait = max(wait, 1.0)
            if self.logger:
                self.logger.info(
                    f"[window] outside windows — sleeping {wait/60:.1f}min "
                    f"until next start (capped at "
                    f"{self._WINDOW_GATE_SLEEP_CAP_S//60}min)",
                    details={"wait_s": wait})
            sleep(wait)
            return True

        # Inside a window. Check remaining vs start-only floor.
        remaining = self._phase_timing.time_until_window_end() or 0.0
        hard_fence = self._phase_timing._hard_fence_seconds
        usable = max(0.0, remaining - hard_fence)
        if usable < self._START_ONLY_FLOOR_S:
            if self.logger:
                self.logger.info(
                    f"[window] only {usable:.0f}s usable in current "
                    f"window (< {self._START_ONLY_FLOOR_S}s floor) — "
                    f"sleeping through end",
                    details={"remaining_s": remaining,
                             "hard_fence_s": hard_fence})
            sleep(remaining + 1.0)
            return True

        self._cluster_deadline_ts = monotonic() + usable
        return False

    def _emulation_loop(self):
        """Main emulation loop — runs workflows in clusters."""
        while self._running:
            self._reload_behavioral_config()
            # Budget selection is hour-dependent even when behavior.json bytes
            # are unchanged, so keep this small runtime update outside the
            # digest-gated reload path.
            if self._shape_controller is not None:
                self._shape_controller.set_conn_budget_per_min(
                    self._budget_conns_per_min())

            # Window-mode gate (PHASE 2026-05-08). Identical behavior for
            # both feedback and controls modes — the windows themselves
            # carry the difference. Sleep until next window if outside;
            # otherwise set a cluster deadline and fall through.
            if self._window_gate_sleep_then_continue():
                continue

            # Push window-state to D4 background services so deficit-burst
            # tops up bg-conn rate to target_conn_per_minute_during_active
            # while inside an active window. Outside a window (LEGACY /
            # BASELINE), in_window=False disables the burst — bg-svc
            # falls back to its hour-rate behavior.
            if self._background_svc is not None:
                # Net-out: pass the daemon's per-minute open count so D4's
                # deficit-burst tops up to target MINUS what PersistentSession
                # already opened — total stays at target instead of stacking,
                # and the mix shifts from dns/http toward ssl. Read-by-value of
                # an int on the main thread → race-free (D4 stays passive).
                external = (self._persistent_svc.opens_in_current_minute()
                            if self._persistent_svc is not None else 0)
                # Build #5: floor opens also count as already-emitted external
                # conns so D4 tops up to target MINUS (psess + floor) — total
                # volume stays near target while the mix shifts to shaped ssl.
                external += (self._floor_svc.opens_in_current_minute()
                             if self._floor_svc is not None else 0)
                self._background_svc.set_window_state(
                    in_window=self._cluster_deadline_ts is not None,
                    volume_target=self._volume_target,
                    external_conns=external,
                )

            # Log activity level
            if self._phase_timing:
                activity_level = self._phase_timing.get_activity_level()
                current_hour = datetime.now(timezone.utc).hour
                print(f"[{datetime.now().strftime('%H:%M')}] Activity level: {activity_level} (UTC hour {current_hour})")
                if self.logger:
                    self.logger.info(f"Activity level: {activity_level}", details={
                        "hour": current_hour, "level": activity_level
                    })

            cluster_size = self._get_cluster_size()

            # D2: Reset per-cluster diversity tracking
            self._cluster_distinct = set()
            self._cluster_remaining = cluster_size

            if self.logger:
                self.logger.decision(
                    choice="cluster_size",
                    selected=str(cluster_size),
                    context="Tasks to run in this cluster",
                    method="calibrated" if self._phase_timing else "random"
                )

            for _ in range(cluster_size):
                # Soft fence (option B): if the cluster's deadline has passed,
                # don't start a new workflow. Lets in-flight workflows finish
                # naturally — they'll overshoot the window by ≤max_steps × per-
                # step_delay, typically 30-60s, which is acceptable.
                if (self._cluster_deadline_ts is not None
                        and monotonic() >= self._cluster_deadline_ts):
                    if self.logger:
                        self.logger.info(
                            "[window] cluster deadline reached — "
                            "skipping remaining workflows in cluster",
                            details={"deadline_ts": self._cluster_deadline_ts})
                    break

                task_delay = self._get_task_delay()
                if self.logger:
                    self.logger.timing_delay(task_delay, reason="inter_task")
                sleep(task_delay)

                # Re-check fence after the inter-task sleep — task_delay
                # can be tens of seconds.
                if (self._cluster_deadline_ts is not None
                        and monotonic() >= self._cluster_deadline_ts):
                    if self.logger:
                        self.logger.info(
                            "[window] cluster deadline reached during "
                            "inter-task sleep — skipping remainder")
                    break

                # Background service traffic
                if self._background_svc:
                    self._background_svc.maybe_generate()

                # Phase 1 shape controller — minute-roll tick. Also driven from
                # the persistent-session daemon's 1s thread (reliable cadence);
                # this call covers the case where the daemon isn't running but
                # conn_state_mix still needs its failed_conn rate refreshed.
                # maybe_tick is minute-roll-guarded → idempotent across callers.
                if self._shape_controller:
                    self._shape_controller.maybe_tick()

                # Phase 3 — scripted protocol probes. Cron-style schedule;
                # cheap when no service is enabled or current minute isn't
                # on any schedule.
                if self._scripted_svc:
                    self._scripted_svc.maybe_run()

                # Phase 2 — schedule OFF gate. PHASE may emit empty
                # workflow_weights {} for OFF hours (e.g. night blocks).
                # Skip workflow execution this tick — D4 + scripted services
                # already fired above so passive traffic continues. Loop
                # comes back next iteration after the inter-task sleep.
                if self._schedule_off_for_now():
                    if self.logger:
                        hour = datetime.now(timezone.utc).hour
                        self.logger.info(
                            f"[schedule] hour={hour} UTC is OFF "
                            f"(empty workflow_weights) — skipping workflow")
                    continue

                # PHASE workflow_budget rate gate (2026-08-14). Refresh the
                # governor for the current hour's block (schedule blocks change
                # under a long-running cluster) then skip the tick when every
                # workflow has spent its budget. Background channels already
                # fired above on their own budgets, so passive traffic
                # continues — this throttles workflow spend only.
                self._exec_governor.update_budget(self._current_workflow_budget())
                if self._exec_budget_blocked():
                    if self.logger:
                        self.logger.info(
                            "[exec-budget] all workflows over their "
                            "target_execs_per_hour — skipping workflow",
                            details=self._exec_governor.status())
                    continue

                # Select workflow
                workflow = self._select_workflow()
                if workflow is not None:
                    self._exec_governor.consume(getattr(workflow, 'name', ''))
                # The `workflow` log field is the canonical workflow name
                # (matches content.workflow_weights keys — see
                # behavioral_config.build_workflow_weights, which keys on
                # workflow.name). The human-readable task/description goes in
                # params so logs stay joinable to PHASE-emitted weights.
                workflow_name = workflow.name
                workflow_desc = workflow.description

                if self.logger:
                    workflow_options = [w.name for w in self.workflows]
                    self.logger.decision(
                        choice="workflow_selection",
                        options=workflow_options,
                        selected=workflow.name,
                        context=workflow_desc,
                        method=(
                            "schedule_block" if self._schedule_by_hour
                            else "behavior_weighted" if self._workflow_weights
                            else "random"
                        )
                    )

                print(workflow.display)

                if self.logger:
                    params = {
                        "agent_type": self._agent_type_label(),
                        "description": workflow_desc,
                        "phase_timing": self._phase_timing is not None,
                    }
                    if hasattr(workflow, 'category'):
                        params["category"] = workflow.category
                    self.logger.workflow_start(workflow_name, params=params)

                success = self._execute_workflow(workflow)

                # Build #5 instrument: report the outcome so the controller can
                # log a per-minute completion ratio next to the shape p50s — a
                # drop is the signal the floor is starving browse-workflows of
                # connections (T too aggressive).
                if self._shape_controller is not None:
                    self._shape_controller.note_workflow(bool(success))

                if success:
                    self._tasks_completed += 1
                    if self._phase_timing:
                        self._phase_timing.record_activity()

            # Inter-cluster delay
            group_delay = self._get_cluster_delay()
            if self.logger:
                self.logger.timing_delay(group_delay, reason="inter_cluster")
            sleep(group_delay)

    # ── Lifecycle ────────────────────────────────────────────────────

    def run(self):
        """Start the emulation loop."""
        # Seed source: PHASE _metadata.seed (peeked + applied in sup/__main__.py
        # before this is reached) → config.seed → constructor self.seed.
        if self.seed != 0:
            random.seed(self.seed)
        else:
            random.seed()

        self.workflows = self._load_workflows()
        self._reload_behavioral_config()

        if self._behavior_v2_snapshot is not None:
            raise RuntimeError(
                "V2 behavior validated and dispatched, but V2 actuation remains "
                "disabled until separately authorized R2/R3 runtime work"
            )

        if not self.workflows:
            print("Error: No workflows loaded!")
            if self.logger:
                self.logger.error("No workflows loaded", fatal=True)
            return

        self._running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        label = self._agent_type_label()
        print(f"\nStarting {label} with {len(self.workflows)} workflows")
        print(f"PHASE timing: {self._phase_timing is not None}")
        if not self._phase_timing is not None:
            print(f"Timing: cluster_size={self.cluster_size}, task_interval={self.task_interval}, group_interval={self.group_interval}")
        print("-" * 60)

        if self.logger:
            self.logger.info(f"{label} started", details={
                "workflow_count": len(self.workflows),
                "phase_timing": self._phase_timing is not None,
            })

        try:
            self._emulation_loop()
        except KeyboardInterrupt:
            self.stop()
            sys.exit(0)

    def stop(self):
        """Stop the emulation and cleanup workflows."""
        if not self._running:
            return
        self._running = False
        label = self._agent_type_label()
        print(f"\nTerminating {label}...")
        if self.logger:
            self.logger.info(f"{label} terminating")
        # Stop the persistent-session + shape-floor threads first so their sockets
        # FIN-close cleanly (-> Zeek conn_state=SF) before the process exits.
        if self._persistent_svc is not None:
            try:
                self._persistent_svc.stop()
            except Exception:
                pass
        if self._floor_svc is not None:
            try:
                self._floor_svc.stop()
            except Exception:
                pass
        for workflow in self.workflows:
            try:
                workflow.cleanup()
            except Exception:
                pass

    def _signal_handler(self, sig, frame):
        self.stop()
        sys.exit(0)
