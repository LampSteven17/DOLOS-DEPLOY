"""
SUP (Synthetic User Persona) - Unified CLI

Usage:
    python -m sup <CONFIG_KEY>
    python -m sup --brain <BRAIN> [--model <MODEL>] [--calibration <PROFILE>]
    python -m sup --list

Examples:
    python -m sup M1                                    # MCHP baseline (no timing)
    python -m sup M3                                    # MCHP + fall24 timing
    python -m sup B0.llama                              # BrowserUse + llama baseline
    python -m sup B2.gemma                              # BrowserUse + gemma + summer24
    python -m sup S4.llama                              # SmolAgents + llama + spring25
    python -m sup --brain browseruse --model gemma --calibration fall24
    python -m sup --list                                # List all configs
"""
import argparse
import os
import random
import sys


def main():
    parser = argparse.ArgumentParser(
        description="SUP - Synthetic User Persona Agent Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration Keys:
  C0/M0       Controls (bare Ubuntu / upstream MITRE pyhuman)
  M1-M4       MCHP (no LLM): baseline / summer24 / fall24 / spring25
  B0.llama/gemma  BrowserUse baseline (no timing)
  B2-B4.llama/gemma  BrowserUse + calibrated timing
  S0.llama/gemma  SmolAgents baseline (no timing)
  S2-S4.llama/gemma  SmolAgents + calibrated timing

  Deprecated keys (B1.llama, M1a.llama, B2c.deepseek, etc.) are still accepted.
        """
    )

    parser.add_argument("config_key", nargs="?", default=None,
        help="Configuration key (e.g., M1, B3.gemma, S2.llama)")
    parser.add_argument("--brain", choices=["mchp", "browseruse", "smolagents"], default=None)
    parser.add_argument("--content", choices=["none", "llm"], default="none")
    parser.add_argument("--model", choices=["llama", "gemma", "gemmac"], default="llama",
        help="llama=llama3.1:8b (GPU), gemma=gemma4:26b (V100), gemmac=gemma4:e2b (CPU)")
    parser.add_argument("--calibration", choices=["summer24", "fall24", "spring25"], default=None,
        help="Calibration timing profile (semester)")
    parser.add_argument("--phase", action="store_true",
        help="Legacy: equivalent to --calibration=summer24")
    parser.add_argument("--seed", type=int, default=42,
        help="Random seed for deterministic behavior (default: 42, 0 = non-deterministic)")
    parser.add_argument("--task", type=str, default=None)
    parser.add_argument("--behavior-config-dir", type=str, default=None,
        help="Override behavioral config directory (default: auto-discover from deployed_sups)")
    parser.add_argument("--list", action="store_true")

    args = parser.parse_args()

    if args.list:
        from runners import list_configs, list_aliases
        print("Available configuration keys:")
        print("-" * 50)
        from phase_workflow.configurations import CONFIGURATIONS
        for key in CONFIGURATIONS:
            print(f"  {key} (phase-workflow-plan-v1)")
        for key in list_configs():
            print(f"  {key}")
        print("")
        aliases = list_aliases()
        if aliases:
            print(f"Deprecated aliases ({len(aliases)} total):")
            # Show a few examples
            shown = list(aliases.items())[:6]
            for old_key, new_key in shown:
                print(f"  {old_key} -> {new_key}")
            if len(aliases) > 6:
                print(f"  ... and {len(aliases) - 6} more")
        return

    if args.config_key:
        from phase_workflow.configurations import is_workflow_configuration
        if is_workflow_configuration(args.config_key):
            from phase_workflow.runtime import run_workflow_runtime
            run_workflow_runtime(args.config_key, args.behavior_config_dir)
            return

    from runners import get_config, build_config

    config = None

    if args.config_key:
        config = get_config(args.config_key)
        if config is None:
            print(f"Error: Unknown configuration key '{args.config_key}'")
            print("Use --list to see available configurations")
            sys.exit(1)
    elif args.brain:
        calibration = args.calibration
        if args.phase and not calibration:
            calibration = "summer24"
        config = build_config(
            brain=args.brain,
            content=args.content,
            model=args.model,
            calibration=calibration,
        )
    else:
        print("No configuration specified, defaulting to M1 (pure MCHP)")
        config = get_config("M1")

    # CLI --seed takes precedence over config default
    config.seed = args.seed

    # PHASE-emitted _metadata.seed overrides CLI default when present, so each
    # (config, dataset) pair has a stable distinct seed. Resolve behavior dir
    # and peek for the seed BEFORE random.seed() so all downstream state is
    # consistent (AgentLogger session_id, Ollama LLM seed, emulation_loop RNG).
    from common.behavioral_config import resolve_behavioral_config_dir, peek_seed
    behavior_dir = resolve_behavioral_config_dir(
        config.config_key, override_dir=args.behavior_config_dir
    )
    phase_seed = peek_seed(behavior_dir, config.config_key)
    if phase_seed is not None and phase_seed != config.seed:
        print(f"  PHASE _metadata.seed={phase_seed} overrides CLI seed={config.seed}")
        config.seed = phase_seed

    # Seed random early, before any runner/task selection calls
    if config.seed != 0:
        random.seed(config.seed)
        # Also seed Ollama LLM output via environment variable
        os.environ["SUP_OLLAMA_SEED"] = str(config.seed)

    print(f"Configuration: {config.config_key}")
    print(f"  Brain: {config.brain}")
    print(f"  Content: {config.content}")
    print(f"  Model: {config.model}")
    print(f"  Calibration: {config.calibration or 'none'}")
    print(f"  Seed: {config.seed}")
    print("-" * 40)

    if config.brain == "mchp":
        from runners.run_mchp import run_mchp
        run_mchp(config, behavior_config_dir=args.behavior_config_dir)
    elif config.brain == "browseruse":
        from runners.run_browseruse import run_browseruse
        run_browseruse(config, task=args.task, behavior_config_dir=args.behavior_config_dir)
    elif config.brain == "smolagents":
        from runners.run_smolagents import run_smolagents
        run_smolagents(config, task=args.task, behavior_config_dir=args.behavior_config_dir)
    else:
        print(f"Error: Unknown brain type '{config.brain}'")
        sys.exit(1)


if __name__ == "__main__":
    main()
