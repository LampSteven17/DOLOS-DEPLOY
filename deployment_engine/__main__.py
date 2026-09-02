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
  --controls            deploy the control fleet only
  --feedback            deploy feedback variants (all, or selected targets/source)
  --canary              deploy the isolated RUSE runtime qualification fleet

Feedback without --target/--source = batch every discovered dataset.
For canonical Decoy feedback, --target accepts one or more exact targets.
Pass --source to deploy one exact generation directory.

examples:
  ./deploy --decoy --preset colfix_v12.5.0 controls + ALL feedback targets
  ./deploy --decoy --controls               control fleet only
  ./deploy --decoy --canary                 isolated RUSE-only runtime canary
  ./deploy --decoy --feedback --preset colfix_v12.5.0
                                            all canonical feedback targets
  ./deploy --decoy --feedback --preset colfix_v12.5.0 \
      --target axes-summer24                 one exact target
  ./deploy --decoy --feedback --preset colfix_v12.5.0 \
      --target axes-fall24 axes-spring25     selected targets in this order
  ./deploy --decoys --feedback --preset colfix_v12.5.0 --gpu rtx \
      --target vt-spring22 vt-summer21       selected targets on explicit RTX
  ./deploy --decoy --controls --feedback --preset colfix_v12.5.0
                                            controls + ALL feedback (explicit)
  ./deploy --decoy --controls --preset colfix_v12.5.0 \
      --target axes-summer24                controls + one feedback target
  ./deploy --ghosts                         controls + ALL GHOSTS feedback
  ./deploy --rampart --controls             RAMPART control fleet only""",
    )
    p.add_argument("--decoy", "--decoys", action="store_true", dest="decoy",
                   help="Deploy DECOY SUP agents (default; --decoys alias)")
    p.add_argument("--rampart", "--ramparts", action="store_true", dest="rampart",
                   help="Deploy RAMPART enterprise network (--ramparts alias)")
    p.add_argument("--ghosts", "--ghost", action="store_true", dest="ghosts",
                   help="Deploy GHOSTS NPC traffic generators (--ghost alias)")
    p.add_argument("config_name", nargs="?", help="Deployment config name (default: {type}-controls)")

    # Scope flags — opt into just controls, just feedback, or (default) both.
    p.add_argument("--controls", action="store_true", help="Deploy the control fleet (no feedback)")
    p.add_argument("--feedback", action="store_true", help="Deploy PHASE feedback variants")
    p.add_argument(
        "--canary", action="store_true",
        help="Deploy the isolated RUSE-only Decoy runtime canary",
    )

    p.add_argument("--preset", type=str,
                   help="PHASE feedback preset (for canonical Decoy feedback: "
                        "/data/axes-mirror/feedback/{preset}/{target}/). Required "
                        "for discovery; not needed with an exact --source.")
    p.add_argument("--source", type=str, help="Explicit PHASE feedback source directory (single)")
    p.add_argument(
        "--target",
        type=str,
        nargs="+",
        help="One or more space-separated exact Decoy feedback target names.",
    )
    p.add_argument("--gpu", type=str, choices=["v100", "rtx"], default=None,
                   help="Canonical Decoy feedback GPU hardware (default: v100).")
    return p


def _teardown_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="teardown",
        description="Teardown deployments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  ./teardown decoy-controls-2026-08-20_130523Z
                                             teardown one exact dated run
  ./teardown --decoy --controls            teardown all active DECOY control deployments
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
    purpose = p.add_mutually_exclusive_group()
    purpose.add_argument(
        "--controls",
        action="store_true",
        help="Filter: only deployments with purpose: control",
    )
    purpose.add_argument(
        "--feedback",
        action="store_true",
        help="Filter: only deployments with purpose: feedback",
    )
    p.add_argument("--failed", action="store_true",
                   help="Filter: only runs stamped failed (deploy_status.json) "
                        "with an exact-prefix OpenStack VM. Composes with "
                        "system/purpose filters; alone, spans all types")
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
    """List available feedback preset directories for one deploy type."""
    from .core.feedback import (
        BASELINE_DATASET_SLOTS,
        DECOY_FEEDBACK_BASE,
        FEEDBACK_BASE,
    )
    root = (
        DECOY_FEEDBACK_BASE
        if deploy_type == "decoy"
        else FEEDBACK_BASE / f"{deploy_type}-controls"
    )
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

    if args.canary:
        incompatible = (
            deploy_type != "decoy" or args.controls or args.feedback
            or args.preset or args.source or args.target or args.gpu
            or args.config_name
        )
        if incompatible:
            output.error(
                "ERROR: --canary is an isolated Decoy scope and cannot be "
                "combined with another system, scope, selector, GPU tier, "
                "or config name."
            )
            return 1
        from .core.plan import (
            build_decoy_canary_plan, execute_plan, show_plan_and_confirm,
        )
        try:
            plan = build_decoy_canary_plan(DEPLOY_DIR)
        except (ValueError, OSError, feedback.FeedbackSourceError) as exc:
            output.error(f"ERROR: {exc}")
            return 1
        if not show_plan_and_confirm(plan, "decoy", gpu_tier="v100"):
            return 0
        return execute_plan(plan, "decoy", None, DEPLOY_DIR, gpu_tier="v100")

    # Canonical Decoy feedback accepts an ordered list of exact targets. Keep
    # the established Rampart/GHOSTS planner contract scalar, and reject
    # ambiguous comma-separated or repeated Decoy values before plan building.
    target: str | list[str] | None = args.target
    if args.target:
        if deploy_type == "decoy":
            for value in args.target:
                if "," in value:
                    output.error(
                        f"ERROR: comma-separated --target value {value!r} is not "
                        "supported; pass targets as separate arguments."
                    )
                    return 1
            seen: set[str] = set()
            for value in args.target:
                if value in seen:
                    output.error(f"ERROR: duplicate --target value: {value}")
                    return 1
                seen.add(value)
        else:
            if len(args.target) != 1:
                output.error(
                    f"ERROR: {deploy_type} --target accepts exactly one value."
                )
                return 1
            target = args.target[0]

    # PHASE consolidated to a single behavior.json per SUP — there are no
    # per-config-file knobs to filter on the deploy side anymore. configs_spec
    # is plumbed through to the distribute playbook for legacy reasons; "all"
    # means "copy *.json" (i.e. behavior.json), None means controls-only path.
    configs_spec = "all" if args.feedback else None

    # --- Resolve intent: controls? feedback? ---
    # --target / --source imply feedback (harmless shorthand).
    explicit_feedback = (bool(configs_spec) or bool(args.source)
                         or bool(target))
    explicit_controls = args.controls
    single_selector = target or args.source

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

    # Require an explicit discovery namespace. Decoy uses the canonical PHASE
    # tree; Rampart/GHOSTS retain their established per-type trees.
    if want_feedback and not args.source:
        if not args.preset:
            detail = (
                "canonical Decoy feedback lives under "
                "/data/axes-mirror/feedback/{preset}/{target}/"
                "{YYYY-MM-DD_HHMMZ}/"
                if deploy_type == "decoy"
                else "PHASE feedback requires an explicit preset namespace"
            )
            output.error(
                f"ERROR: --preset is required when deploying feedback ({detail})."
            )
            _print_available_presets(deploy_type)
            return 1
        from .core.feedback import DECOY_FEEDBACK_BASE, FEEDBACK_BASE
        if deploy_type == "decoy":
            preset_root = DECOY_FEEDBACK_BASE / args.preset
        else:
            preset_root = FEEDBACK_BASE / f"{deploy_type}-controls" / args.preset
        if not preset_root.is_dir():
            output.error(f"ERROR: --preset {args.preset!r} not found "
                         f"({preset_root}).")
            _print_available_presets(deploy_type)
            return 1

    gpu_tier = getattr(args, "gpu", None) or "v100"

    # --- Build plan: list of (label, behavior_source, configs_spec) tasks ---
    from .core.plan import build_deploy_plan, show_plan_and_confirm, execute_plan

    plan = build_deploy_plan(
        deploy_type,
        want_controls=want_controls,
        want_feedback=want_feedback,
        configs_spec=configs_spec,
        single_selector=single_selector,
        target=target,
        source=args.source,
        preset=args.preset,
        deploy_dir=DEPLOY_DIR,
        tier_plan=None,
        gpu_tier=gpu_tier,
    )
    if plan is None:
        return 1
    if not plan:
        output.error("Nothing to deploy. Use --controls and/or --feedback.")
        return 1

    if not show_plan_and_confirm(plan, deploy_type, gpu_tier=gpu_tier):
        return 0

    return execute_plan(plan, deploy_type, args.config_name, DEPLOY_DIR,
                        gpu_tier=gpu_tier)



def _cmd_teardown(argv: list[str]) -> int:
    parser = _teardown_parser()
    args = parser.parse_args(argv)

    has_system = args.decoy or args.rampart or args.ghosts
    purpose = "control" if args.controls else ("feedback" if args.feedback else None)
    if purpose is not None and not has_system:
        output.error(
            "ERROR: --controls/--feedback requires a system selector "
            "(--decoy, --rampart, or --ghosts)"
        )
        return 1

    if args.teardown_all:
        from .teardown import run_teardown_all
        return run_teardown_all(DEPLOY_DIR)

    has_filter = has_system or purpose is not None or args.failed
    if has_filter:
        from .teardown import run_teardown_filtered
        # --failed with no explicit type spans all types (otherwise the
        # type filter would match nothing). With a type flag it narrows.
        types = {"decoy": args.decoy, "rampart": args.rampart, "ghosts": args.ghosts}
        if args.failed and not has_system:
            types = {"decoy": True, "rampart": True, "ghosts": True}
        return run_teardown_filtered(
            DEPLOY_DIR,
            types=types,
            purpose=purpose,
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
