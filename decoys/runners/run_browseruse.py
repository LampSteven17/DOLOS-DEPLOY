"""
Runner for BrowserUse brain configurations.

Configurations (exp-3):
- B1.{llama,gemma}: BrowserUse baseline (no timing)
- B2.{llama,gemma}: BrowserUse + summer24 calibrated timing
- B3.{llama,gemma}: BrowserUse + fall24 calibrated timing
- B4.{llama,gemma}: BrowserUse + spring25 calibrated timing
"""
from datetime import datetime
from runners.run_config import SUPConfig
from common.logging.agent_logger import AgentLogger


def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def run_browseruse(config: SUPConfig, task: str = None, behavior_config_dir: str = None):
    """Run BrowserUse brain in single-task mode."""
    # Resolve behavioral config directory
    from common.behavioral_config import (
        resolve_behavioral_config_dir, load_behavioral_config, apply_phase_seed,
        resolve_runtime_dispatch,
    )
    resolved_behavior_config_dir = resolve_behavioral_config_dir(config.config_key, override_dir=behavior_config_dir)
    fc = load_behavioral_config(resolved_behavior_config_dir, config.config_key)

    # PHASE _metadata.seed override before AgentLogger creation — see
    # apply_phase_seed docstring for why this lives in the runner.
    apply_phase_seed(config, resolved_behavior_config_dir, fc)

    from common.behavior_v2 import BehaviorV2Snapshot
    if isinstance(fc, BehaviorV2Snapshot):
        resolve_runtime_dispatch(fc, config.brain, config.config_key)
        raise RuntimeError(
            "V2 single-task actuation is disabled until separately authorized R2/R3 work"
        )

    logger = AgentLogger(agent_type=config.config_key)
    logger.session_start(config={
        "brain": config.brain,
        "model": config.model,
        "calibration": config.calibration,
        "config_key": config.config_key,
        "seed": config.seed,
        "behavior_config_dir": str(resolved_behavior_config_dir),
    })

    from brains.browseruse import BrowserUseAgent, DEFAULT_PROMPTS, PHASE_PROMPTS
    from brains.browseruse.tasks import get_random_task

    prompts = PHASE_PROMPTS if config.phase else DEFAULT_PROMPTS

    # Apply prompt augmentation from behavioral config
    prompt_augmentation = getattr(fc, "prompt_augmentation", None)
    if prompt_augmentation and prompt_augmentation.get("prompt_content"):
        from brains.browseruse.prompts import BUPrompts
        prompts = BUPrompts(task=prompts.task, content=prompt_augmentation["prompt_content"])
        log(f"[behavior] Applied prompt augmentation for {config.config_key}")
    if task is None:
        task = get_random_task()

    log(f"Running BrowserUse agent (config: {config.config_key})")
    log(f"Calibration: {config.calibration or 'none'}")
    logger.info("Starting BrowserUse agent", details={
        "config_key": config.config_key,
        "calibration": config.calibration,
        "task": task
    })

    try:
        agent = BrowserUseAgent(prompts=prompts, model=config.model, logger=logger)
        result = agent.run(task)
        logger.session_success(message="BrowserUse agent completed successfully",
                               details={"result": str(result)[:500] if result else None})
        return result
    except KeyboardInterrupt:
        logger.info("Agent stopped by user (KeyboardInterrupt)")
    except Exception as e:
        logger.session_fail(message="BrowserUse agent failed", exception=e)
        raise
    finally:
        logger.session_end()


