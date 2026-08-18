"""DECOY SUP deployment command."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from ..core import output
from ..core.ansible_runner import AnsibleRunner, AnsibleEvent, default_event_handler
from ..core.config import DeploymentConfig
from ..core.openstack import OpenStack
from ..core.ssh_config import install_ssh_config
from ..core.feedback import generate_feedback_config
from ..core.revision import RevisionError, resolve_ruse_revision
from ..core.vm_naming import make_vm_prefix
from ..core import run_status
from ..core.deploy_steps import (
    ssh_connectivity_test, register_phase, neighborhood_extra_ips,
)


def run_decoy_spinup(
    config_name: str,
    deploy_dir: Path,
    behavior_source: str | None = None,
    configs_spec: str | None = None,
    gpu_tier: str = "v100",
) -> int:
    """Deploy DECOY SUP agents.

    gpu_tier ∈ {"v100", "rtx", "rtx-a"}. v100 = default (gemma4:26b).
    rtx / rtx-a = B2R.gemma + S2R.gemma on RTX 2080 Ti (gemma4:e4b);
    the two RTX tiers map to distinct physical card pools.
    """
    # If feedback args given but config is decoy-controls, generate feedback config
    if behavior_source and config_name == "decoy-controls":
        config_name = generate_feedback_config(
            Path(behavior_source), configs_spec or "all", deploy_dir,
            gpu_tier=gpu_tier,
        )

    config_dir = deploy_dir / config_name
    config_file = config_dir / "config.yaml"

    if not config_file.exists():
        output.error(f"ERROR: No config.yaml found for: {config_name}")
        return 1

    config = DeploymentConfig.load(config_file)

    # Find hosts.ini
    hosts_ini = _find_hosts_ini(config_dir, deploy_dir)
    if not hosts_ini:
        output.error(f"ERROR: No hosts.ini found for: {config_name}")
        return 1

    # A VM install must be reproducible from one immutable Git object. Resolve
    # this before provisioning so a dirty runtime tree or malformed override
    # cannot leave paid-for VMs waiting on an install that can never match the
    # orchestrator. RUSE_GIT_REF is the explicit escape hatch for deploying a
    # previously published commit while local development is in progress.
    try:
        ruse_revision = resolve_ruse_revision(deploy_dir.parent)
    except RevisionError as exc:
        output.error(f"ERROR: Cannot select immutable RUSE revision: {exc}")
        return 1

    # Generate run ID and paths
    run_id = time.strftime("%m%d%y%H%M%S")
    run_dir = config_dir / "runs" / run_id
    dep_id = _make_run_dep_id(config_name, run_id)
    vm_prefix = make_vm_prefix(dep_id)
    vm_count = config.vm_count()

    # Deploy-time fail-loud (S0): every non-control SUP must have a
    # resolvable behavior.json under the configured behavior_source. This
    # catches missing files BEFORE we provision any VMs — otherwise
    # distribute-behavior-configs.yaml would catch it later, after spending
    # ~20 min on provision + install. C0 and M0 are exempt (no behavior
    # consumption).
    effective_source = behavior_source or config.behavior_source
    src_err = _validate_behavior_source(effective_source, config)
    if src_err:
        output.error("")
        output.error("ABORTING: behavioral configuration not detected.")
        for line in src_err:
            output.error(f"  {line}")
        output.error("")
        output.error("Every DECOY SUP must have a behavior.json. Either fix the")
        output.error("behavior_source in config.yaml, regenerate the missing")
        output.error("PHASE feedback files, or write the controls defaults at")
        output.error("/data/axes-mirror/feedback/decoy-controls/controls/.")
        return 1

    # Display header
    output.banner(f"DEPLOY: {config_name}")
    output.info(f"  VMs:       {config.brain_summary()}")
    output.info(f"  Run ID:    {run_id}")
    output.info(f"  VM prefix: {vm_prefix}*")
    output.info(f"  RUSE ref:  {ruse_revision}")
    if behavior_source:
        output.info(f"  Feedback:  {behavior_source}")
    output.info("")

    # Create run directory
    run_dir.mkdir(parents=True, exist_ok=True)
    _copy_file(config_file, run_dir / "config.yaml")
    (run_dir / "ruse-revision.txt").write_text(f"{ruse_revision}\n")

    # Stamp FAILED up front; flipped to OK only at the final clean return below.
    # Any early return (provision/install/distribute/register abort), exception,
    # or kill leaves this run marked failed — which is what `./teardown --failed`
    # targets. See core/run_status.py.
    run_status.write_run_status(run_dir, run_status.FAILED, "in_progress")

    runner = AnsibleRunner(deploy_dir / "logs")

    # Phase 0: idempotent same-deploy refresh.
    # If a prior run under this same config_name has matching gpu_tier +
    # deployments[] template (= same logical deploy), teardown its VMs and
    # drop its run_dir before we provision the new ones. Without this,
    # ./deploy on an existing config silently piles new VMs alongside old
    # ones (each new run_id makes a different OpenStack name, so there's
    # no collision — just orphan accumulation).
    #
    # Different gpu_tier or different deployments[] under the same name
    # = operator hand-edited config.yaml = NOT a match, leave alone and
    # proceed (caller can fix it up explicitly with ./teardown).
    _teardown_matching_prior_runs(config_dir, run_dir, config)

    # Phase 1: Provision
    output.info("")
    output.info(f"--- Provisioning {vm_count} VMs ---")

    provision_result = runner.run_playbook(
        "shared/provision-vms.yaml",
        hosts_ini,
        extra_vars={
            "deployment_dir": str(config_dir),
            "deployment_id": dep_id,
            "run_dir": str(run_dir),
            # Pass vm_prefix explicitly. provision-vms.yaml hardcoded
            # `r-{deployment_id}-` (legacy from when DECOY used the same
            # prefix as RAMPART). Now d-=DECOY / r-=RAMPART / g-=GHOSTS.
            # Without this, VMs got created as r- but every consumer
            # (audit, teardown, register_experiment) looked for d-.
            "vm_prefix": vm_prefix,
        },
        on_event=default_event_handler,
    )

    inventory_path = run_dir / "inventory.ini"
    if not inventory_path.exists():
        output.info("")
        output.error("Provisioning failed -- no VMs were created successfully.")
        output.dim(f"  Log: {provision_result.log_path}")
        return 1

    if provision_result.rc != 0:
        output.info("")
        output.info("WARNING: Provisioning completed with failures. Continuing install for successful VMs.")

    # Count provisioned VMs and extract host info
    provisioned_hosts = _parse_inventory(inventory_path)
    provisioned = len(provisioned_hosts)
    output.info(f"  {provisioned}/{vm_count} VMs provisioned")

    # Test SSH connectivity (done in Python for real-time output)
    output.info("")
    output.info("--- Testing SSH connectivity ---")
    ssh_ok = ssh_connectivity_test(provisioned_hosts)
    # S1: Fail-loud if too many VMs unreachable. Previously this was a warning
    # and install proceeded against unreachable hosts, eventually "succeeding"
    # while the SUP services on unreachable VMs never got configured.
    ssh_threshold = 0.9
    if ssh_ok < provisioned * ssh_threshold:
        output.error(f"  FAIL: SSH reachable on only {ssh_ok}/{provisioned} VMs "
                     f"(threshold {int(ssh_threshold*100)}%). Aborting.")
        return 1
    elif ssh_ok < provisioned:
        output.info(f"  WARNING: SSH reachable on {ssh_ok}/{provisioned} VMs (threshold met)")
    else:
        output.info(f"  All {ssh_ok} VMs reachable via SSH")

    # Phase 2: Install
    output.info("")
    output.info(f"--- Installing on {provisioned} VMs ---")

    install_playbook = "decoy/install-sups.yaml"
    extra_vars = {
        "deployment_dir": str(config_dir),
        "deployment_id": dep_id,
        "run_dir": str(run_dir),
        "ruse_revision": ruse_revision,
    }

    # Override behavior_source if provided via CLI
    if behavior_source:
        extra_vars["behavior_source"] = behavior_source

    install_result = runner.run_playbook(
        install_playbook,
        inventory_path,
        extra_vars=extra_vars,
        on_event=default_event_handler,
    )

    # I1: Fail-loud on install failures. Ansible exits rc=2 when any host
    # fails a task — in our install-sups.yaml that means an S3/S4/S5
    # assertion tripped (stage2 rc, service is-active, MCHP cron count).
    # Previously spinup.py kept going, distributed configs to every VM
    # (including the failed ones), registered in PHASE, and printed
    # "DONE: 7/7 VMs deployed" even when one or more VMs never got a
    # working service. Abort here so the operator sees the failure
    # immediately and can diagnose from the Ansible log.
    if install_result.rc != 0:
        failed_hosts, succeeded_hosts = _parse_ansible_recap(install_result.log_path)
        total = len(failed_hosts) + len(succeeded_hosts)
        output.error("")
        output.error(f"ABORTING: install-sups.yaml exited with rc={install_result.rc}")
        if total > 0:
            output.error(f"  {len(succeeded_hosts)}/{total} VMs passed install assertions")
            if failed_hosts:
                output.error(f"  Failed: {', '.join(sorted(failed_hosts))}")
        output.error(f"  Log: {install_result.log_path}")
        output.error("  Tear down with: ./teardown " + f"{config_name}-{run_id}")
        return 1

    # Phase 2b: Distribute behavioral configs (if applicable)
    effective_source = behavior_source or config.behavior_source
    if effective_source:
        output.info("")
        output.info("--- Distributing behavioral configs ---")
        dist_vars = {
            "deployment_dir": str(config_dir),
            "deployment_id": dep_id,
            "run_dir": str(run_dir),
            "config_source": effective_source,
        }
        if configs_spec and configs_spec != "all":
            dist_vars["behavior_configs"] = configs_spec

        dist_result = runner.run_playbook(
            "decoy/distribute-behavior-configs.yaml",
            inventory_path,
            extra_vars=dist_vars,
            on_event=default_event_handler,
        )
        # Fail loud on distribute failures. Previously this fired-and-forgot,
        # so a play-level abort (e.g. a `pause` module crashing inside a
        # strategy:free play) would leave behavior.json partially distributed
        # AND skip the install-time service-active assertions, while the
        # deploy reported OK. 9 deploys on 2026-05-01 went out that way.
        if dist_result.rc != 0:
            output.error("")
            output.error(f"ABORTING: distribute-behavior-configs.yaml exited rc={dist_result.rc}")
            output.error(f"  Log: {dist_result.log_path}")
            output.error("  behavior.json may be partially distributed and the")
            output.error("  install-time service-active assertion never fired.")
            output.error(f"  Tear down: ./teardown {config_name}-{run_id}")
            return 1

    # Phase 2c: Neighborhood sidecar (topology-mimicry layer).
    # FEEDBACK ONLY. Controls never reach this branch because:
    #   (a) effective_source is None on controls, and
    #   (b) we gate on topology_mimicry rates existing in the PHASE source.
    # See docs/topology-mimicry.md for design rationale.
    if effective_source:
        sups_json = _synthesize_neighborhood_config(
            Path(effective_source), inventory_path, run_dir,
        )
        if sups_json is not None:
            rc = _provision_and_install_neighborhood(
                runner, dep_id, run_dir, deploy_dir,
            )
            if rc != 0:
                output.error("")
                output.error("ABORTING: neighborhood sidecar failed.")
                output.error("Topology-mimicry layer is not active — feedback deploy "
                             "would be running without the network-layer feature.")
                output.error(f"  Tear down with: ./teardown {config_name}-{run_id}")
                return 1

    # Post-deploy: SSH config + PHASE registration
    snippet_path = run_dir / "ssh_config_snippet.txt"
    if snippet_path.exists():
        output.info("")
        install_ssh_config(snippet_path, f"{config_name}/{run_id}")

    # P1: PHASE registration is fail-loud. Previously a registration failure
    # printed a WARNING and the deploy continued, leaving VMs running but
    # invisible to PHASE inference — logs collected but never analyzed. DONE
    # must mean "every VM functional AND registered" per the fail-loud
    # contract.
    phase_ok = register_phase(
        snippet_path, config_name, run_id,
        extra_ips=neighborhood_extra_ips(run_dir),
        gpu_tier=config.gpu_tier,
    )
    if not phase_ok:
        output.error("")
        output.error("ABORTING: PHASE experiments.json registration failed.")
        output.error("VMs are running but logs won't be picked up by PHASE inference.")
        output.error("Tear down and fix register_experiment.py, or register manually.")
        return 1

    # Final summary
    output.info("")
    output.info(f"DONE: {provisioned}/{vm_count} VMs deployed")
    output.info(f"  Log: {install_result.log_path}")

    if install_result.rc == 0:
        run_status.write_run_status(run_dir, run_status.OK, "deploy complete")
    return install_result.rc


# --- Helpers ---

def _parse_ansible_recap(log_path: Path) -> tuple[set[str], set[str]]:
    """Parse PLAY RECAP from an Ansible log. Returns (failed_hosts, succeeded_hosts).

    PLAY RECAP format:
        hostname : ok=N  changed=N  unreachable=N  failed=N  skipped=N  rescued=N  ignored=N

    A host is considered failed if failed>0 or unreachable>0. Otherwise succeeded.
    Returns empty sets if log can't be read — caller still aborts on rc != 0.
    """
    failed: set[str] = set()
    succeeded: set[str] = set()
    if not log_path.exists():
        return failed, succeeded
    try:
        text = log_path.read_text()
    except OSError:
        return failed, succeeded

    # Find the PLAY RECAP section — everything after the last "PLAY RECAP"
    recap_idx = text.rfind("PLAY RECAP")
    if recap_idx == -1:
        return failed, succeeded

    recap = text[recap_idx:]
    pat = re.compile(
        r"^(\S+)\s*:\s*"
        r"ok=(\d+)\s+"
        r"changed=\d+\s+"
        r"unreachable=(\d+)\s+"
        r"failed=(\d+)",
        re.MULTILINE,
    )
    for match in pat.finditer(recap):
        host = match.group(1)
        unreachable = int(match.group(3))
        fails = int(match.group(4))
        if unreachable > 0 or fails > 0:
            failed.add(host)
        else:
            succeeded.add(host)
    return failed, succeeded


def _find_hosts_ini(config_dir: Path, deploy_dir: Path) -> Path | None:
    if (config_dir / "hosts.ini").exists():
        return config_dir / "hosts.ini"
    if (deploy_dir / "hosts.ini").exists():
        return deploy_dir / "hosts.ini"
    return None


def _derive_behavior_paths(sup_behavior: str) -> tuple[str, str]:
    """Mirror distribute-behavior-configs.yaml regex.

    Returns (behavior_dir, baseline_config). E.g.
      M1         -> ('M',       'M1')
      M2         -> ('M',       'M1')
      B0.gemma   -> ('B.gemma', 'B0.gemma')
      B2C.gemma  -> ('B.gemma', 'B0C.gemma')
      S2.gemma   -> ('S.gemma', 'S0.gemma')
      B2R.gemma  -> ('B.gemma', 'B0R.gemma')  # R-tier reads its OWN baseline
      S2R.gemma  -> ('S.gemma', 'S0R.gemma')  # PHASE now emits distinct B0R/S0R

    R-tier note (2026-06-12): PHASE used to ship only .gemma content shared
    across V100/RTX, so R was stripped (B2R -> B0). PHASE now emits per-tier
    B0R/S0R configs that genuinely differ (seed, pools, behavior modifiers,
    persistent_sessions on/off), so R-tier must read its OWN baseline. The
    behavior_dir stays B.gemma (the family dir) — only the baseline_config
    keeps its R. behavior_dir's regex [A-Z]* greedily consumes the R.
    """
    m = re.match(r'^([A-Z])\d+[A-Z]*(.*)$', sup_behavior)
    behavior_dir = m.group(1) + m.group(2) if m else sup_behavior
    baseline_version = "1" if sup_behavior[:1] == "M" else "0"
    baseline_config = re.sub(r'^([A-Z])\d+', rf"\g<1>{baseline_version}", sup_behavior)
    return behavior_dir, baseline_config


def _stamped_namespace(behavior_json_path: Path) -> str | None:
    """Reconstruct the lineage namespace `{model_preset}_v{model_version}` from a
    behavior.json's `_metadata` PHASE stamp. Returns None when the stamp is
    absent/unreadable — absent stamp DEFERS (older sources have none), matching
    the manifest-optional contract. Field names per the PHASE 2026-06 feedback
    spec; adjust here if PHASE finalizes different names."""
    try:
        meta = (json.loads(behavior_json_path.read_text()) or {}).get("_metadata") or {}
    except (OSError, json.JSONDecodeError):
        return None
    mp, mv = meta.get("model_preset"), meta.get("model_version")
    return f"{mp}_v{mv}" if mp and mv else None


def _validate_behavior_source(
    effective_source: str | None, config,
) -> list[str]:
    """Check every non-C0/M0 SUP has a resolvable behavior.json.

    Returns a list of error lines (empty = OK). Caller decides how to
    abort. Walks config.deployments, derives the expected
    {behavior_dir}/{baseline_config}/behavior.json path, and verifies
    each one exists under effective_source. C0 / M0 exempt (they don't
    read behavior.json).
    """
    errors: list[str] = []
    if not effective_source:
        errors.append("No behavior_source configured. DECOY controls must point at")
        errors.append("/data/axes-mirror/feedback/decoy-controls/controls (set in config.yaml).")
        return errors

    src = Path(effective_source)
    if not src.is_dir():
        errors.append(f"behavior_source not a directory: {src}")
        return errors

    # Lineage assert (PHASE 2026-06): feedback sources live under a
    # {preset}_v{version} namespace dir, so src.parent.name IS the expected
    # lineage. Catch a --preset pointed at a source whose configs are stamped
    # for a different lineage. Only feedback namespaces look like
    # "{preset}_v{version}"; the un-namespaced controls/ slot (parent name e.g.
    # "decoy-controls") has no "_v", so it's skipped.
    expected_ns = src.parent.name
    check_lineage = "_v" in expected_ns
    lineage_errs: list[str] = []

    missing: list[str] = []
    seen: set[tuple[str, str]] = set()
    for dep in config.deployments:
        beh = dep.get("behavior", "")
        if beh in ("C0", "M0"):
            continue
        behavior_dir, baseline_config = _derive_behavior_paths(beh)
        if (behavior_dir, baseline_config) in seen:
            continue
        seen.add((behavior_dir, baseline_config))
        path = src / behavior_dir / baseline_config / "behavior.json"
        if not path.is_file():
            missing.append(f"{beh}: expected {path}")
            continue
        if check_lineage:
            stamped = _stamped_namespace(path)
            if stamped and stamped != expected_ns:
                lineage_errs.append(
                    f"{beh}: {path} stamped lineage {stamped!r} != "
                    f"deployed namespace {expected_ns!r}")

    if missing:
        errors.append(f"behavior_source: {src}")
        errors.append(f"missing behavior.json for {len(missing)} SUP(s):")
        for m in missing:
            errors.append(f"  - {m}")
    if lineage_errs:
        errors.append(f"lineage mismatch under {src}: configs stamped for a "
                      f"different model_preset/model_version than --preset "
                      f"{expected_ns!r}:")
        for m in lineage_errs:
            errors.append(f"  - {m}")
    return errors


def _make_run_dep_id(deployment_name: str, run_id: str) -> str:
    """Build run dep_id: strip prefixes, remove hyphens, append run_id."""
    dep = deployment_name
    if dep.startswith("decoy-"):
        dep = dep[len("decoy-"):]
    dep = dep.replace("-", "")
    return f"{dep}{run_id}"


def _copy_file(src: Path, dst: Path) -> None:
    import shutil
    shutil.copy2(src, dst)


def _teardown_matching_prior_runs(
    config_dir: Path, new_run_dir: Path, new_config: DeploymentConfig,
) -> None:
    """Teardown VMs from any prior run of this config_name whose
    gpu_tier + deployments[] match the new config.

    Three checks per prior run (all must match to count as "same deploy"):
      1. prior run_dir/config.yaml exists (= the run got past Phase 0)
      2. prior config.gpu_tier == new config.gpu_tier
      3. prior config.deployments == new config.deployments

    On match: openstack server delete for every VM under the prior run's
    prefix, then safe_rmtree the prior run_dir. The experiments.json entry
    is left alone — register_phase's upsert will refresh end_date=None
    + IPs at the end of this same spinup.

    On mismatch (gpu_tier or deployments[] changed): leave the prior run
    fully intact. Operator clearly meant something different by reusing
    the name; they can clean it up with explicit ./teardown.
    """
    runs_dir = config_dir / "runs"
    if not runs_dir.is_dir():
        return

    prior_run_dirs = [
        d for d in runs_dir.iterdir()
        if d.is_dir() and d != new_run_dir and (d / "config.yaml").exists()
    ]
    if not prior_run_dirs:
        return

    from ..core.teardown_steps import request_server_deletes, safe_rmtree, wait_until_zero

    to_teardown: list[tuple[Path, str]] = []  # (prior_run_dir, vm_prefix)
    for prior in prior_run_dirs:
        try:
            prior_cfg = DeploymentConfig.load(prior / "config.yaml")
        except Exception as e:
            output.dim(f"  skipping prior run {prior.name}: can't parse config.yaml ({e})")
            continue
        if prior_cfg.gpu_tier != new_config.gpu_tier:
            output.dim(
                f"  prior run {prior.name} has gpu_tier={prior_cfg.gpu_tier} "
                f"(new is {new_config.gpu_tier}) — leaving alone"
            )
            continue
        if prior_cfg.deployments != new_config.deployments:
            output.dim(
                f"  prior run {prior.name} has different deployments[] — leaving alone"
            )
            continue
        prior_dep_id = _make_run_dep_id(new_config.deployment_name, prior.name)
        prior_prefix = make_vm_prefix(prior_dep_id)
        to_teardown.append((prior, prior_prefix))

    if not to_teardown:
        return

    output.info("")
    output.info(f"--- Refreshing {len(to_teardown)} matching prior run(s) ---")
    os_client = OpenStack()
    for prior_dir, prior_prefix in to_teardown:
        servers = os_client.server_list_with_ids(prefix=prior_prefix)
        if servers:
            output.info(f"  Deleting {len(servers)} VM(s) under {prior_prefix}*")
            request_server_deletes(os_client, servers)
            remaining = wait_until_zero(os_client, prior_prefix)
            if remaining:
                output.error(
                    f"  ERROR: {remaining} VM(s) under {prior_prefix}* still alive after teardown wait. "
                    f"Aborting before provisioning new VMs (avoid mixing old + new state)."
                )
                raise SystemExit(1)
        else:
            output.dim(f"  no live VMs under {prior_prefix}* — just dropping run_dir")
        safe_rmtree(prior_dir)
        output.dim(f"  dropped prior run_dir {prior_dir.name}")


def _parse_inventory(inventory_path: Path) -> list[dict]:
    """Parse inventory.ini into list of {name, ip, behavior}."""
    import re
    hosts = []
    for line in inventory_path.read_text().splitlines():
        match = re.match(r"^(\S+)\s+ansible_host=(\S+)\s+sup_behavior=(\S+)", line)
        if match:
            hosts.append({
                "name": match.group(1),
                "ip": match.group(2),
                "behavior": match.group(3),
            })
    return hosts

# ─── Neighborhood sidecar (topology-mimicry) ───────────────────────────────

def _resolve_sup_behavior_json(behavior_source: Path, behavior: str,
                               baseline_config: str) -> Path | None:
    """Locate the behavior.json for a SUP in the PHASE source tree.

    Mirrors the derivation the distribute playbook does on-VM:
      behavior_dir = {first_letter}{.model_suffix?}  (e.g. B.gemma, M)
      path = {source}/{behavior_dir}/{baseline_config}/behavior.json
    """
    # Strip trailing C (CPU-variant) for behavior_dir derivation — PHASE
    # generates one config per {brain_letter}{.model} regardless of CPU/GPU.
    m = re.match(r'^([A-Z])\d+[CR]?(?:\.(\w+))?$', behavior)
    if not m:
        return None
    behavior_dir = f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)
    path = behavior_source / behavior_dir / baseline_config / "behavior.json"
    return path if path.exists() else None


def _synthesize_neighborhood_config(behavior_source: Path, inventory_path: Path,
                                    run_dir: Path) -> dict | None:
    """Read each SUP's behavior.json topology_mimicry rates and write
    run_dir/neighborhood-sups.json.

    Returns the config dict if at least one SUP has non-zero rates; None
    if no topology_mimicry was configured anywhere (daemon would be idle —
    don't bother provisioning the sidecar).
    """
    import json

    # Parse inventory for SUP name/ip/behavior tuples
    sups = []
    sidecar_seed = None  # first non-None _metadata.seed encountered
    for line in inventory_path.read_text().splitlines():
        m = re.match(r'^(\S+)\s+ansible_host=(\S+)\s+sup_behavior=(\S+)', line)
        if not m:
            continue
        name, ip, behavior = m.group(1), m.group(2), m.group(3)
        if behavior in ("C0", "M0"):
            # Controls within a feedback deploy aren't feedback-driven.
            continue
        # Derive baseline config key (B2.gemma -> B0.gemma, M2 -> M1, etc.)
        baseline_version = "1" if behavior[0] == "M" else "0"
        baseline = re.sub(r'^([A-Z])\d+', r'\g<1>' + baseline_version, behavior)
        bjson = _resolve_sup_behavior_json(behavior_source, behavior, baseline)
        if bjson is None:
            continue
        try:
            data = json.loads(bjson.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if sidecar_seed is None:
            raw = (data.get("_metadata") or {}).get("seed")
            if raw is not None:
                try:
                    sidecar_seed = int(raw)
                except (TypeError, ValueError):
                    pass
        rates = ((data.get("diversity") or {}).get("topology_mimicry") or {})
        # Filter to int-ish positive values only
        clean_rates = {}
        for k, v in rates.items():
            try:
                n = int(v)
                if n > 0:
                    clean_rates[k] = n
            except (TypeError, ValueError):
                continue
        sups.append({"name": name, "ip": ip, "rates": clean_rates})

    has_active = any(s["rates"] for s in sups)
    if not has_active:
        output.dim("  No topology_mimicry rates in PHASE source — skipping neighborhood sidecar")
        return None

    cfg = {"sups": sups}
    if sidecar_seed is not None:
        # Sidecar reads this and seeds random.seed() at daemon start.
        # Picks the first SUP's _metadata.seed — all SUPs in a deploy share
        # the same dataset, so any one is fine for the sidecar's RNG anchor.
        cfg["seed"] = sidecar_seed
    out_path = run_dir / "neighborhood-sups.json"
    out_path.write_text(json.dumps(cfg, indent=2) + "\n")
    output.info("")
    output.info("--- Synthesized neighborhood config ---")
    output.info(f"  SUPs with topology_mimicry: {sum(1 for s in sups if s['rates'])}/{len(sups)}")
    total_ph = sum(sum(s['rates'].values()) for s in sups)
    output.info(f"  Total probes / hour (all SUPs): {total_ph}")
    output.info(f"  Config: {out_path}")
    return cfg


def _provision_and_install_neighborhood(
    runner: AnsibleRunner, dep_id: str, run_dir: Path, deploy_dir: Path,
) -> int:
    """Provision 1 neighborhood VM, write neighborhood-inventory.ini, run
    install-neighborhood.yaml. Returns 0 on success, non-zero on failure."""
    import subprocess
    import shlex
    import json

    # Sidecar is part of the DECOY deploy → lives under d-.
    vm_name = f"{make_vm_prefix(dep_id)}neighborhood-0"
    rc_file = os.path.expanduser("~/vxn3kr-bot-rc")
    # v1.small = 1 vCPU, 2 GB RAM — a probe daemon needs almost nothing.
    # Avoids pressure on the v1.14vcpu.28g pool used by the SUPs.
    flavor = "v1.small"
    image = "noble-amd64"
    network = "ext_net"
    keypair = "bot-desktop"
    security_group = "default"

    output.info("")
    output.info("--- Provisioning neighborhood sidecar VM ---")
    output.info(f"  Name: {vm_name}")

    # Create VM (idempotent: exit 0 if already exists)
    create_cmd = (
        f"source {shlex.quote(rc_file)} && "
        f"if openstack server show {shlex.quote(vm_name)} &>/dev/null; then "
        f"  echo EXISTS; exit 0; "
        f"else "
        f"  openstack server create "
        f"    --flavor {flavor} --image {image} --boot-from-volume 200 "
        f"    --network {network} --key-name {keypair} "
        f"    --security-group {security_group} "
        f"    --property deployment={dep_id} "
        f"    -f value -c id {shlex.quote(vm_name)}; "
        f"fi"
    )
    r = subprocess.run(["bash", "-c", create_cmd], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        output.error(f"  FAIL: VM create: {(r.stderr or '').strip()[:200]}")
        return 1
    output.info(f"  [{time.strftime('%H:%M:%S')}]    OK  {vm_name} provisioned")

    # Wait for ACTIVE
    for attempt in range(60):
        rs = subprocess.run(
            ["bash", "-c",
             f"source {shlex.quote(rc_file)} && "
             f"openstack server show {shlex.quote(vm_name)} -f value -c status"],
            capture_output=True, text=True, timeout=30,
        )
        status = (rs.stdout or "").strip()
        if status == "ACTIVE":
            break
        if status == "ERROR":
            output.error(f"  FAIL: neighborhood VM in ERROR state")
            return 1
        time.sleep(5)
    else:
        output.error(f"  FAIL: neighborhood VM never reached ACTIVE ({status})")
        return 1

    # Get IP
    ri = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(rc_file)} && "
         f"openstack server show {shlex.quote(vm_name)} -f value -c addresses "
         f"| grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+'"],
        capture_output=True, text=True, timeout=30,
    )
    vm_ip = (ri.stdout or "").strip().splitlines()[0] if ri.stdout.strip() else ""
    if not vm_ip:
        output.error(f"  FAIL: could not resolve IP for {vm_name}")
        return 1
    output.info(f"  [{time.strftime('%H:%M:%S')}]    OK  {vm_name} => {vm_ip}")

    # Write inventory
    inv_path = run_dir / "neighborhood-inventory.ini"
    inv_path.write_text(
        f"# Auto-generated neighborhood inventory\n"
        f"# Generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n\n"
        f"[neighborhood_hosts]\n"
        f"{vm_name} ansible_host={vm_ip}\n\n"
        f"[neighborhood_hosts:vars]\n"
        f"ansible_user=ubuntu\n"
        f"ansible_python_interpreter=/usr/bin/python3\n"
        f"ansible_ssh_common_args=-o StrictHostKeyChecking=no\n"
    )

    # Wait for SSH to be reachable
    ssh_ok = False
    for attempt in range(30):
        rp = subprocess.run(
            ["ssh",
             "-i", os.path.expanduser("~/.ssh/id_ed25519"),
             "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10",
             "-o", "BatchMode=yes",
             f"ubuntu@{vm_ip}", "echo ok"],
            capture_output=True, timeout=15,
            env={**os.environ, "SSH_AUTH_SOCK": ""},
        )
        if rp.returncode == 0:
            ssh_ok = True
            break
        time.sleep(5)
    if not ssh_ok:
        output.error(f"  FAIL: SSH never reachable on {vm_name}")
        return 1

    # Add to ~/.ssh/config so operator can SSH by name
    snippet_path = run_dir / "neighborhood-ssh-snippet.txt"
    snippet_path.write_text(
        f"############# Neighborhood - {dep_id} #############\n\n"
        f"Host n-*\n"
        f"    User ubuntu\n"
        f"    PreferredAuthentications publickey\n"
        f"    IdentityFile ~/.ssh/id_ed25519\n"
        f"    IdentitiesOnly yes\n"
        f"    StrictHostKeyChecking no\n"
        f"    UserKnownHostsFile /dev/null\n\n"
        f"Host {vm_name}\n"
        f"    HostName {vm_ip}\n\n"
        f"#############################################\n"
    )

    # Run install playbook
    output.info("")
    output.info("--- Installing neighborhood daemon ---")
    result = runner.run_playbook(
        "decoy/install-neighborhood.yaml",
        inv_path,
        extra_vars={
            "deployment_dir": str(run_dir.parent.parent),
            "run_dir": str(run_dir),
        },
        on_event=default_event_handler,
    )
    if result.rc != 0:
        output.error(f"  FAIL: install-neighborhood.yaml rc={result.rc}")
        output.error(f"  Log: {result.log_path}")
        return 1

    output.info(f"  Neighborhood sidecar active at {vm_ip}")
    return 0
