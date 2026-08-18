"""
Runner for MCHP brain configurations.

Configurations (exp-3):
- M0: Upstream MITRE pyhuman (control - DO NOT MODIFY)
- M1: Pure MCHP (no LLM, no timing)
- M2: MCHP + summer24 calibrated timing
- M3: MCHP + fall24 calibrated timing
- M4: MCHP + spring25 calibrated timing
"""
import os
from datetime import datetime
from runners.run_config import SUPConfig
from common.logging.agent_logger import AgentLogger


def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def run_mchp(config: SUPConfig, behavior_config_dir: str = None):
    """
    Run MCHP brain with optional calibrated timing.

    Args:
        config: SUP configuration
        behavior_config_dir: Optional override for behavioral config directory.
    """
    # Determine calibration profile (config.calibration takes precedence)
    calibration_profile = config.calibration

    # Resolve behavioral config directory
    from common.behavioral_config import (
        resolve_behavioral_config_dir, load_behavioral_config,
        apply_phase_seed, resolve_runtime_dispatch,
    )
    resolved_behavior_config_dir = resolve_behavioral_config_dir(config.config_key, override_dir=behavior_config_dir)
    fc = load_behavioral_config(resolved_behavior_config_dir, config.config_key)

    # PHASE _metadata.seed override BEFORE AgentLogger creation, so the
    # session_id derives from the PHASE seed (deterministic per-deploy)
    # rather than falling back to uuid.uuid4().
    apply_phase_seed(config, resolved_behavior_config_dir, fc)

    logger = AgentLogger(agent_type=config.config_key)

    # Mode dispatch (PHASE 2026-05-08). When PHASE marks this SUP as
    # controls, bypass the brain agent entirely and run the brain-agnostic
    # controls floor. Cross-deploy diff stays bit-identical regardless of
    # which service started this process.
    if resolve_runtime_dispatch(
        fc, config.brain, config.config_key
    ) == "scripted_baseline":
        from brains.controls import run_controls
        logger.session_start(config={
            "brain": "controls",
            "config_key": config.config_key,
            "behavior_config_dir": str(resolved_behavior_config_dir),
            "launched_from": "mchp",
        })
        try:
            run_controls(config.config_key,
                         behavior_config_dir=str(resolved_behavior_config_dir),
                         logger=logger)
        except KeyboardInterrupt:
            logger.info("Controls stopped by user (KeyboardInterrupt)")
        except Exception as e:
            logger.session_fail(message="Controls runner failed", exception=e)
            raise
        finally:
            logger.session_end()
        return

    logger.session_start(config={
        "brain": config.brain,
        "content": config.content,
        "model": config.model,
        "calibration": calibration_profile,
        "config_key": config.config_key,
        "seed": config.seed,
        "behavior_config_dir": str(resolved_behavior_config_dir),
    })

    # Set up environment for LLM-augmented configurations
    if config.content == "llm":
        from common.config.model_config import get_model
        model_name = get_model(config.model)
        os.environ["OLLAMA_MODEL"] = model_name
        os.environ["LITELLM_MODEL"] = f"ollama/{model_name}"
        os.environ["HYBRID_LLM_BACKEND"] = "litellm"
        from augmentations.content import set_logger
        set_logger(logger)

    from brains.mchp import MCHPAgent

    is_augmented = config.content == "llm"

    log(f"Running MCHP agent (config: {config.config_key})")
    log(f"Calibration: {calibration_profile or 'none'}")
    if is_augmented:
        log("Excluding Windows-only workflows (OpenOffice, MSPaint)")
    logger.info(f"Starting MCHP agent", details={
        "config_key": config.config_key,
        "calibration": calibration_profile,
    })

    try:
        from common.behavior_v2 import BehaviorV2Snapshot
        agent = MCHPAgent(
            logger=logger,
            calibration_profile=calibration_profile,
            exclude_windows_workflows=is_augmented,
            seed=config.seed,
            behavior_config_dir=str(resolved_behavior_config_dir),
            config_key=config.config_key,
            initial_behavior_snapshot=(
                fc if isinstance(fc, BehaviorV2Snapshot) else None
            ),
        )
        agent.run()
        logger.session_success(message="MCHP agent completed successfully")
    except KeyboardInterrupt:
        logger.info("Agent stopped by user (KeyboardInterrupt)")
    except Exception as e:
        logger.session_fail(message="MCHP agent failed", exception=e)
        raise
    finally:
        logger.session_end()


if __name__ == "__main__":
    import argparse
    from runners.run_config import build_config

    parser = argparse.ArgumentParser(description="Run MCHP brain")
    parser.add_argument("--content", choices=["none", "llm"], default="none")
    parser.add_argument("--model", choices=["llama", "gemma", "gemmac", "gemmar", "deepseek", "lfm", "ministral", "qwen"], default="llama")
    parser.add_argument("--calibration", choices=["summer24", "fall24", "spring25"], default=None)
    parser.add_argument("--phase-timing", action="store_true", help="Legacy: use summer24 calibration")
    parser.add_argument("--behavior-config-dir", type=str, default=None,
                        help="Override behavioral config directory")
    args = parser.parse_args()

    calibration = args.calibration
    if args.phase_timing and not calibration:
        calibration = "summer24"

    config = build_config(brain="mchp", content=args.content, model=args.model,
                          calibration=calibration)
    run_mchp(config, behavior_config_dir=args.behavior_config_dir)