def run_browseruse_loop(config: SUPConfig, behavior_config_dir: str = None):
    """Run BrowserUse in loop mode (continuous execution)."""
    calibration_profile = config.calibration

    # Resolve behavioral config directory
    from common.behavioral_config import (
        resolve_behavioral_config_dir, load_behavioral_config,
        apply_phase_seed, resolve_runtime_dispatch,
    )
    resolved_behavior_config_dir = resolve_behavioral_config_dir(config.config_key, override_dir=behavior_config_dir)
    fc = load_behavioral_config(resolved_behavior_config_dir, config.config_key)

    # PHASE _metadata.seed override before AgentLogger creation.
    apply_phase_seed(config, resolved_behavior_config_dir, fc)

    logger = AgentLogger(agent_type=config.config_key)

    # Mode dispatch (PHASE 2026-05-08). When PHASE marks this SUP as
    # controls, bypass BrowserUse entirely and run the brain-agnostic
    # controls floor.
    if resolve_runtime_dispatch(
        fc, config.brain, config.config_key
    ) == "scripted_baseline":
        from brains.controls import run_controls
        logger.session_start(config={
            "brain": "controls",
            "config_key": config.config_key,
            "behavior_config_dir": str(resolved_behavior_config_dir),
            "launched_from": "browseruse",
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
        "model": config.model,
        "calibration": calibration_profile,
        "loop_mode": True,
        "config_key": config.config_key,
        "seed": config.seed,
        "behavior_config_dir": str(resolved_behavior_config_dir),
    })

    from brains.browseruse import BrowserUseLoop, DEFAULT_PROMPTS, PHASE_PROMPTS

    prompts = PHASE_PROMPTS if config.phase else DEFAULT_PROMPTS

    # Apply prompt augmentation from behavioral config (already loaded above)
    prompt_augmentation = getattr(fc, "prompt_augmentation", None)
    if prompt_augmentation and prompt_augmentation.get("prompt_content"):
        from brains.browseruse.prompts import BUPrompts
        prompts = BUPrompts(task=prompts.task, content=prompt_augmentation["prompt_content"])
        log(f"[behavior] Applied prompt augmentation for {config.config_key}")

    log(f"Running BrowserUse loop (config: {config.config_key})")
    log(f"Calibration: {calibration_profile or 'none'}")
    logger.info("Starting BrowserUse loop", details={
        "config_key": config.config_key,
        "calibration": calibration_profile,
    })

    try:
        from common.behavior_v2 import BehaviorV2Snapshot
        agent = BrowserUseLoop(
            model=config.model,
            prompts=prompts,
            logger=logger,
            calibration_profile=calibration_profile,
            seed=config.seed,
            behavior_config_dir=str(resolved_behavior_config_dir),
            config_key=config.config_key,
            initial_behavior_snapshot=(
                fc if isinstance(fc, BehaviorV2Snapshot) else None
            ),
        )
        agent.run()
        logger.session_success(message="BrowserUse loop completed successfully")
    except KeyboardInterrupt:
        logger.info("Agent stopped by user (KeyboardInterrupt)")
    except Exception as e:
        logger.session_fail(message="BrowserUse loop failed", exception=e)
        raise
    finally:
        logger.session_end()


if __name__ == "__main__":
    import argparse
    from runners.run_config import build_config

    parser = argparse.ArgumentParser(description="Run BrowserUse brain")
    parser.add_argument("task", nargs="?", default=None)
    parser.add_argument("--model", choices=["llama", "gemma", "gemmac", "gemmar", "deepseek", "lfm", "ministral", "qwen"], default="llama")
    parser.add_argument("--calibration", choices=["summer24", "fall24", "spring25"], default=None)
    parser.add_argument("--phase", action="store_true", help="Legacy: use summer24 calibration")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--behavior-config-dir", type=str, default=None,
                        help="Override behavioral config directory")
    args = parser.parse_args()

    calibration = args.calibration
    if args.phase and not calibration:
        calibration = "summer24"

    config = build_config(brain="browseruse", model=args.model,
                          calibration=calibration, cpu_only=args.cpu)

    if args.loop:
        run_browseruse_loop(config, behavior_config_dir=args.behavior_config_dir)
    else:
        run_browseruse(config, task=args.task, behavior_config_dir=args.behavior_config_dir)
