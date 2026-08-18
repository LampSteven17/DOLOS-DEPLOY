"""
Behavioral configuration loader.

Loads a single behavior.json per SUP for hot-swappable behavior adjustment.
PHASE's Feedback Engine emits one file per SUP config at:

  {feedback_source}/{behavior_dir}/{config_key}/behavior.json

e.g. axes-decoy-controls_axes-summer24_std-ctrls/B.gemma/B0.gemma/behavior.json

At runtime the deploy playbook copies it into:

  /opt/ruse/deployed_sups/<config_key>/behavioral_configurations/behavior.json

Directory auto-discovery:
  1. RUSE_BEHAVIOR_CONFIG_DIR env var
  2. /opt/ruse/deployed_sups/<config_key>/behavioral_configurations/  (prod)
  3. <project_root>/deployed_sups/<config_key>/behavioral_configurations/ (dev)

If --behavior-config-dir points at an experiment-level dir (containing
M/, B.gemma/, S.gemma/ subdirs), the behavior subdir is auto-resolved
from config_key. Pointing at a SUP-level dir is also supported.
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List


# PHASE feedback contract (post 2026-05-08). Two shapes, one switch on
# _metadata.mode. Both shapes carry timing.active_minute_windows + the
# four window-contract fields; the difference is what content/behavior
# fields each shape populates.
MODE_FEEDBACK = "feedback"   # full PHASE-tuned schema (workflow_weights, site_categories, ...)
MODE_CONTROLS = "controls"   # hardcoded floor (google_search_pool + browse_url_pool, fixed page_fetch, no LLM)
BEHAVIOR_CONTRACT_V1 = "ruse.decoy.behavior/v1"
SUPPORTED_BEHAVIOR_CONTRACTS = frozenset({BEHAVIOR_CONTRACT_V1})


@dataclass
class BehavioralConfig:
    """Behavioral configuration for a single SUP."""
    # PHASE-emitted mode discriminator. Always "feedback" or "controls" post
    # 2026-05-08; load_behavioral_config raises FATAL on any other value.
    mode: str = MODE_FEEDBACK
    # Optional during the PHASE migration. Once present it is strict: RUSE
    # refuses versions it does not implement rather than guessing semantics.
    contract_version: Optional[str] = None

    # Per-target seed (PHASE-emitted _metadata.seed). When present, overrides
    # the CLI --seed default so each (config, dataset) pair has a stable but
    # distinct RNG. None means PHASE didn't ship a seed; CLI default applies.
    seed: Optional[int] = None

    # Window contract — present in BOTH shapes.
    # UTC minute-of-day [start, end) half-open ranges. Feedback emits 5–15
    # windows; controls emits a single 60-minute slot.
    active_minute_windows: Optional[List[List[int]]] = None
    # Volume target during active windows (conn/min). Driven by D4 deficit-
    # burst. Feedback ~1500; controls ~2.0 (baseline floor).
    target_conn_per_minute_during_active: Optional[float] = None
    # PHASE-owned contract values; RUSE reads, never overrides.
    min_window_minutes: Optional[int] = None
    hard_fence_seconds: Optional[int] = None
    # Source metadata for the window selection (methods, budget, selection
    # rule). Logged only — no runtime behavior depends on it.
    active_minute_window_source: Optional[dict] = None

    # FEEDBACK-shape fields. Present only when mode == "feedback"; None
    # otherwise. Brain runners gate consumption on `mode`.
    workflow_weights: Optional[dict] = None     # {"BrowseWeb": 0.45, ...}
    # Phase 2 — PHASE-emitted content.schedule: list of time blocks where
    # each block has {hour_range: [a, b], workflow_weights: {...}}. When
    # present, overrides the flat workflow_weights above. RUSE validates
    # 0..23 hour coverage at reload time and aborts fail-loud on gaps.
    schedule: Optional[List[dict]] = None
    behavior_modifiers: Optional[dict] = None   # {"page_dwell": {...}, ...}
    site_config: Optional[dict] = None          # content.site_categories flat dict
    prompt_augmentation: Optional[dict] = None  # {"prompt_content": "..."}
    timing_profile: Optional[dict] = None       # raw timing block (burst_percentiles, etc.)
    variance_injection: Optional[dict] = None   # timing.variance — cluster_size_sigma + idle_gap_sigma + hourly_std_targets
    diversity_injection: Optional[dict] = None   # background_services + workflow_rotation + topology_mimicry
    # Phase 4: download_url_pool may be either list[str] (legacy flat pool)
    # OR dict[str, list[str]] with keys {small, medium, large}. RUSE picks
    # a bucket via download_size_mix when the dict shape is shipped.
    download_url_pool: Optional[object] = None  # content.download_url_pool — for DownloadFiles
    download_size_mix: Optional[dict] = None    # behavior.download.size_mix
    download_outcome_mix: Optional[dict] = None  # behavior.download.outcome_mix
    whois_domain_pool: Optional[List[str]] = None  # content.whois_domain_pool — for WhoisLookup
    # Phase 1 pools — PHASE ships per-target curated lists, RUSE wraps as tasks
    # (BU/Smol) or uses directly (MCHP). Each is None when PHASE hasn't shipped
    # the field yet; consumer workflows fall back to hard-coded task lists.
    browse_url_pool: Optional[List[str]] = None    # content.browse_url_pool — URLs for browse_web
    youtube_video_pool: Optional[List[str]] = None  # content.youtube_video_pool — video IDs for browse_youtube
    google_search_pool: Optional[List[str]] = None  # content.google_search_pool — queries for web/google search
    ablation_gate: Optional[dict] = None        # informational; missing sections are deliberate when present
    # Closed-loop connection-shape contract (Phase 1, 2026-06-16). PHASE-emitted
    # under diversity.* — per-connection target percentile distributions and the
    # conn_state outcome mix. Both absent by default (additive; OFF when missing).
    # connection_shape: {enabled, orig_bytes/{p25,p50,p75,p90,max}, resp_bytes,
    #   orig_pkts, resp_pkts, duration}. RUSE honors orig_bytes + duration on the
    #   persistent-session channel today; orig_pkts/resp_* await later builds.
    # conn_state_mix: {SF, failed_conn, OTH, RSTR} target fractions (FOLD: REJ+S0
    #   collapsed into failed_conn; SF is the uncontrolled baseline, reference only).
    connection_shape: Optional[dict] = None     # diversity.connection_shape
    conn_state_mix: Optional[dict] = None        # diversity.conn_state_mix

    # CONTROLS-shape fields. Present only when mode == "controls"; None
    # otherwise. The controls runner consumes these to drive a fixed
    # no-LLM browse loop. Note: google_search_pool + browse_url_pool are
    # shared with feedback mode — see fields above.
    page_fetch_interval_seconds: Optional[int] = None    # content.page_fetch_interval_seconds
    llm_calls_enabled: Optional[bool] = None             # content.llm_calls_enabled (always False in controls)
    tool_pool: Optional[List[str]] = None                # behavior.tool_pool — usually ["web_browse"]
    enable_download: Optional[bool] = None               # behavior.enable_download
    enable_whois: Optional[bool] = None                  # behavior.enable_whois
    active_hour: Optional[int] = None                    # _metadata.active_hour (UTC) — derives windows
    off_hour: Optional[int] = None                       # _metadata.off_hour (UTC)

    def is_empty(self) -> bool:
        # Window contract is always present in valid configs; an empty config
        # is impossible post 2026-05-08 (FATAL fires before construction).
        return self.active_minute_windows is None



    def topology_mimicry(self) -> Optional[dict]:
        """Return PHASE's topology_mimicry rates for this SUP, or None.

        Populated by PHASE as diversity.topology_mimicry. Consumed off-host
        by the per-deploy neighborhood sidecar VM — the SUP itself does not
        act on these values. Sidecar aggregates each SUP's rates into a
        sups.json master config and synthesizes inbound TCP/UDP probes
        targeting each SUP IP.

        See docs/topology-mimicry.md for the full rate schema and rationale.
        """
        if not self.diversity_injection:
            return None
        return self.diversity_injection.get("topology_mimicry")

    def is_ablation_gated(self) -> bool:
        """True if PHASE's ablation engine gated any sections off.

        Used by warning emitters to distinguish intentional omissions
        from unexpected ones.
        """
        if not self.ablation_gate:
            return False
        # ablation is active if any feature is in inactive/flat_zero OR
        # gating_features is set (the presence of gate metadata at all
        # means PHASE consciously decided what to include).
        return bool(
            self.ablation_gate.get("inactive")
            or self.ablation_gate.get("flat_zero")
            or self.ablation_gate.get("gating_features")
        )


def config_key_to_behavior_dir(config_key: str) -> str:
    """
    Map a SUP config key to its PHASE behavior directory name.

    M1-M4 -> 'M', B0-B4.llama -> 'B.llama', S0-S4.gemma -> 'S.gemma', etc.
    C-infix (CPU variants: B2C.gemma) and R-infix (RTX variants:
    B2R.gemma) collapse to the base behavior_dir — PHASE only ships
    one .gemma source per family, and tier variants reuse it.
    Controls (C0, M0) return the key unchanged.
    """
    m = re.match(r'^([A-Z])\d+[A-Z]*(?:\.(\w+))?$', config_key)
    if not m:
        return config_key
    return f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)


def _resolve_behavior_subdir(path: Path, config_key: str) -> Path:
    """Resolve an experiment-level dir to the SUP-level subdir, or return as-is.

    If `path` contains behavior.json it's already a SUP dir — use directly.
    Otherwise drill one level via config_key_to_behavior_dir() / config_key.
    """
    if (path / "behavior.json").exists():
        return path

    behavior = config_key_to_behavior_dir(config_key)
    subdir = path / behavior / config_key
    if subdir.is_dir():
        return subdir
    # Fall back to {behavior}/ if operator gave an already-narrowed dir
    subdir = path / behavior
    if subdir.is_dir():
        return subdir

    return path


def resolve_behavioral_config_dir(config_key: str, override_dir: Optional[str] = None) -> Path:
    """
    Resolve the behavioral configurations directory path using auto-discovery.

    Supports pointing at either a behavior directory (has *.json files)
    or an experiment directory (has M/, B.llama/, etc. subdirs).

    Args:
        config_key: SUP config key (e.g., 'M3', 'B3.gemma')
        override_dir: Optional explicit path (from --behavior-config-dir CLI flag)

    Returns:
        Path to the behavioral configurations directory (created if it doesn't exist)
    """
    if override_dir:
        path = Path(override_dir).expanduser()
        resolved = _resolve_behavior_subdir(path, config_key)
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    # 1. Check env var
    env_dir = os.environ.get("RUSE_BEHAVIOR_CONFIG_DIR")
    if env_dir:
        path = Path(env_dir).expanduser()
        resolved = _resolve_behavior_subdir(path, config_key)
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    # 2. Check deployed path
    deployed_base = Path("/opt/ruse/deployed_sups")
    if deployed_base.exists():
        path = deployed_base / config_key / "behavioral_configurations"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # 3. Development fallback: relative to project root
    project_root = Path(__file__).parent.parent.parent
    path = project_root / "deployed_sups" / config_key / "behavioral_configurations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def apply_phase_seed(config, behavior_config_dir: Path) -> Optional[int]:
    """Read _metadata.seed from PHASE behavior.json and apply process-wide.

    Each brain runner calls this BEFORE creating AgentLogger so the session_id
    derives from the PHASE seed (deterministic per-(config, dataset)) and
    Ollama LLM output is seeded consistently. systemd invokes the runners
    directly (not sup/__main__.py), so the override has to live here.

    Sets: random.seed, os.environ['SUP_OLLAMA_SEED'], config.seed.
    Returns the applied seed, or None if PHASE shipped no seed.
    """
    import os
    import random as _rand
    seed = peek_seed(behavior_config_dir)
    if seed is None:
        return None
    if config is not None and getattr(config, "seed", None) != seed:
        # flush=True so the line lands in systemd.log immediately rather
        # than waiting for Python's stdout block buffer to fill.
        print(f"  PHASE _metadata.seed={seed} overrides config.seed="
              f"{getattr(config, 'seed', None)}", flush=True)
    if config is not None:
        config.seed = seed
    _rand.seed(seed)
    os.environ["SUP_OLLAMA_SEED"] = str(seed)
    return seed


def peek_seed(config_dir: Path) -> Optional[int]:
    """Read just _metadata.seed from behavior.json without full validation.

    Used by BaseEmulationLoop.run() to override the CLI default seed BEFORE
    random.seed() is called, so the full behavioral config reload happens
    against a stable RNG state. Missing file / malformed JSON / absent seed
    field all return None (caller falls back to CLI default).
    """
    path = config_dir / "behavior.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    seed = (data.get("_metadata") or {}).get("seed")
    if seed is None:
        return None
    try:
        return int(seed)
    except (TypeError, ValueError):
        return None


def load_workflow_gates(config_dir: Path) -> dict:
    """Read just the {enable_whois, enable_download} flags from behavior.json.

    Used by brain workflow loaders to decide whether to register the
    feedback-content workflows (whois_lookup, download_files) BEFORE the
    full BehavioralConfig load. PHASE's dumb_baseline emits both as false
    to signal a single-workflow degenerate mode; PHASE feedback proper
    emits true (or omits, which we default to true).

    Missing file or malformed JSON returns defaults (both True) and lets
    the subsequent load_behavioral_config() raise the loud fail message.
    No partial validation here — this is just a peek at two booleans.
    """
    path = config_dir / "behavior.json"
    if not path.exists():
        return {"enable_whois": True, "enable_download": True}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"enable_whois": True, "enable_download": True}
    beh = data.get("behavior") or {}
    return {
        "enable_whois":    bool(beh.get("enable_whois", True)),
        "enable_download": bool(beh.get("enable_download", True)),
    }


def load_behavioral_config(config_dir: Path, config_key: str) -> BehavioralConfig:
    """Load the consolidated behavior.json for a SUP.

    Every DECOY SUP (except C0/M0 controls, which have no service that reads
    behavior.json) MUST have a behavior.json. The deploy system writes either
    the controls defaults or PHASE-tuned feedback into
    /opt/ruse/deployed_sups/<key>/behavioral_configurations/behavior.json
    before starting the service.

    Missing file → loud RuntimeError. The brain service crash-loops; audit
    surfaces the failure on the next sweep. Previously this returned an
    empty BehavioralConfig and agents fell through to hardcoded defaults,
    which silently masked broken distribution and left no signal that
    feedback never landed.

    Malformed JSON also raises (json.JSONDecodeError) — same fail-loud
    semantics, same diagnostic path.
    """
    path = config_dir / "behavior.json"
    if not path.exists():
        msg = (
            f"\n\n"
            f"==============================================================\n"
            f"  DECOY BEHAVIORAL CONFIG MISSING — REFUSING TO START\n"
            f"==============================================================\n"
            f"  config_key:        {config_key}\n"
            f"  expected location: {path}\n"
            f"\n"
            f"  Every DECOY SUP must have a behavior.json before service\n"
            f"  start. The deploy pipeline either failed to distribute it,\n"
            f"  or someone removed it post-deploy.\n"
            f"\n"
            f"  Fix:\n"
            f"    1. Re-run distribute-behavior-configs.yaml against this VM, OR\n"
            f"    2. Re-deploy the SUP from scratch.\n"
            f"\n"
            f"  Do NOT fall back to legacy / hardcoded defaults — feedback\n"
            f"  must be verified end-to-end on every run.\n"
            f"==============================================================\n"
        )
        # Print to stderr so it lands in systemd_error.log even if logger
        # isn't configured yet, then raise to crash the service.
        import sys
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(
            f"behavior.json missing for {config_key} at {path}"
        )

    # File present. A malformed JSON here means the source emitted a broken
    # file or the copy corrupted it — fail loud, don't silently degrade.
    with open(path, "r") as f:
        data = json.load(f)

    timing = data.get("timing") or {}
    content = data.get("content") or {}
    behavior = data.get("behavior") or {}
    metadata = data.get("_metadata") or {}
    mode = metadata.get("mode")
    contract_version = metadata.get("contract_version")

    if (contract_version is not None
            and contract_version not in SUPPORTED_BEHAVIOR_CONTRACTS):
        supported = ", ".join(sorted(SUPPORTED_BEHAVIOR_CONTRACTS))
        raise RuntimeError(
            f"behavior.json contract unsupported for {config_key} at {path}: "
            f"{contract_version!r} (supported: {supported})"
        )

    # Mode dispatch (PHASE 2026-05-08). PHASE consolidated to two shapes —
    # "feedback" (full schema) and "controls" (hardcoded floor). Every
    # other value is a FATAL contract violation: schema regression, hand
    # edit, or version skew. Silent fallback is forbidden.
    if mode not in (MODE_FEEDBACK, MODE_CONTROLS):
        msg = (
            f"\n\n"
            f"==============================================================\n"
            f"  DECOY MODE CONTRACT VIOLATED — REFUSING TO START\n"
            f"==============================================================\n"
            f"  config_key:    {config_key}\n"
            f"  path:          {path}\n"
            f"  _metadata.mode: {mode!r}\n"
            f"\n"
            f"  PHASE-emitted behavior.json must declare _metadata.mode\n"
            f"  as exactly one of:\n"
            f"    {MODE_FEEDBACK!r}   → full PHASE-tuned schema\n"
            f"    {MODE_CONTROLS!r}   → hardcoded baseline floor\n"
            f"\n"
            f"  Anything else is a contract violation (schema regression,\n"
            f"  hand-edit, parallel writer). Silent fallback is forbidden\n"
            f"  — experiment validity depends on the mode being explicit.\n"
            f"\n"
            f"  Fix:\n"
            f"    1. Re-run PHASE feedback so the new schema is emitted, OR\n"
            f"    2. Update PHASE's feedback engine to set _metadata.mode.\n"
            f"==============================================================\n"
        )
        import sys
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(
            f"behavior.json mode contract violated for {config_key} "
            f"at {path} (mode={mode!r}, expected 'feedback' or 'controls')"
        )

    # Window contract — both shapes carry it. Coerce to list-of-list.
    raw_windows = timing.get("active_minute_windows") or []
    active_minute_windows = [list(w) for w in raw_windows]

    target_rate = timing.get("target_conn_per_minute_during_active")
    if target_rate is not None:
        try:
            target_rate = float(target_rate)
        except (TypeError, ValueError):
            target_rate = None

    raw_seed = metadata.get("seed")
    try:
        parsed_seed = int(raw_seed) if raw_seed is not None else None
    except (TypeError, ValueError):
        parsed_seed = None

    fc = BehavioralConfig(
        mode=mode,
        contract_version=contract_version,
        seed=parsed_seed,
        active_minute_windows=active_minute_windows,
        target_conn_per_minute_during_active=target_rate,
        min_window_minutes=timing.get("min_window_minutes"),
        hard_fence_seconds=timing.get("hard_fence_seconds"),
        active_minute_window_source=timing.get("active_minute_window_source"),
    )

    if mode == MODE_FEEDBACK:
        # Full PHASE-tuned schema.
        prompt: Optional[dict] = None
        if "prompt_content" in data:
            prompt = {"prompt_content": data["prompt_content"]}

        download_url_pool = content.get("download_url_pool") or None
        whois_domain_pool = content.get("whois_domain_pool") or None

        fc.timing_profile = timing or None
        fc.variance_injection = timing.get("variance")
        fc.workflow_weights = content.get("workflow_weights")
        fc.schedule = content.get("schedule") or None
        fc.site_config = content.get("site_categories")
        fc.behavior_modifiers = behavior or None
        fc.diversity_injection = data.get("diversity")
        fc.prompt_augmentation = prompt
        fc.download_url_pool = download_url_pool
        download_block = (behavior.get("download") or {})
        fc.download_size_mix = download_block.get("size_mix") or None
        fc.download_outcome_mix = download_block.get("outcome_mix") or None
        fc.whois_domain_pool = whois_domain_pool
        fc.browse_url_pool = content.get("browse_url_pool") or None
        fc.youtube_video_pool = content.get("youtube_video_pool") or None
        fc.google_search_pool = content.get("google_search_pool") or None
        fc.ablation_gate = metadata.get("ablation_gate")
        # Closed-loop shape contract (Phase 1). Both live under diversity.* and
        # are passed through verbatim — validation/sampling lives in the
        # ShapeController consumer (warn-loud + fallback on malformed, never a
        # fail-loud crash, since these are additive and a producer typo must not
        # take down the fleet). Absent → None → controller never built.
        diversity = data.get("diversity") or {}
        fc.connection_shape = diversity.get("connection_shape")
        fc.conn_state_mix = diversity.get("conn_state_mix")
    else:
        # Controls shape — hardcoded floor. Single 60-min window is already
        # parsed into active_minute_windows above. Phase 1 consolidation
        # (2026-05-20): url_queries dropped in favor of google_search_pool,
        # and browse_url_pool now shipped on controls too.
        fc.google_search_pool = content.get("google_search_pool") or None
        fc.browse_url_pool = content.get("browse_url_pool") or None
        fc.page_fetch_interval_seconds = content.get("page_fetch_interval_seconds")
        fc.llm_calls_enabled = content.get("llm_calls_enabled")
        fc.tool_pool = behavior.get("tool_pool") or None
        fc.enable_download = behavior.get("enable_download")
        fc.enable_whois = behavior.get("enable_whois")
        fc.active_hour = metadata.get("active_hour")
        fc.off_hour = metadata.get("off_hour")

    return fc


def build_workflow_weights(workflows, behavioral_config: BehavioralConfig) -> Optional[List[float]]:
    """
    Build a weights list parallel to workflows for random.choices().

    Args:
        workflows: List of workflow objects (must have .name attribute)
        behavioral_config: BehavioralConfig with workflow_weights dict

    Returns:
        List of floats parallel to workflows, or None if no weights configured
    """
    if not behavioral_config.workflow_weights:
        return None

    weights = behavioral_config.workflow_weights
    if behavioral_config.contract_version:
        known = {
            getattr(w, 'name', None) or w.__class__.__name__
            for w in workflows
        }
        unknown = sorted(set(weights) - known)
        if unknown:
            raise RuntimeError(
                f"content.workflow_weights contains unsupported workflow "
                f"name(s): {unknown}; supported by this SUP: {sorted(known)}"
            )
    result = []
    for w in workflows:
        # Try workflow name first, then class name
        name = getattr(w, 'name', None) or w.__class__.__name__
        weight = weights.get(name, weights.get(w.__class__.__name__, 1.0))
        result.append(float(weight))

    # Only return if at least one non-default weight was found
    if any(name in weights for w in workflows
           for name in [getattr(w, 'name', ''), w.__class__.__name__]):
        return result
    return None


def build_calibrated_timing_config(timing_profile: dict):
    """Build a CalibratedTimingConfig from the behavior.json timing section.

    timing_profile is the raw `timing` dict from behavior.json:
      active_minute_windows: [[start,end), ...]   # hard 0/1 schedule per minute
      target_conn_per_minute_during_active: float # rate inside ON-windows
      min_window_minutes:    int
      hard_fence_seconds:    int
      burst_percentiles:
        connections_per_burst: {"5":..,"25":..,"50":..,"75":..,"95":..,"max":..}
        idle_gap_minutes:      {"5":..,"25":..,"50":..,"75":..,"95":..}
        burst_duration_minutes:{"5":..,"25":..,"50":..,"75":..,"95":..}
      hourly_distribution:   OPTIONAL — vestigial from the pre-window schema.

    Window-mode (2026-05-08) replaced the soft hourly-fractions approach
    with hard active_minute_windows + per-minute target rate. PHASE no
    longer emits hourly_distribution because windows fully specify the
    schedule (off outside windows; target N conn/min inside). If it's
    absent, default to uniform — the window gate enforces the real
    schedule, so hourly is a no-op anyway.
    """
    from common.timing.phase_timing import CalibratedTimingConfig

    burst = timing_profile["burst_percentiles"]
    hourly = timing_profile.get("hourly_distribution") or [1.0 / 24] * 24
    return CalibratedTimingConfig(
        dataset="feedback",
        hourly_fractions=hourly,
        burst_duration=burst["burst_duration_minutes"],
        idle_gap=burst["idle_gap_minutes"],
        connections_per_burst=burst["connections_per_burst"],
    )
