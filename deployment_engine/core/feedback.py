"""Feedback source auto-detection and config generation."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import output


# Rampart and GHOSTS retain their established source tree. Canonical Decoy
# feedback is emitted independently by PHASE under DECOY_FEEDBACK_BASE.
FEEDBACK_BASE = Path("/mnt/AXES2U1/feedback")
DECOY_FEEDBACK_BASE = Path("/data/axes-mirror/feedback")
DECOY_FEEDBACK_TARGETS = (
    "axes-fall24",
    "axes-spring25",
    "axes-spring26",
    "axes-summer24",
    "cptc11-zeektx",
    "vt-fall21",
    "vt-spring22",
    "vt-summer21",
)
DECOY_FEEDBACK_SUP_CONFIGS = (
    "scripted-cpu",
    "mchp-cpu",
    "browseruse-gpu",
    "smolagents-gpu",
)
DECOY_FEEDBACK_DEPLOYMENTS = (
    {"behavior": "scripted-cpu", "flavor": "v1.14vcpu.28g", "count": 1},
    {"behavior": "mchp-cpu", "flavor": "v1.14vcpu.28g", "count": 1},
    {"behavior": "browseruse-gpu", "flavor": "v100-1gpu.14vcpu.28g", "count": 1},
    {"behavior": "smolagents-gpu", "flavor": "v100-1gpu.14vcpu.28g", "count": 1},
)
DECOY_GENERATION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}Z$")


class FeedbackSourceError(RuntimeError):
    """A selected PHASE feedback generation cannot be deployed."""


def _is_decoy_generation_name(value: str) -> bool:
    if not DECOY_GENERATION_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d_%H%MZ")
    except ValueError:
        return False
    return True


def validate_decoy_feedback_generation(source_dir: Path) -> None:
    """Validate exactly four canonical plans through the runtime loader."""
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise FeedbackSourceError(f"feedback generation is not a directory: {source_dir}")
    if not _is_decoy_generation_name(source_dir.name):
        raise FeedbackSourceError(
            f"feedback generation must match YYYY-MM-DD_HHMMZ: {source_dir}"
        )

    expected = {
        f"{sup_config}_behavior.json" for sup_config in DECOY_FEEDBACK_SUP_CONFIGS
    }
    actual = {path.name for path in source_dir.glob("*_behavior.json")}
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise FeedbackSourceError(
            f"feedback generation must contain exactly the four canonical "
            f"behavior files ({'; '.join(details)}): {source_dir}"
        )

    from decoys.phase_workflow.loader import WorkflowPlanError, load_workflow_plan

    for sup_config in DECOY_FEEDBACK_SUP_CONFIGS:
        behavior_path = source_dir / f"{sup_config}_behavior.json"
        try:
            load_workflow_plan(behavior_path, sup_config)
        except WorkflowPlanError as exc:
            raise FeedbackSourceError(
                f"invalid {sup_config} plan {behavior_path}: {exc}"
            ) from exc


def find_decoy_feedback_by_target(target: str, preset: str) -> Path:
    """Select and validate one exact target's lexically newest generation."""
    if target not in DECOY_FEEDBACK_TARGETS:
        raise FeedbackSourceError(
            f"unknown Decoy feedback target {target!r}; expected one of "
            f"{', '.join(DECOY_FEEDBACK_TARGETS)}"
        )
    target_dir = DECOY_FEEDBACK_BASE / preset / target
    if not target_dir.is_dir():
        raise FeedbackSourceError(f"feedback target directory not found: {target_dir}")
    generations = sorted(
        path for path in target_dir.iterdir()
        if path.is_dir() and _is_decoy_generation_name(path.name)
    )
    if not generations:
        raise FeedbackSourceError(
            f"no YYYY-MM-DD_HHMMZ generation found for target {target}: {target_dir}"
        )
    selected = generations[-1]
    # Deliberately validate only the newest timestamp. An invalid newest
    # generation is a hard failure, never an implicit rollback.
    validate_decoy_feedback_generation(selected)
    return selected


def find_all_decoy_feedback_sources(preset: str) -> list[dict]:
    """Resolve all eight required targets in deterministic target order."""
    preset_root = DECOY_FEEDBACK_BASE / preset
    if not preset_root.is_dir():
        raise FeedbackSourceError(f"feedback preset directory not found: {preset_root}")
    return [
        {
            "path": find_decoy_feedback_by_target(target, preset),
            "target": target,
            "preset": preset,
        }
        for target in DECOY_FEEDBACK_TARGETS
    ]


def decoy_feedback_source_identity(source_dir: Path) -> tuple[str, str]:
    """Return the exact (preset, target) encoded by a generation path."""
    source_dir = Path(source_dir)
    validate_decoy_feedback_generation(source_dir)
    target = source_dir.parent.name
    preset = source_dir.parent.parent.name
    if target not in DECOY_FEEDBACK_TARGETS:
        raise FeedbackSourceError(
            f"feedback source parent is not a canonical target: {source_dir.parent}"
        )
    if not preset:
        raise FeedbackSourceError(f"feedback source has no preset parent: {source_dir}")
    return preset, target


def _decoy_preset_slug(preset: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", preset.lower()).strip("-")
    if not slug:
        raise FeedbackSourceError(f"invalid feedback preset name: {preset!r}")
    return slug
WORKFLOW_CAPABILITIES_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "phase-workflow-plan-v1"
    / "capabilities-v1.json"
)

