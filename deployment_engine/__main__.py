"""RUSE Deploy CLI — Python-based deployment orchestrator.

Entry points (via shell scripts at RUSE/ root):
  ./deploy   → python3 -m deployment_engine deploy [--decoy|--rampart|--ghosts] [--feedback] ...
  ./teardown → python3 -m deployment_engine teardown <target> | --all
  ./list     → python3 -m deployment_engine list
  ./audit    → python3 -m deployment_engine audit [--decoy|--rampart|--ghosts]

Layout (post 2026-05-08 restructure):
  deployment_engine/        ← code (this package)
    core/                   ← shared utilities (output, config, openstack,
                              ansible_runner, ssh_config, vm_naming,
                              feedback, teardown_steps, deploy_steps,
                              phase_run_registry, enterprise_ssh_config)
    decoy/  rampart/  ghosts/   ← per-type spinup, teardown, audit
    teardown.py / list.py              ← thin top-level routers
    playbooks/              ← Ansible YAMLs

  deployments/              ← state only (no code)
    {type}-controls/        ← per-deploy config + runs/
    {type}-feedback-*/
    hosts.ini, ansible.cfg, catalog.yaml, logs/
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from .core import output

# ENGINE_DIR = where this code + playbooks live (RUSE/deployment_engine).
# DEPLOY_DIR = where per-deploy state lives (RUSE/deployments/), set
# below from ENGINE_DIR.parent. Two distinct roots so code and state are
# decoupled.
ENGINE_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = ENGINE_DIR.parent / "deployments"
LOGS_DIR = DEPLOY_DIR / "logs"


def _deploy_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy",
        description="Deploy DECOY SUP agents, RAMPART enterprise networks, or GHOSTS NPCs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""default behavior (no scope flags):
  Deploys BOTH controls AND every discovered PHASE feedback dataset for the
  chosen type. Use --controls or --feedback to narrow.

scope flags:
  --controls            deploy baseline controls only
  --feedback            deploy feedback variants (all, or --target/--source one)

Feedback without --target/--source = batch every discovered dataset.
Pass --target or --source to deploy a single dataset.

examples:
  ./deploy --decoy                          controls + ALL feedback datasets
  ./deploy --decoy --controls               baseline controls only
  ./deploy --decoy --feedback               ALL feedback datasets (no controls)
  ./deploy --decoy --feedback --target sum24  single dataset (no controls)
  ./deploy --decoy --feedback --target sum24,axyear,vt50g --gpu rtx
                                            multi-dataset batch on RTX tier
  ./deploy --decoy --controls --feedback    controls + ALL feedback (explicit)
  ./deploy --decoy --controls --target sum24  controls + single feedback
  ./deploy --ghosts                         controls + ALL GHOSTS feedback
  ./deploy --rampart --controls             RAMPART baseline only""",
    )
    p.add_argument("--decoy", "--decoys", action="store_true", dest="decoy",
                   help="Deploy DECOY SUP agents (default; --decoys alias)")
    p.add_argument("--rampart", "--ramparts", action="store_true", dest="rampart",
                   help="Deploy RAMPART enterprise network (--ramparts alias)")
    p.add_argument("--ghosts", "--ghost", action="store_true", dest="ghosts",
                   help="Deploy GHOSTS NPC traffic generators (--ghost alias)")
    p.add_argument("config_name", nargs="?", help="Deployment config name (default: {type}-controls)")

    # Scope flags — opt into just controls, just feedback, or (default) both.
    p.add_argument("--controls", action="store_true", help="Deploy baseline controls (no feedback)")
    p.add_argument("--feedback", action="store_true", help="Deploy PHASE feedback variants")

    p.add_argument("--preset", type=str,
                   help="PHASE feedback namespace {preset}_v{version} "
                        "(e.g. std-ctrls_v7.1.2). REQUIRED whenever feedback is "
                        "in scope (datasets live under "
                        "/mnt/AXES2U1/feedback/{type}-controls/{preset}/). Not "
                        "needed for --controls-only or --source (which gives a "
                        "full path). controls/ baseline is un-namespaced.")
    p.add_argument("--source", type=str, help="Explicit PHASE feedback source directory (single)")
    p.add_argument("--target", type=str,
                   help="Dataset target(s). Single (--target sum24) OR comma-"
                        "separated list (--target sum24,axyear,vt50g) for a "
                        "multi-dataset batch on one GPU tier.")
    p.add_argument("--gpu", type=str, choices=["v100", "rtx", "rtx-a", "cpu"], default=None,
                   help="GPU tier for feedback B2/S2 VMs (default: v100). "
                        "rtx = rtx2080ti-1gpu (PCI alias rtx2080ti:1) w/ "
                        "B2R.gemma+S2R.gemma (gemma4:e4b, fits 11GB VRAM). "
                        "rtx-a = rtx2080ti-A-1gpu (PCI alias 2080ti-rtx-a:1) "
                        "— same SUPs, separate physical card pool; use when "
                        "the rtx pool is exhausted. cpu = card-free 3-VM "
                        "(M2+B2C+S2C, gemma4:e2b) for when no GPU is available.")
    p.add_argument("--exp1", action="store_true",
                   help="Static tier plan exp1 (DECOY feedback only): 8 axes "
                        "datasets on v100, vt1g+vt10g on rtx, vt50g on rtx-a; "
                        "cptc excluded. Sized to physical card pools (19 v100 / "
                        "4 rtx / 4 rtx-a, controls eat 2 v100 + 2 rtx-a). "
                        "Requires --preset; conflicts with --target/--source/--gpu. "
                        "Plan defined in core/feedback.py::TIER_PLANS.")
    return p


