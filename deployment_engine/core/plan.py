"""Deploy plan: build, render, execute.

A "plan" is a list of deploy tasks. For a typical `./deploy --decoy` it's
[controls task, feedback task per discovered dataset]. The plan is built
from CLI flags + filesystem discovery, rendered to the user with a
manifest summary per task, confirmed, then executed sequentially.

Per-type spinup functions are imported lazily inside execute_plan so this
module doesn't pull in all three subsystems just to build a plan.

Each task is a dict:
    {
        "label":           human-readable string,
        "behavior_source": Path | None,
        "configs_spec":    str | None,         # legacy ansible filter
        "manifest":        dict | None,
        "is_controls":     bool,
    }
"""

from __future__ import annotations

from pathlib import Path

from . import output
from .config import DeploymentConfig
from .feedback import (
    DECOY_FEEDBACK_DEPLOYMENTS,
    FeedbackSourceError,
    config_vm_table_lines,
    decoy_feedback_source_identity,
    find_all_decoy_feedback_sources,
    find_decoy_feedback_by_target,
    find_all_feedback_sources,
    find_feedback_by_target,
    load_manifest,
    manifest_summary_lines,
    validate_manifest_target,
)


def build_deploy_plan(
    deploy_type: str,
    *,
    want_controls: bool,
    want_feedback: bool,
    configs_spec: str | None,
    single_selector: str | list[str] | None,
    target: str | list[str] | None,
    source: str | None,
    deploy_dir: Path,
    preset: str | None = None,
    tier_plan: list[tuple[str, list[str]]] | None = None,
) -> list[dict] | None:
    """Resolve controls + feedback intent into an ordered list of deploy tasks.

    `preset` is the {preset}_v{version} namespace scoping feedback discovery
    (None for controls-only or when `source` is an explicit full path).

    `tier_plan` is a static (gpu_tier, [targets]) assignment (e.g.
    feedback.TIER_PLANS["exp1"]) — each resulting feedback task carries its
    own "gpu_tier" key, overriding the invocation-wide --gpu value.

    Returns None on hard failure. Empty list = nothing to do.
    """
    plan: list[dict] = []

    if want_controls:
        plan.append(_build_controls_task(deploy_type, deploy_dir))

    if want_feedback:
        feedback_tasks = _build_feedback_tasks(
            deploy_type, configs_spec, single_selector, target, source,
            preset=preset, tier_plan=tier_plan,
        )
        if feedback_tasks is None:
            return None
        plan.extend(feedback_tasks)

    return plan


def _build_controls_task(deploy_type: str, deploy_dir: Path) -> dict:
    """Build the controls task. Reads behavior_source from the deployment's
    config.yaml. Fails loud — no silent try/except fallback.

    Post 2026-05-08 the controls/ slot is PHASE-emitted with its own
    manifest.json + per-SUP behavior.json. If the config or manifest is
    missing, that's a setup bug and we surface it; we don't paper over it.
    """
    controls_cfg_path = deploy_dir / f"{deploy_type}-controls" / "config.yaml"
    if not controls_cfg_path.exists():
        # No config means controls aren't set up for this type — render
        # the legacy line and let the spinup decide what to do. This is
        # the only "soft" case: a fresh repo without a controls config
        # shouldn't crash the planner.
        return {
            "label": f"{deploy_type}-controls (control)",
            "behavior_source": None,
            "configs_spec": None,
            "manifest": None,
            "is_controls": True,
            "deployments": [],
        }

    cfg = DeploymentConfig.load(controls_cfg_path)  # raises on parse errors — fail loud
    src = Path(cfg.behavior_source) if cfg.behavior_source else None
    return {
        "label": f"{deploy_type}-controls (control)",
        "behavior_source": src,
        "configs_spec": None,
        "manifest": load_manifest(src) if src else None,
        "is_controls": True,
        "deployments": cfg.deployments,
    }