# Slots inside {type}-controls/ that are NOT feedback datasets. The
# `controls` slot is the new authoritative location for the baseline
# behavior.json files (PHASE feedback_engine.dumb_baseline output);
# config.yaml's behavior_source already points at it for the decoy-controls
# deployment, so picking it up here would deploy a redundant feedback
# variant on top of the baseline.
BASELINE_DATASET_SLOTS = {"controls"}

# Post 2026-04-23 layout:
#   /mnt/AXES2U1/feedback/
#     ├── decoy-controls/{dataset}/...
#     ├── rampart-controls/{dataset}/...
#     └── ghosts-controls/{dataset}/...
# Each dataset dir contains a manifest.json + per-SUP/NPC/node configs.
# The type subtree is selected via _type_root(deploy_type).


# ── Manifest helpers (post 2026-04-23 /mnt/AXES2U1/feedback layout) ─────
#
# PHASE now writes a manifest.json alongside each generated feedback source.
# It's a provenance index — deploy_key, training_dataset, version_preset,
# model_name, generated_at_utc, active_features_union, per-SUP ok/skipped
# status. Not a config itself (nested per-SUP files still carry the knobs),
# but indispensable for operator confirmation: "am I about to deploy the
# right feedback, freshly generated, for the right target?"

def load_manifest(source: Path) -> dict | None:
    """Load manifest.json from a PHASE source dir. Returns None if missing/malformed.

    Missing manifest is not a failure — older sources (and hand-built dev
    dirs) won't have one. Caller decides how strict to be.
    """
    mf = source / "manifest.json"
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _format_age(iso_ts: str) -> str:
    """Format how long ago an ISO-8601 UTC timestamp was, as '(12m ago)' etc."""
    import datetime
    try:
        gen_dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    if gen_dt.tzinfo is None:
        gen_dt = gen_dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    secs = int((now - gen_dt).total_seconds())
    if secs < 60:
        return f"({secs}s ago)"
    if secs < 3600:
        return f"({secs // 60}m ago)"
    if secs < 86400:
        return f"({secs // 3600}h ago)"
    return f"({secs // 86400}d ago)"


_BRAIN_NAMES = {"M": "MCHP", "B": "BrowserUse", "S": "SmolAgents"}


def _vm_runtime_details(behavior: str) -> tuple[str, str]:
    """Return (brain_name, llm_model) for a behavior key.

    Brain: M → MCHP (no LLM), B → BrowserUse, S → SmolAgents.
    LLM: R-infix → gemma4:e4b (RTX 2080 Ti edge), C-infix → gemma4:e2b
    (CPU edge), no infix → gemma4:26b (V100), M-brain → '—' (no LLM).
    """
    prefix = behavior.split(".")[0]
    brain = _BRAIN_NAMES.get(prefix[0], "?")
    if prefix.startswith("M"):
        return (brain, "—")
    if "R" in prefix:
        model = "gemma4:e4b"
    elif "C" in prefix:
        model = "gemma4:e2b"
    else:
        model = "gemma4:26b"
    return (brain, model)


# Pure-control behaviors aren't brain SUPs — _vm_runtime_details would
# mislabel them (C0's "C" reads as a CPU-edge LLM). Map them explicitly.
_CONTROL_BEHAVIOR_LABELS = {
    "C0": ("bare ubuntu", "—"),
    "M0": ("MITRE pyhuman", "—"),
}


def _legacy_controls_vm_runtime_details(behavior: str) -> tuple[str, str]:
    """Return runtime details for controls outside the workflow contract."""
    special = _CONTROL_BEHAVIOR_LABELS.get(behavior.split(".")[0])
    return special if special else _vm_runtime_details(behavior)


def _workflow_control_runtime_details(
    sup_config: str, capabilities: dict,
) -> tuple[str, str] | None:
    """Read one canonical control's brain and model from the contract."""
    configurations = capabilities.get("sup_configurations")
    if not isinstance(configurations, dict) or sup_config not in configurations:
        return None
    configuration = configurations[sup_config]
    brain = configuration.get("brain")
    if not isinstance(brain, str) or not brain:
        raise ValueError(f"invalid Brain capability for {sup_config}")

    # Convert canonical contract identifiers to their operator casing without
    # maintaining a second SUP-configuration map.
    words = re.sub(r"(use|agents)$", r"-\1", brain).split("-")
    brain_label = "".join(
        word.upper() if word == "mchp" else word.capitalize()
        for word in words
    )

    profiles = capabilities.get("brain_profiles", {}).get(sup_config, {})
    profile = profiles.get("control", {}) if isinstance(profiles, dict) else {}
    model = profile.get("model") if isinstance(profile, dict) else None
    if model is None:
        model_label = "—"
    elif isinstance(model, dict) and isinstance(model.get("ollama"), str):
        model_label = model["ollama"]
    else:
        raise ValueError(f"invalid model capability for {sup_config}")
    return brain_label, model_label


def _render_vm_table(rows: list[tuple], indent: str) -> list[str]:
    """Aligned SUP-config/Brain/Flavor/LLM-model table from rows of
    (configuration, brain, flavor,
    model) rows. Empty list when no rows."""
    if not rows:
        return []
    headers = ("SUP config", "Brain", "Flavor", "LLM model")
    widths = [
        max(len(headers[i]), max(len(row[i]) for row in rows))
        for i in range(4)
    ]
    fmt = f"{{:<{widths[0]}}}  {{:<{widths[1]}}}  {{:<{widths[2]}}}  {{:<{widths[3]}}}"
    sep = "  ".join("─" * w for w in widths)
    lines = [f"{indent}{fmt.format(*headers)}", f"{indent}{sep}"]
    for row in rows:
        lines.append(f"{indent}{fmt.format(*row)}")
    return lines