def _teardown_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="teardown",
        description="Teardown deployments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  ./teardown decoy-controls-2026-08-20_130523Z
                                             teardown one exact dated run
  ./teardown --decoy --feedback            teardown all active DECOY feedback deployments
  ./teardown --rampart                     teardown all active RAMPART deployments
  ./teardown --ghosts --feedback           teardown all active GHOSTS feedback deployments
  ./teardown --all                         nuclear: delete ALL VMs""",
    )
    p.add_argument(
        "target", nargs="?", help="Teardown target: name-YYYY-MM-DD_HHMMSSZ"
    )
    p.add_argument("--all", action="store_true", dest="teardown_all", help="Delete ALL DECOY, Enterprise, and GHOSTS VMs")

    # Filter flags for batch teardown
    p.add_argument("--decoy", "--decoys", action="store_true", dest="decoy",
                   help="Filter: DECOY SUP deployments (--decoys alias)")
    p.add_argument("--rampart", "--ramparts", action="store_true", dest="rampart",
                   help="Filter: RAMPART enterprise deployments (--ramparts alias)")
    p.add_argument("--ghosts", "--ghost", action="store_true", dest="ghosts",
                   help="Filter: GHOSTS NPC deployments (--ghost alias)")
    p.add_argument("--feedback", action="store_true", help="Filter: only feedback-enabled deployments")
    p.add_argument("--failed", action="store_true",
                   help="Filter: only runs stamped failed (deploy_status.json). "
                        "Composes with type/--feedback filters; alone, spans all types")
    return p


def _list_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="list", description="List active deployments")


def _audit_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="audit",
        description="Health audit of active deployments (DECOY by default)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""subsystem flags (mutually exclusive; default --decoy):
  --decoy     audit DECOY SUP deployments (default)
  --rampart   audit RAMPART enterprise deployments (not yet implemented)
  --ghosts    audit GHOSTS NPC deployments

DECOY checks per VM:
  - SSH reachable
  - SUP systemd service active + NRestarts probe
  - Brain process running
  - Ollama model loaded (matches expected for behavior)
  - GPU model loaded into VRAM (V100 VMs)
  - Recent log activity (latest jsonl fresh)
  - MCHP maintenance cron entries (M VMs)
  - behavior.json present + window-mode contract (FEEDBACK / CONTROLS / FATAL)
  - Volume — median bg-conn/min during ON-windows vs target

cross-deployment:
  - OpenStack vs inventory orphans/missing
  - PHASE phase-run-v1 registration
  - duplicate run_ids
  - orphan boot volumes

Outputs a terminal summary + markdown report at deployments/logs/audit_*.md""",
    )
    p.add_argument("--decoy", "--decoys", action="store_true", dest="decoy",
                   help="Audit DECOY deployments (default; --decoys alias)")
    p.add_argument("--rampart", "--ramparts", action="store_true", dest="rampart",
                   help="Audit RAMPART deployments (not yet implemented; --ramparts alias)")
    p.add_argument("--ghosts", "--ghost", action="store_true", dest="ghosts",
                   help="Audit GHOSTS deployments (--ghost alias)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]

    if not argv:
        print("Usage: deploy|teardown|list|audit [options]", file=sys.stderr)
        return 1

    command = argv[0]
    rest = argv[1:]

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    session_log = output.start_session_log(LOGS_DIR, command)

    try:
        if command == "deploy":
            return _cmd_deploy(rest)
        elif command == "teardown":
            return _cmd_teardown(rest)
        elif command == "list":
            return _cmd_list(rest)
        elif command == "audit":
            return _cmd_audit(rest)
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            print("Usage: deploy|teardown|list|audit [options]", file=sys.stderr)
            return 1

    except KeyboardInterrupt:
        output.info("\nInterrupted.")
        return 130

    finally:
        output.close_session_log()


def _print_available_presets(deploy_type: str) -> None:
    """List the {preset}_v{version} namespace dirs available for a deploy type
    (excludes the un-namespaced controls/ baseline slot)."""
    from .core.feedback import FEEDBACK_BASE, BASELINE_DATASET_SLOTS
    root = FEEDBACK_BASE / f"{deploy_type}-controls"
    if not root.is_dir():
        output.info(f"  (no feedback root at {root})")
        return
    presets = sorted(d.name for d in root.iterdir()
                     if d.is_dir() and d.name not in BASELINE_DATASET_SLOTS)
    if presets:
        output.info("  Available --preset namespaces:")
        for p in presets:
            output.info(f"    {p}")
    else:
        output.info(f"  (no namespaces found under {root})")


def _cmd_deploy(argv: list[str]) -> int:
    parser = _deploy_parser()
    args = parser.parse_args(argv)

    # --- Resolve deploy type ---
    deploy_type = "rampart" if args.rampart else ("ghosts" if args.ghosts else "decoy")

    # PHASE consolidated to a single behavior.json per SUP — there are no
    # per-config-file knobs to filter on the deploy side anymore. configs_spec
    # is plumbed through to the distribute playbook for legacy reasons; "all"
    # means "copy *.json" (i.e. behavior.json), None means controls-only path.
    configs_spec = "all" if args.feedback else None

    # --- Static tier plan (--exp1) ---
    # A named, operator-curated dataset→tier assignment sized to the
    # physical GPU pools. Implies feedback; per-task gpu_tier overrides
    # the invocation-wide --gpu (which is therefore a conflict).
    tier_plan = None
    if args.exp1:
        if deploy_type != "decoy":
            output.error("ERROR: --exp1 is a DECOY tier plan (GPU tiers are decoy-specific).")
            return 1
        if args.target or args.source:
            output.error("ERROR: --exp1 conflicts with --target/--source "
                         "(the plan already names its datasets).")
            return 1
        if args.gpu:
            output.error("ERROR: --exp1 conflicts with --gpu "
                         "(the plan assigns a tier per dataset).")
            return 1
        from .core.feedback import TIER_PLANS
        tier_plan = TIER_PLANS["exp1"]

    # --- Resolve intent: controls? feedback? ---
    # --target / --source / --exp1 imply feedback (harmless shorthand).
    explicit_feedback = (bool(configs_spec) or bool(args.source)
                         or bool(args.target) or bool(tier_plan))
    explicit_controls = args.controls
    single_selector = args.target or args.source

    # Default (neither flag specified): deploy BOTH controls and all feedback.
    # This matches the "full experimental run" mental model and is the new
    # behavior as of 2026-04-23. Use --controls or --feedback to narrow.
    if not explicit_feedback and not explicit_controls:
        want_controls = True
        want_feedback = True
        configs_spec = "all"
    else:
        want_controls = explicit_controls
        want_feedback = explicit_feedback
        if want_feedback and not configs_spec:
            configs_spec = "all"

    # --- Validate --preset namespace (PHASE 2026-06 feedback layout) ---
    # Feedback datasets now live under {type}-controls/{preset}/{dataset}. The
    # namespace must be explicit (no guessing across lineages). Required whenever
    # feedback is in scope and discovery (not an explicit --source path) is used.
    if want_feedback and not args.source:
        if not args.preset:
            output.error("ERROR: --preset is required when deploying feedback "
                         "(PHASE feedback now lives under "
                         f"/mnt/AXES2U1/feedback/{deploy_type}-controls/"
                         "{preset}/{dataset}/).")
            _print_available_presets(deploy_type)
            return 1
        from .core.feedback import FEEDBACK_BASE
        preset_root = FEEDBACK_BASE / f"{deploy_type}-controls" / args.preset
        if not preset_root.is_dir():
            output.error(f"ERROR: --preset {args.preset!r} not found "
                         f"({preset_root}).")
            _print_available_presets(deploy_type)
            return 1

    # --- Build plan: list of (label, behavior_source, configs_spec) tasks ---
    from .core.plan import build_deploy_plan, show_plan_and_confirm, execute_plan

    plan = build_deploy_plan(
        deploy_type,
        want_controls=want_controls,
        want_feedback=want_feedback,
        configs_spec=configs_spec,
        single_selector=single_selector,
        target=args.target,
        source=args.source,
        preset=args.preset,
        deploy_dir=DEPLOY_DIR,
        tier_plan=tier_plan,
    )
    if plan is None:
        return 1
    if not plan:
        output.error("Nothing to deploy. Use --controls and/or --feedback.")
        return 1

    gpu_tier = getattr(args, "gpu", None) or "v100"
    if not show_plan_and_confirm(plan, deploy_type, gpu_tier=gpu_tier):
        return 0

    return execute_plan(plan, deploy_type, args.config_name, DEPLOY_DIR,
                        gpu_tier=gpu_tier)



def _cmd_teardown(argv: list[str]) -> int:
    parser = _teardown_parser()
    args = parser.parse_args(argv)

    if args.teardown_all:
        from .teardown import run_teardown_all
        return run_teardown_all(DEPLOY_DIR)

    has_filter = args.decoy or args.rampart or args.ghosts or args.feedback or args.failed
    if has_filter:
        from .teardown import run_teardown_filtered
        # --failed with no explicit type spans all types (otherwise the
        # type filter would match nothing). With a type flag it narrows.
        types = {"decoy": args.decoy, "rampart": args.rampart, "ghosts": args.ghosts}
        if args.failed and not (args.decoy or args.rampart or args.ghosts):
            types = {"decoy": True, "rampart": True, "ghosts": True}
        return run_teardown_filtered(
            DEPLOY_DIR,
            types=types,
            feedback_only=args.feedback,
            failed_only=args.failed,
        )

    if not args.target:
        output.error(
            "ERROR: specify a target (name-YYYY-MM-DD_HHMMSSZ), "
            "use filter flags, or use --all"
        )
        parser.print_help(sys.stderr)
        return 1

    from .teardown import run_teardown
    return run_teardown(args.target, DEPLOY_DIR)


def _cmd_list(argv: list[str]) -> int:
    _list_parser().parse_args(argv)  # just for --help support
    from .list import run_list
    return run_list(DEPLOY_DIR)


def _cmd_audit(argv: list[str]) -> int:
    args = _audit_parser().parse_args(argv)

    # Mutual exclusion. Default decoy when none specified.
    flags = sum(1 for f in (args.decoy, args.rampart, args.ghosts) if f)
    if flags > 1:
        output.error("Pass at most one of --decoy / --rampart / --ghosts")
        return 1

    if args.rampart:
        from .rampart.audit import run_rampart_audit
        return run_rampart_audit(DEPLOY_DIR)
    if args.ghosts:
        from .ghosts.audit import run_ghosts_audit
        return run_ghosts_audit(DEPLOY_DIR)

    # Default: --decoy
    from .decoy.audit import run_audit
    return run_audit(DEPLOY_DIR)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(main())