def _build_feedback_tasks(
    deploy_type: str,
    configs_spec: str | None,
    single_selector: str | list[str] | None,
    target: str | list[str] | None,
    source: str | None,
    preset: str | None = None,
    tier_plan: list[tuple[str, list[str]]] | None = None,
) -> list[dict] | None:
    """Resolve feedback sources into tasks. Returns None on resolution failure.

    For canonical Decoy feedback, `target` may be an ordered list of exact
    target names. Legacy Rampart/GHOSTS callers continue to pass one string.

    Legacy `target` may be a single dataset name OR a comma-separated list
    (e.g. "axes-summer24,axes-year,vt-fall22-50gb") — each is resolved
    independently and added to the plan. Useful for batching a subset
    of feedback datasets onto a specific GPU tier in one invocation.

    `preset` is the {preset}_v{version} namespace dir scoping discovery under
    {type}-controls/. Ignored when an explicit `--source` path is given (it
    already encodes the namespace). Required-ness is enforced upstream in the CLI.
    """
    if deploy_type == "decoy":
        return _build_decoy_feedback_tasks(
            configs_spec, target=target, source=source, preset=preset
        )

    sources: list[dict] = []
    if tier_plan:
        # Static tier plan: resolve every (tier, targets) pair up front,
        # fail loud if any dataset is missing from the namespace. Each
        # source carries its tier so the task gets a per-task gpu_tier.
        for tier, targets in tier_plan:
            for tgt in targets:
                src_path = find_feedback_by_target(tgt, deploy_type=deploy_type,
                                                   preset=preset)
                if not src_path:
                    ns = f"{preset}/" if preset else ""
                    output.error(
                        f"ERROR: tier plan target '{tgt}' (tier {tier}) not found "
                        f"under /mnt/AXES2U1/feedback/{deploy_type}-controls/{ns}"
                    )
                    return None
                sources.append({"path": src_path, "dataset": src_path.name,
                                "preset": preset or "?", "gpu_tier": tier})
    elif single_selector:
        if source:
            src_path = Path(source)
            if not src_path.is_dir():
                output.error(f"ERROR: --source not a directory: {src_path}")
                return None
            sources = [{"path": src_path, "dataset": src_path.name, "preset": "?"}]
        elif target:
            # Comma-separated list → multi-target batch. Resolve each
            # independently; fail loud if any one is unknown.
            targets = [t.strip() for t in target.split(",") if t.strip()]
            for tgt in targets:
                src_path = find_feedback_by_target(tgt, deploy_type=deploy_type,
                                                   preset=preset)
                if not src_path:
                    ns = f"{preset}/" if preset else ""
                    output.error(
                        f"ERROR: No feedback source for target '{tgt}' "
                        f"under /mnt/AXES2U1/feedback/{deploy_type}-controls/{ns}"
                    )
                    return None
                sources.append({"path": src_path, "dataset": src_path.name, "preset": preset or "?"})
    else:
        sources = find_all_feedback_sources(deploy_type, preset=preset)
        if not sources:
            ns = f"{preset}/" if preset else ""
            output.error(f"ERROR: No PHASE feedback configs found for '{deploy_type}'")
            output.info(f"  Searched: /mnt/AXES2U1/feedback/{deploy_type}-controls/{ns}")
            return None

    return [
        {
            "label": (f"{deploy_type}-feedback: {src['dataset']}"
                      + (f" [{src['gpu_tier']}]" if src.get("gpu_tier") else "")),
            "behavior_source": src["path"],
            "configs_spec": configs_spec,
            "manifest": load_manifest(src["path"]),
            "is_controls": False,
            # Per-task tier from a static tier plan; None = use the
            # invocation-wide --gpu value.
            "gpu_tier": src.get("gpu_tier"),
        }
        for src in sources
    ]


def _build_decoy_feedback_tasks(
    configs_spec: str | None,
    *,
    target: str | list[str] | None,
    source: str | None,
    preset: str | None,
) -> list[dict] | None:
    """Resolve canonical flat PHASE generations for Decoy only."""
    try:
        if source:
            source_path = Path(source)
            source_preset, source_target = decoy_feedback_source_identity(source_path)
            sources = [{
                "path": source_path,
                "preset": source_preset,
                "target": source_target,
            }]
        elif target:
            if not preset:
                raise FeedbackSourceError(
                    "--preset is required for Decoy feedback discovery"
                )
            targets = [target] if isinstance(target, str) else target
            sources = []
            for selected_target in targets:
                if "," in selected_target:
                    raise FeedbackSourceError(
                        "canonical Decoy --target values must be "
                        "space-separated"
                    )
                sources.append({
                    "path": find_decoy_feedback_by_target(
                        selected_target, preset
                    ),
                    "preset": preset,
                    "target": selected_target,
                })
        else:
            if not preset:
                raise FeedbackSourceError(
                    "--preset is required for Decoy feedback discovery"
                )
            sources = find_all_decoy_feedback_sources(preset)
    except FeedbackSourceError as exc:
        output.error(f"ERROR: {exc}")
        return None

    return [
        {
            "label": f"decoy-feedback: {item['target']}",
            "behavior_source": item["path"],
            "configs_spec": configs_spec,
            "manifest": None,
            "is_controls": False,
            "purpose": "feedback",
            "target": item["target"],
            "preset": item["preset"],
            "deployments": [dict(dep) for dep in DECOY_FEEDBACK_DEPLOYMENTS],
            "gpu_tier": "v100",
        }
        for item in sources
    ]