def template_vm_table_lines(gpu_tier: str, indent: str = "    ") -> list[str]:
    """Per-VM provisioning table for a FEEDBACK tier template
    (FEEDBACK_TEMPLATES_BY_TIER[gpu_tier]). Empty list if the tier is unknown."""
    spec = FEEDBACK_TEMPLATES_BY_TIER.get(gpu_tier)
    if not spec:
        return []
    rows = []
    for vm in spec["template"]:
        brain, model = _vm_runtime_details(vm["behavior"])
        rows.append((vm["behavior"], brain, vm["flavor"], model))
    return _render_vm_table(rows, indent)


def config_vm_table_lines(deployments: list[dict], indent: str = "    ") -> list[str]:
    """Per-VM provisioning table for a config.yaml `deployments` list
    ([{behavior, flavor, count}, ...]). Used for CONTROLS, whose VMs come from
    config.yaml rather than a GPU-tier template."""
    try:
        capabilities = json.loads(
            WORKFLOW_CAPABILITIES_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"cannot read workflow capabilities {WORKFLOW_CAPABILITIES_PATH}: {exc}"
        ) from exc

    rows = []
    for dep in deployments or []:
        beh = dep.get("behavior", "?")
        details = _workflow_control_runtime_details(beh, capabilities)
        brain, model = details or _legacy_controls_vm_runtime_details(beh)
        rows.append((beh, brain, dep.get("flavor", "?"), model))
    return _render_vm_table(rows, indent)


def manifest_summary_lines(
    source: Path, manifest: dict | None, indent: str = "    ",
    gpu_tier: str | None = None,
) -> list[str]:
    """Return lines summarizing a manifest + per-VM plan for user display.

    Shape (indent applied to each line):
      target env:  axes-summer24     preset: std-ctrls
      feedback:    /mnt/AXES2U1/feedback/decoy-controls/axes-summer24
                   generated 2026-04-23T16:15:19Z  (12m ago)

      VMs to provision (5), tier=rtx:
        Behavior   Brain       Flavor                       LLM model
        ────────   ─────────   ─────────────────────────    ─────────
        M2         MCHP        v1.14vcpu.28g                —
        B2R.gemma  BrowserUse  rtx2080ti-1gpu.14vcpu.28g    gemma4:e4b
        ...
    """
    if not manifest:
        return [
            f"{indent}feedback:    {source}",
            f"{indent}(no manifest.json — legacy / dev source)",
        ]

    dataset = manifest.get("training_dataset", "?")
    preset = manifest.get("version_preset", "?")
    generated = manifest.get("generated_at_utc", "")

    age = _format_age(generated) if generated else ""
    gen_line = f"generated {generated}  {age}" if generated else "(no timestamp)"

    lines = [
        f"{indent}target env:  {dataset}       preset: {preset}",
        f"{indent}feedback:    {source}",
        f"{indent}             {gen_line}",
    ]
    if gpu_tier:
        spec = FEEDBACK_TEMPLATES_BY_TIER.get(gpu_tier)
        vm_count = len(spec["template"]) if spec else 0
        lines.append("")
        lines.append(f"{indent}VMs to provision ({vm_count}), tier={gpu_tier}:")
        lines.extend(template_vm_table_lines(gpu_tier, indent=indent + "  "))
    return lines


def validate_manifest_target(
    manifest: dict | None, deploy_type: str,
) -> str | None:
    """Assert manifest.target matches the deploy type. Returns error msg or None.

    Catches the class of bugs where an operator points a decoy deploy at a
    rampart source (the file-layout globs would still match because each
    type validates its own nested shape, but the manifest is authoritative).
    """
    if not manifest:
        return None  # no manifest = can't check, defer to layout glob
    target = manifest.get("target")
    if not target:
        return None
    expected = "decoy" if deploy_type in ("decoy", None) else deploy_type
    if target != expected:
        return (
            f"manifest.target={target!r} does not match deploy type {expected!r} — "
            f"source at {manifest.get('deploy_key', '?')} was generated for a "
            f"different target"
        )
    return None