def show_plan_and_confirm(
    plan: list[dict], deploy_type: str, gpu_tier: str = "v100",
) -> bool:
    """Render the combined plan + ask y/N. Returns True iff the user confirms.

    Fails loud (returns False) if a legacy Rampart/GHOSTS manifest target does
    not match deploy_type. Skips the prompt for one controls-only task.

    gpu_tier only applies to DECOY feedback tasks and is rendered as a
    `tier:` line so the operator can confirm flavor + LLM model before
    burning quota.
    """
    n = len(plan)
    output.banner(f"DEPLOY PLAN ({deploy_type}, {n} task{'s' if n != 1 else ''})")
    output.info("")

    any_mismatch = False
    for i, task in enumerate(plan, 1):
        output.info(f"  {i}. {task['label']}")
        if task["is_controls"]:
            src = task.get("behavior_source")
            mf = task.get("manifest")
            if src is None:
                output.info("      (control — no PHASE feedback)")
            else:
                for line in manifest_summary_lines(src, mf, indent="      "):
                    output.info(line)
            # Controls VMs come from config.yaml's deployments, not a tier
            # template — render them so the operator sees the full baseline
            # topology (C0/M0/M1 + V100/RTX/CPU SUPs), same as feedback tasks.
            deps = task.get("deployments")
            if deps:
                output.info("")
                output.info(f"      VMs to provision ({sum(d.get('count', 1) for d in deps)}):")
                for line in config_vm_table_lines(deps, indent="        "):
                    output.info(line)
        else:
            if deploy_type == "decoy":
                source = task["behavior_source"]
                output.info("      purpose:     feedback")
                output.info(f"      target:      {task['target']}")
                output.info(f"      generation:  {source.name}")
                output.info(f"      source:      {source}")
                output.info("")
                output.info("      VMs to provision (4), fixed V100 topology:")
                for line in config_vm_table_lines(
                    task["deployments"], indent="        "
                ):
                    output.info(line)
            else:
                mf = task["manifest"]
                err = validate_manifest_target(mf, deploy_type)
                for line in manifest_summary_lines(
                    task["behavior_source"], mf, indent="      ",
                ):
                    output.info(line)
                if err:
                    output.error(f"      FAIL: {err}")
                    any_mismatch = True
        output.info("")

    if any_mismatch:
        output.error("Aborting: one or more manifests don't match deploy type.")
        return False

    if n == 1 and plan[0]["is_controls"]:
        return True

    if not output.confirm(f"Deploy these {n} config(s)?"):
        output.info("Cancelled.")
        return False
    return True


def execute_plan(
    plan: list[dict], deploy_type: str, config_name: str | None,
    deploy_dir: Path, gpu_tier: str = "v100",
) -> int:
    """Run each task in the plan sequentially. Returns 0 iff all succeed.

    Lazy-imports the per-type spinup so this module doesn't pull in all
    three subsystems unconditionally.
    """
    if deploy_type == "rampart":
        from ..rampart.spinup import run_rampart_spinup as spinup
        default_config = "rampart-controls"
    elif deploy_type == "ghosts":
        from ..ghosts.spinup import run_ghosts_spinup as spinup
        default_config = "ghosts-controls"
    else:
        from ..decoy.spinup import run_decoy_spinup as spinup
        default_config = "decoy-controls"

    base_config = config_name or default_config
    results: list[tuple[str, int]] = []

    for i, task in enumerate(plan, 1):
        output.info("")
        output.info(f"[{i}/{len(plan)}] Deploying {task['label']}...")
        # Controls runs deploy under their own config name ({type}-controls),
        # not a derived feedback-style dir. The controls config.yaml already
        # declares behavior_source, so spinup picks it up at load time —
        # passing it again here would route through generate_feedback_config
        # and create a parallel `decoy-feedback-stdctrls-contro-all/` dir
        # with the verbose name + duplicated state. is_controls=True signals
        # spinup to skip that branch.
        if task["is_controls"]:
            spinup_source: str | None = None
        else:
            src = task["behavior_source"]
            spinup_source = str(src) if src else None
        try:
            # gpu_tier only applies to decoy deploys + feedback tasks. Controls
            # tasks use their own config.yaml-declared flavors and ignore the kwarg;
            # rampart/ghosts spinups don't accept it. Pass conditionally.
            spinup_kwargs = {}
            if deploy_type == "decoy" and not task["is_controls"]:
                spinup_kwargs["gpu_tier"] = task.get("gpu_tier") or gpu_tier
            rc = spinup(
                base_config, deploy_dir,
                spinup_source,
                task["configs_spec"],
                **spinup_kwargs,
            )
        except SystemExit as e:
            # Per-type spinups call sys.exit() on validation failure. Convert
            # to rc so the batch-summary still renders. Other deploys in the
            # plan continue.
            rc = e.code if isinstance(e.code, int) else 1
            # 130 = SIGINT via __main__.py's signal handler. Don't chug to the
            # next task — propagate so the user's Ctrl+C aborts the batch.
            if rc == 130:
                raise
        results.append((task["label"], rc))
        if rc != 0:
            output.error(f"  FAILED: {task['label']} (rc={rc})")

    if len(results) > 1:
        output.info("")
        output.banner("DEPLOY SUMMARY")
        for label, rc in results:
            status = "OK" if rc == 0 else f"FAILED (rc={rc})"
            output.info(f"  {label}: {status}")
        output.info("")

    failed = sum(1 for _, rc in results if rc != 0)
    if failed:
        output.info(f"DONE: {len(results) - failed}/{len(results)} succeeded, {failed} failed")
        return 1
    output.info(f"DONE: all {len(results)} deployment(s) launched")
    return 0