def require_manifest_training_dataset(source: Path, deploy_type: str) -> str:
    """Return the exact PHASE model target required for a feedback config."""
    manifest = load_manifest(source)
    if manifest is None:
        output.error(f"ERROR: required manifest.json missing or invalid: {source}")
        raise SystemExit(1)
    mismatch = validate_manifest_target(manifest, deploy_type)
    if mismatch:
        output.error(f"ERROR: {mismatch}")
        raise SystemExit(1)
    training_dataset = manifest.get("training_dataset")
    if (
        not isinstance(training_dataset, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", training_dataset) is None
    ):
        output.error(
            f"ERROR: manifest.training_dataset missing or invalid: "
            f"{source / 'manifest.json'}"
        )
        raise SystemExit(1)
    return training_dataset

# GPU-conserving 5-VM feedback template (gemma4 cutover 2026-04-08):
# gemma only, V100 only (no RTX) — V100 uses gemma4:26b, CPU uses gemma4:e2b.
FEEDBACK_TEMPLATE = [
    {"behavior": "M2",        "flavor": "v1.14vcpu.28g",        "count": 1},
    {"behavior": "B2.gemma",  "flavor": "v100-1gpu.14vcpu.28g", "count": 1},
    {"behavior": "S2.gemma",  "flavor": "v100-1gpu.14vcpu.28g", "count": 1},
    {"behavior": "B2C.gemma", "flavor": "v1.14vcpu.28g",        "count": 1},
    {"behavior": "S2C.gemma", "flavor": "v1.14vcpu.28g",        "count": 1},
]

# RTX-tier 5-VM feedback template (added 2026-05-20). For targets where
# we don't expect to "fix" the score anyway (unfixable failure-mode
# representatives: human-everywhere, N-human-everywhere, anti-correlated,
# topology-fingerprint datasets). RTX 2080 Ti has 11 GB VRAM — gemma4:26b
# (~14 GB int4) doesn't fit, so R-tier SUPs use gemma4:e4b (edge 4B,
# ~3 GB int4) instead. Same model family as V100 (gemma4:26b) and CPU
# (gemma4:e2b) — three tiers of gemma4 keep results comparable.
#
# Feedback content (timing/pools/services) is portable across gemma4
# variants — the .gemma feedback PHASE ships works as the source; only
# the runtime model differs. R-tier behavior keys (B2R.gemma / S2R.gemma)
# signal to the audit + install pipeline (via R-infix) that this VM uses
# the smaller-VRAM gemma4 edge variant.
FEEDBACK_TEMPLATE_RTX = [
    {"behavior": "M2",        "flavor": "v1.14vcpu.28g",                 "count": 1},
    {"behavior": "B2R.gemma", "flavor": "rtx2080ti-1gpu.14vcpu.28g",     "count": 1},
    {"behavior": "S2R.gemma", "flavor": "rtx2080ti-1gpu.14vcpu.28g",     "count": 1},
    {"behavior": "B2C.gemma", "flavor": "v1.14vcpu.28g",                 "count": 1},
    {"behavior": "S2C.gemma", "flavor": "v1.14vcpu.28g",                 "count": 1},
]

# RTX A-pool variant — same B2R/S2R behavior keys (identical gemma4:e4b
# runtime + behavior.json plumbing) but targets the separate
# rtx2080ti-A-* OpenStack flavor (PCI alias 2080ti-rtx-a:1). Lets us
# spread RTX deploys across both physical card pools when one is full.
FEEDBACK_TEMPLATE_RTX_A = [
    {"behavior": "M2",        "flavor": "v1.14vcpu.28g",                 "count": 1},
    {"behavior": "B2R.gemma", "flavor": "rtx2080ti-A-1gpu.14vcpu.28g",   "count": 1},
    {"behavior": "S2R.gemma", "flavor": "rtx2080ti-A-1gpu.14vcpu.28g",   "count": 1},
    {"behavior": "B2C.gemma", "flavor": "v1.14vcpu.28g",                 "count": 1},
    {"behavior": "S2C.gemma", "flavor": "v1.14vcpu.28g",                 "count": 1},
]

# CPU-only variant — NO GPU cards. Drops the two GPU brains (B2/S2 or
# B2R/S2R) entirely, leaving M2 (MCHP, no LLM) + the two CPU brains
# (B2C/S2C on gemma4:e2b). For datasets we want "at least running" when
# the GPU pools are full and no card is available — e.g. vt1g/vt10g, which
# were swapped out of exp1's rtx slots for cptc8/cptc9 (2026-06-16). The
# CPU brains already appear in every GPU tier, so the behavior.json source
# (B.gemma/B0C.gemma, S.gemma/S0C.gemma, M1) is the same one PHASE ships —
# no new feedback content needed.
FEEDBACK_TEMPLATE_CPU = [
    {"behavior": "M2",        "flavor": "v1.14vcpu.28g", "count": 1},
    {"behavior": "B2C.gemma", "flavor": "v1.14vcpu.28g", "count": 1},
    {"behavior": "S2C.gemma", "flavor": "v1.14vcpu.28g", "count": 1},
]

# Map gpu_tier name → template + flavor_capacity dict. Used by
# generate_feedback_config to pick the right shape based on --gpu CLI flag.
FEEDBACK_TEMPLATES_BY_TIER = {
    "v100": {
        "template": FEEDBACK_TEMPLATE,
        "flavor_capacity": {
            "v1.14vcpu.28g": 3,
            "v100-1gpu.14vcpu.28g": 2,
        },
    },
    "cpu": {
        "template": FEEDBACK_TEMPLATE_CPU,
        "flavor_capacity": {
            "v1.14vcpu.28g": 3,
        },
    },
    "rtx": {
        "template": FEEDBACK_TEMPLATE_RTX,
        "flavor_capacity": {
            "v1.14vcpu.28g": 3,
            "rtx2080ti-1gpu.14vcpu.28g": 2,
        },
    },
    "rtx-a": {
        "template": FEEDBACK_TEMPLATE_RTX_A,
        "flavor_capacity": {
            "v1.14vcpu.28g": 3,
            "rtx2080ti-A-1gpu.14vcpu.28g": 2,
        },
    },
}

# Maps PHASE dataset names → short abbreviations for deployment directory names.
# Used by generate_feedback_config() to abbreviate "axes-summer24" → "sum24" etc.
# Lookup: try exact match on full dataset name first, then substring (longest key first).
# Dataset names are extracted from the PHASE source directory name via
# _parse_source_name() (middle component of {experiment}_{dataset}_{preset}).
DATASET_ABBREVIATIONS = {
    # PHASE canonical names (exact match from the source dir's dataset component)
    "axes-summer24": "sum24",
    "axes-fall24": "fall24",
    "axes-spring25": "spr25",
    "axes-summer25": "sum25",
    "axes-fall25": "fall25",
    "axes-2025": "2025",
    "axes-all": "axall",
    "axes-year": "axyear",
    "cptc8-23": "cptc8",
    "cptc9-24": "cptc9",
    "vt-fall22-1gb": "vt1g",
    "vt-fall22-10gb": "vt10g",
    "vt-fall22-50gb": "vt50g",
    # Short aliases (for CLI --target convenience and substring fallback)
    "summer24": "sum24",
    "sum24": "sum24",
    "fall24": "fall24",
    "spring25": "spr25",
    "spr25": "spr25",
    "summer25": "sum25",
    "sum25": "sum25",
    "fall25": "fall25",
    "cptc8": "cptc8",
    "cptc9": "cptc9",
    "vt-1gb": "vt1g",
    "vt1g": "vt1g",
    "vt-10gb": "vt10g",
    "vt10g": "vt10g",
    "vt-50gb": "vt50g",
    "vt50g": "vt50g",
}


def _abbreviate_dataset(dataset: str) -> str:
    """Abbreviate a PHASE dataset name for use in deployment directory names.

    Tries exact match first, then longest substring match to avoid
    false positives (e.g., "all" matching "vt-fall22-1gb").
    """
    # Exact match
    if dataset in DATASET_ABBREVIATIONS:
        return DATASET_ABBREVIATIONS[dataset]

    # Longest substring match (avoids "all" matching "fall22")
    best_key = ""
    best_abbrev = ""
    for key, abbrev in DATASET_ABBREVIATIONS.items():
        if key in dataset and len(key) > len(best_key):
            best_key = key
            best_abbrev = abbrev

    return best_abbrev if best_abbrev else dataset[:6]


def resolve_feedback_args(
    configs_spec: str | None = None,
    source: str | None = None,
    target: str | None = None,
    deploy_type: str | None = None,
    preset: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve feedback CLI args into (behavior_source, configs_spec).

    configs_spec: "all", comma-separated filenames, or None (baseline).
    source: explicit PHASE directory path, or None (auto-detect).
    target: dataset target name (e.g., "summer24") to match against feedback dirs.
    deploy_type: "ghosts" or "decoy" — prefers matching feedback source.
    preset: {preset}_v{version} namespace dir scoping discovery (None when an
        explicit `source` path is given — it already encodes the namespace).

    Returns (None, None) if no feedback configs were requested.
    """
    if not configs_spec:
        return None, None

    # Determine source
    behavior_source = source
    if not behavior_source:
        if target:
            detected = find_feedback_by_target(target, deploy_type=deploy_type,
                                               preset=preset)
        else:
            detected = auto_detect_feedback_source(deploy_type=deploy_type,
                                                   preset=preset)
        if not detected:
            msg = f"No PHASE feedback configs found for target '{target}'" if target else "No PHASE feedback configs found"
            output.info(f"ERROR: {msg}. Use --source <path>")
            raise SystemExit(1)
        behavior_source = str(detected)

    source_path = Path(behavior_source)
    if not source_path.is_dir():
        output.info(f"ERROR: Feedback source not found: {behavior_source}")
        raise SystemExit(1)

    output.info(f"  Feedback source: {behavior_source}")
    output.info(f"  Configs: {configs_spec}")
    return behavior_source, configs_spec


def _type_root(deploy_type: str | None, preset: str | None = None) -> Path | None:
    """Return the per-type subtree under FEEDBACK_BASE, e.g. decoy-controls.

    When `preset` (a `{preset}_v{version}` namespace dir, e.g. std-ctrls_v7.1.2)
    is given, descends one level into it: `{type}-controls/{preset}/`. PHASE
    inserts this namespace between `{type}-controls` and `{dataset}` (2026-06);
    datasets live UNDER it. Scoping root here makes the namespace transparent to
    every discovery function — their dataset iteration (`root.iterdir()`) is
    unchanged — and to the resolved source Path that spinup/distribute walk. The
    `controls/` baseline slot is un-namespaced and is reached via config.yaml's
    behavior_source, not through this function.

    Returns None if FEEDBACK_BASE doesn't exist or the subtree is missing.
    deploy_type=None / "decoy" all resolve to decoy-controls.
    """
    if not FEEDBACK_BASE.is_dir():
        return None
    kind = _deploy_type_prefix(deploy_type)  # "decoy" | "rampart" | "ghosts"
    root = FEEDBACK_BASE / f"{kind}-controls"
    if preset:
        root = root / preset
    return root if root.is_dir() else None


def auto_detect_feedback_source(deploy_type: str | None = None,
                                preset: str | None = None) -> Path | None:
    """Find the most recent dataset subdir under {type}-controls/[{preset}/].

    New layout (/mnt/AXES2U1/feedback/{type}-controls/{preset}/{dataset}/) is
    scoped per-type and per-preset namespace, so there's no cross-type fallback
    like the old flat layout.
    """
    root = _type_root(deploy_type, preset)
    if root is None:
        return None

    best = None
    best_mtime = 0.0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        if d.name in BASELINE_DATASET_SLOTS:
            continue
        if not _is_valid_feedback_source(d, deploy_type):
            continue
        mtime = d.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best = d

    return best


# ── PHASE source validation & metadata extraction (post Stage 2) ───────────
#
# Stage 2 dropped manifest.json. Feedback source dirs are now identified by
# their generated file layout (type-specific globs), and metadata is parsed
# from the directory name itself: {experiment}_{dataset}_{preset}.


def _parse_source_name(source_dir: Path) -> tuple[str, str, str]:
    """Extract (experiment, dataset, preset) from a feedback source dir.

    Post 2026-04-23 layout: source_dir.name IS the dataset (e.g.
    "axes-summer24"), and preset + deploy_key live in manifest.json. When
    available, the manifest is authoritative — dataset/preset come from it
    and experiment is synthesized as "axes-{deploy_key}".

    Falls back to directory-name-only parsing when manifest is missing,
    using `std-ctrls` as the default preset (matches the only preset PHASE
    currently emits). This keeps hand-built dev sources working without a
    manifest.
    """
    manifest = load_manifest(source_dir)
    if manifest:
        deploy_key = manifest.get("deploy_key", source_dir.parent.name)
        dataset = manifest.get("training_dataset", source_dir.name)
        preset = manifest.get("version_preset", "std-ctrls")
        return (f"axes-{deploy_key}", dataset, preset)

    # Fallback (no manifest, e.g. hand-built dev source): dir name is the
    # dataset; its parent is now the {preset}_v{version} namespace dir, so derive
    # the preset from that name rather than treating the parent as the deploy_key.
    ns = source_dir.parent.name
    preset = ns.split("_v")[0] if "_v" in ns else "std-ctrls"
    return (f"axes-{source_dir.name}", source_dir.name, preset)


def _ns_preset_token(source_dir: Path, preset_name: str) -> str:
    """Deployment-name preset token, sanitized to [a-z0-9].

    Post-2026-06 feedback sources live under a `{preset}_v{version}` namespace
    dir (`source_dir.parent.name`). Use the FULL namespace (version INCLUDED) so
    two lineages OR two versions of the same dataset don't collide on
    deployment_name / run_dir / VM prefix / experiments.json key — e.g.
    `std-ctrls_v7.1.2` → `stdctrlsv712`, `std-ctrls_v7.1.5` → `stdctrlsv715`
    (distinct), whereas the old `manifest.version_preset` token dropped the
    version and collided. Falls back to the parsed `preset_name` for legacy flat
    sources (no `_v` in the parent)."""
    ns = source_dir.parent.name
    token = ns if "_v" in ns else preset_name
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _is_valid_feedback_source(source_dir: Path, deploy_type: str | None) -> bool:
    """Check whether source_dir contains the expected file layout.

    Validity is inferred from the presence of the generator's output files:
      DECOY   — {behavior}/{sup}/behavior.json        (consolidated per-SUP file)
                {behavior}/{sup}/timing_profile.json  (legacy 8-file fallback)
      RAMPART — {bare_node}/user-roles.json           (per-node pyhuman configs)
      GHOSTS  — npc-*/timeline.json                   (per-NPC timelines)

    DECOY consolidated its 8 per-SUP JSONs into a single behavior.json as of
    2026-04-16. The legacy timing_profile.json glob is kept so feedback
    sources generated before the cutover still validate and deploy.

    If deploy_type is None/unknown, accept any of the recognised patterns.
    """
    if not source_dir.is_dir():
        return False

    if deploy_type == "rampart":
        return any(source_dir.glob("*/user-roles.json"))
    if deploy_type == "ghosts":
        return any(source_dir.glob("npc-*/timeline.json"))
    if deploy_type in ("decoy", None):
        return (
            any(source_dir.glob("*/*/behavior.json"))
            or any(source_dir.glob("*/*/timing_profile.json"))
        )

    # Unknown type — accept any marker
    return (
        any(source_dir.glob("*/user-roles.json"))
        or any(source_dir.glob("npc-*/timeline.json"))
        or any(source_dir.glob("*/*/behavior.json"))
        or any(source_dir.glob("*/*/timing_profile.json"))
    )


def find_all_feedback_sources(deploy_type: str | None = None,
                              preset: str | None = None) -> list[dict]:
    """Find all PHASE feedback dataset dirs under {type}-controls/[{preset}/].

    Returns list of dicts sorted by dataset name:
        [{"path": Path, "name": str, "preset": str, "dataset": str}, ...]

    A directory is included if its file layout matches the generator
    output for that type. Metadata (preset, dataset) comes from
    manifest.json when present; otherwise directory name is treated as
    the dataset with preset defaulting to "std-ctrls".
    """
    root = _type_root(deploy_type, preset)
    if root is None:
        return []

    results = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        if d.name in BASELINE_DATASET_SLOTS:
            continue
        if not _is_valid_feedback_source(d, deploy_type):
            continue

        _experiment, dataset, preset = _parse_source_name(d)
        results.append({
            "path": d,
            "name": d.name,
            "preset": preset,
            "dataset": dataset,
        })

    return results


# Known dataset targets — maps short/friendly names to search strings
# used against feedback dataset directory names under
# /mnt/AXES2U1/feedback/{type}-controls/.
# The search string must appear somewhere in the directory name.
DATASET_TARGETS = {
    # AXES seasonal
    "summer24": "summer24",
    "sum24": "summer24",
    "axes-summer24": "axes-summer24",
    "fall24": "fall24",
    "axes-fall24": "axes-fall24",
    "spring25": "spring25",
    "spr25": "spring25",
    "axes-spring25": "axes-spring25",
    "summer25": "summer25",
    "sum25": "summer25",
    "axes-summer25": "axes-summer25",
    "fall25": "fall25",
    "axes-fall25": "axes-fall25",
    # AXES aggregate
    "2025": "axes-2025",
    "axes-2025": "axes-2025",
    "all": "axes-all",
    "axall": "axes-all",
    "axes-all": "axes-all",
    "year": "axes-year",
    "axyear": "axes-year",
    "axes-year": "axes-year",
    # CPTC
    "cptc8-23": "cptc8-23",
    "cptc8": "cptc8-23",
    "cptc9-24": "cptc9-24",
    "cptc9": "cptc9-24",
    # VirusTotal
    "vt-fall22-1gb": "vt-fall22-1gb",
    "vt-1gb": "vt-fall22-1gb",
    "vt1g": "vt-fall22-1gb",
    "vt-fall22-10gb": "vt-fall22-10gb",
    "vt-10gb": "vt-fall22-10gb",
    "vt10g": "vt-fall22-10gb",
    "vt-fall22-50gb": "vt-fall22-50gb",
    "vt-50gb": "vt-fall22-50gb",
    "vt50g": "vt-fall22-50gb",
}


# Static tier plans: a named, operator-curated assignment of feedback
# datasets to GPU tiers, sized to the physical card pools (which are NOT
# queryable from OpenStack — totals are operator knowledge). Selected via
# `./deploy --decoy --exp1`. Each entry is an ordered list of
# (gpu_tier, [dataset targets]) — targets use the same aliases as --target
# (resolved through DATASET_TARGETS).
#
# exp1 (2026-06-10; rev 2026-06-16): V100 pool holds 19 cards, controls eat 2
# → 8 feedback deploys (16 cards); non-A rtx pool fits 2 deploys; rtx-a fits 1
# next to controls' B0R/S0R. 2026-06-16: the two non-A rtx slots were swapped
# from vt1g/vt10g to cptc8/cptc9 (operator decision; same 4-card footprint).
# cptc now carries physical connection_shape byte/duration targets — the
# Phase-1 shape lever — so it is a real shape target now, NOT the structurally
# -hopeless case the old service_mix_targets era assumed. Its volume target
# stays high (185-208/min) so the audit BG column (a loose D4-floor check) may
# still read red, but volume is the DEAD lever — the model scores on
# per-connection shape, which is reachable. vt1g/vt10g now deploy via --target.
TIER_PLANS = {
    "exp1": [
        ("v100", ["2025", "axall", "axyear", "fall24", "fall25",
                  "spr25", "sum24", "sum25"]),
        ("rtx", ["cptc8", "cptc9"]),
        ("rtx-a", ["vt50g"]),
        # CPU-only (no card) — vt1g/vt10g lost their rtx slots to cptc8/cptc9
        # (2026-06-16); run them card-free so they're at least emitting.
        ("cpu", ["vt1g", "vt10g"]),
    ],
}


def find_feedback_by_target(target: str, deploy_type: str | None = None,
                            preset: str | None = None) -> Path | None:
    """Find a feedback dataset dir matching the given target name.

    e.g. target="summer24" or "sum24" resolves to dir "axes-summer24"
    under {FEEDBACK_BASE}/{type}-controls/[{preset}/]. DATASET_TARGETS normalizes
    short aliases. Most-recent mtime wins if multiple dirs match.
    """
    root = _type_root(deploy_type, preset)
    if root is None:
        return None

    # Normalize target name to its canonical dataset substring
    search = DATASET_TARGETS.get(target, target)

    best = None
    best_mtime = 0.0
    for d in root.iterdir():
        if not d.is_dir() or search not in d.name:
            continue
        if d.name in BASELINE_DATASET_SLOTS:
            continue
        if not _is_valid_feedback_source(d, deploy_type):
            continue
        mtime = d.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best = d

    return best


def _deploy_type_prefix(deploy_type: str | None) -> str | None:
    """Map deploy_type to the prefix used in PHASE feedback directory names.

    Defaults to "decoy" when no type is specified.
    """
    if deploy_type == "ghosts":
        return "ghosts"
    if deploy_type == "rampart":
        return "rampart"
    return "decoy"


def generate_feedback_config(
    source_dir: Path,
    configs_spec: str,
    deploy_dir: Path,
    gpu_tier: str = "v100",
) -> str:
    """Generate one canonical four-VM DECOY feedback configuration."""
    import yaml

    if gpu_tier != "v100":
        raise FeedbackSourceError(
            f"canonical Decoy feedback requires V100 GPU SUPs; got {gpu_tier!r}"
        )
    preset, target = decoy_feedback_source_identity(source_dir)
    dep_name = f"decoy-feedback-{_decoy_preset_slug(preset)}-{target}"
    dep_dir = deploy_dir / dep_name
    dep_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "deployment_name": dep_name,
        "purpose": "feedback",
        "target": target,
        "capture_interface": "eno2",
        "behavior_source": str(source_dir),
        "flavor_capacity": {
            "v1.14vcpu.28g": 2,
            "v100-1gpu.14vcpu.28g": 2,
        },
        "deployments": [dict(item) for item in DECOY_FEEDBACK_DEPLOYMENTS],
    }

    config_path = dep_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    return dep_name


def generate_rampart_feedback_config(
    source_dir: Path,
    configs_spec: str,
    deploy_dir: Path,
    base_config_name: str = "rampart-controls",
) -> str:
    """Generate a RAMPART feedback deployment config.yaml. Returns deployment name."""
    import yaml

    if not _is_valid_feedback_source(source_dir, "rampart"):
        output.error(
            f"ERROR: {source_dir} is not a valid RAMPART feedback source "
            f"(no {{bare_node}}/user-roles.json files found)"
        )
        raise SystemExit(1)

    target = require_manifest_training_dataset(source_dir, "rampart")
    _experiment, dataset, preset_name = _parse_source_name(source_dir)

    # Abbreviate dataset (exact match first, then longest substring match)
    dataset_abbrev = _abbreviate_dataset(dataset)

    # Scope label
    if configs_spec == "all":
        scope_label = "all"
    else:
        first = configs_spec.split(",")[0]
        scope_label = first.replace(".json", "").split("_")[0]

    preset_clean = _ns_preset_token(source_dir, preset_name)
    dep_name = f"rampart-feedback-{preset_clean}-{dataset_abbrev}-{scope_label}"
    dep_dir = deploy_dir / dep_name
    dep_dir.mkdir(parents=True, exist_ok=True)

    # Copy base config and update deployment_name
    base_config_path = deploy_dir / base_config_name / "config.yaml"
    if base_config_path.exists():
        base = yaml.safe_load(base_config_path.read_text())
    else:
        base = {"type": "rampart"}

    base["deployment_name"] = dep_name
    base["purpose"] = "feedback"
    base["target"] = target
    base["capture_interface"] = base.get("capture_interface", "eno2")
    base["behavior_source"] = str(source_dir)
    if configs_spec == "all":
        base["behavior_configs"] = "all"
    else:
        base["behavior_configs"] = [f.strip() for f in configs_spec.split(",")]

    # Feedback-only flavor bump (2026-04-28): override enterprise.cloud_config
    # to point at axes-cicd-feedback.json which maps the "small" alias to
    # m1.medium (4GB RAM / 2 vCPU / 40GB disk). Controls keep m1.small via
    # the original axes-cicd.json — pristine baseline. Reason: m1.small (1cpu/
    # 2GB) caused rolling sshd kills under emulation load on Windows endpoints
    # (audit on 2026-04-28 showed ~5 of 180 winep rotating-down at any time).
    # NOTE: originally bumped to m1.xlarge but the cluster ran out of 8-core
    # host slots for it, so axes-cicd-feedback.json settled on m1.medium.
    if "enterprise" in base and isinstance(base["enterprise"], dict):
        base["enterprise"]["cloud_config"] = "cloud-configs/axes-cicd-feedback.json"

    config_path = dep_dir / "config.yaml"
    with open(config_path, "w") as f:
        f.write(f"---\n")
        f.write(f"# Auto-generated RAMPART feedback deployment: {dep_name}\n")
        f.write(f"# Feedback source: {source_dir}\n")
        f.write(f"# Preset: {preset_name} | Dataset: {dataset} | Scope: {scope_label}\n\n")
        yaml.dump(base, f, default_flow_style=False, sort_keys=False)

    return dep_name


def generate_ghosts_feedback_config(
    source_dir: Path,
    configs_spec: str,
    deploy_dir: Path,
) -> str:
    """Generate a GHOSTS feedback deployment config.yaml. Returns deployment name."""
    import yaml

    if not _is_valid_feedback_source(source_dir, "ghosts"):
        output.error(
            f"ERROR: {source_dir} is not a valid GHOSTS feedback source "
            f"(no npc-*/timeline.json files found)"
        )
        raise SystemExit(1)

    target = require_manifest_training_dataset(source_dir, "ghosts")
    _experiment, dataset, preset_name = _parse_source_name(source_dir)

    # Abbreviate dataset (exact match first, then longest substring match)
    dataset_abbrev = _abbreviate_dataset(dataset)

    # Scope label
    if configs_spec == "all":
        scope_label = "all"
    else:
        first = configs_spec.split(",")[0]
        scope_label = first.replace(".json", "").split("_")[0]

    preset_clean = _ns_preset_token(source_dir, preset_name)
    dep_name = f"ghosts-feedback-{preset_clean}-{dataset_abbrev}-{scope_label}"
    dep_dir = deploy_dir / dep_name
    dep_dir.mkdir(parents=True, exist_ok=True)

    # Build behavior_configs field
    if configs_spec == "all":
        behavior_configs = "all"
    else:
        behavior_configs = [f.strip() for f in configs_spec.split(",")]

    config = {
        "deployment_name": dep_name,
        "type": "ghosts",
        "purpose": "feedback",
        "target": target,
        "capture_interface": "eno2",
        "behavior_source": str(source_dir),
        "behavior_configs": behavior_configs,
        "ghosts": {
            # API runs the full Docker stack (postgres + 5 containers) → keep it
            # on the big flavor. NPC clients are .NET traffic generators on
            # m1.medium (2 vCPU / 4 GB, 2026-06-10): a feedback dataset = 14 + 5×2
            # = 24 cores (was 84 on v1.14vcpu.28g, 54 on the interim m1.xlarge).
            # Justified by the v9.0.0 canary: the .NET client no longer leaks
            # (flat ~160 MB over 12h), so the big-RAM headroom is unnecessary;
            # Firefox is the footprint (~3 GB under continuous always-on load,
            # less on the real 1h/day schedule). 4 GB is tight, so the memcap in
            # install-ghosts-clients.yaml is sized to 3G to protect sshd. Controls
            # stay on v1.14vcpu.28g (pristine, no memcap), same controls/feedback
            # flavor split RAMPART uses.
            "api_flavor": "v1.14vcpu.28g",
            "client_flavor": "m1.medium",
            "client_count": 5,
            "ghosts_repo": "https://github.com/cmu-sei/GHOSTS.git",
            # Pinned to v9.0.0 to match controls (2026-06-09) — controls and
            # feedback build the SAME GHOSTS version so the only intended
            # difference is the PHASE timeline. The Firefox install + memcap +
            # flavor remain feedback-only (controls pristine upstream).
            "ghosts_branch": "v9.0.0",
        },
    }

    config_path = dep_dir / "config.yaml"
    with open(config_path, "w") as f:
        f.write(f"---\n")
        f.write(f"# Auto-generated GHOSTS feedback deployment: {dep_name}\n")
        f.write(f"# Feedback source: {source_dir}\n")
        f.write(f"# Preset: {preset_name} | Dataset: {dataset} | Scope: {scope_label}\n\n")
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    return dep_name
