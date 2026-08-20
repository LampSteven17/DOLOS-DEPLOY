"""GHOSTS NPC traffic generator deployment command."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import output
from ..core import run_status
from ..core.ansible_runner import AnsibleRunner, default_event_handler
from ..core.config import DeploymentConfig
from ..core.openstack import OpenStack
from ..core.ssh_config import install_ssh_config
from ..core.vm_naming import make_ghosts_vm_prefix
from ..core.deploy_steps import register_phase_run, ssh_connectivity_test
from ..core.phase_run_registry import (
    PhaseRunRegistryError,
    close_deployment,
    run_id_from_started_at,
    utc_deployment_start,
)


def run_ghosts_spinup(
    config_name: str | None,
    deploy_dir: Path,
    behavior_source: str | None = None,
    configs_spec: str | None = None,
) -> int:
    """Deploy GHOSTS API + NPC client VMs."""
    config_name = config_name or "ghosts-controls"

    # Feedback args + ghosts-controls base → derive a feedback config dir
    # (e.g. ghosts-feedback-stdctrls-fall24-all). Skip when behavior_source
    # is None — that's the controls path (ghosts-controls/config.yaml's own
    # behavior_source is picked up below as effective_source). Keeps controls
    # deploys named ghosts-controls instead of getting a verbose derived dir.
    if behavior_source and config_name == "ghosts-controls":
        from ..core.feedback import generate_ghosts_feedback_config
        config_name = generate_ghosts_feedback_config(
            Path(behavior_source), configs_spec or "all", deploy_dir,
        )

    config_dir = _find_ghosts_config(config_name, deploy_dir)
    if not config_dir:
        output.error("ERROR: No GHOSTS deployment config found")
        return 1

    deployment = config_dir.name
    config = DeploymentConfig.load(config_dir / "config.yaml")

    # Source dispatch: explicit CLI behavior_source (feedback dataset) wins;
    # else fall back to the deployment's own config.yaml (controls path).
    effective_source = behavior_source or config.behavior_source

    client_count = config.ghosts_client_count()
    total_vms = 1 + client_count  # 1 API + N clients

    started_at = utc_deployment_start()
    run_id = run_id_from_started_at(started_at)
    run_dir = config_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    dep_id = _make_dep_id(deployment, run_id)
    g_prefix = make_ghosts_vm_prefix(dep_id)

    # Header
    output.banner(f"DEPLOY: GHOSTS ({deployment})")
    output.info(f"  VMs:       {total_vms} (1 api + {client_count} npc)")
    output.info(f"  Run ID:    {run_id}")
    output.info(f"  VM prefix: {g_prefix}*")
    output.info(f"  Repo:      {config.ghosts_repo()} ({config.ghosts_branch()})")
    if effective_source:
        output.info(f"  Source:    {effective_source}")
        if configs_spec:
            output.info(f"  Configs:   {configs_spec}")
    output.info("")

    # Snapshot config
    shutil.copy2(config_dir / "config.yaml", run_dir / "config.yaml")

    # Stamp FAILED up front; flipped to OK only at the final clean return below.
    # Any early return (provision/SSH/install/register abort), exception, or
    # kill leaves this run marked failed — which is what `./teardown --failed`
    # targets. Matches decoy/spinup.py + rampart/spinup.py. See core/run_status.py.
    run_status.write_run_status(run_dir, run_status.FAILED, "in_progress")

    os_client = OpenStack()

    # Phase 0: idempotent same-deploy refresh.
    # If a prior run under this same config_name has a matching ghosts
    # topology (= same logical deploy), teardown its VMs and drop its
    # run_dir before we provision the new ones. Without this, re-running
    # ./deploy against an existing config silently piles new VMs alongside
    # old ones — each new run_id hashes to a different g- prefix, so there's
    # no name collision, just orphan accumulation. Matches decoy/spinup.py's
    # _teardown_matching_prior_runs. A hand-edited ghosts block (client_count
    # / flavor change) reads as a different deploy → left intact.
    _teardown_matching_prior_runs(os_client, config_dir, run_dir, config)

    # [1/5] Provision VMs
    output.info("[1/5] Provisioning VMs...")
    vms = _provision_vms(os_client, config, g_prefix, run_dir)
    if not vms:
        output.error("  FAILED: Could not provision VMs")
        return 1

    api_vm = vms["api"]
    client_vms = vms["clients"]
    all_vms = [api_vm] + client_vms

    output.info(f"  {len(all_vms)}/{total_vms} VMs provisioned")

    # Route PHASE per-NPC timelines (if any) BEFORE writing the inventory
    # so per-host timeline paths can be baked into inventory variables.
    # Fail loud on a feedback source with no npc-*/timeline.json — no
    # silent fallback to a single shared timeline.
    timeline_mapping: dict[str, Path] = {}
    if effective_source:
        output.info("")
        output.info("  Routing PHASE per-NPC timelines...")
        try:
            timeline_mapping = _build_npc_timeline_mapping(
                Path(effective_source), client_vms, run_dir,
            )
        except RuntimeError as e:
            # _build_npc_timeline_mapping already printed the detailed error
            output.error(f"  Aborting: {e}")
            return 1
        if not timeline_mapping:
            output.error(
                f"  FAILED: no npc-*/timeline.json files in {effective_source}"
            )
            output.error(
                "  Expected PHASE Stage 2 layout with "
                "{source}/npc-0/timeline.json, npc-1/timeline.json, ..."
            )
            return 1
        output.info(f"  Routed {len(timeline_mapping)} per-NPC timelines")

    # Write inventory and SSH config (inventory includes per-host timeline paths)
    _write_inventory(api_vm, client_vms, run_dir, deployment, timeline_mapping)
    _write_ssh_config(all_vms, run_dir, deployment)

    # [2/5] Test SSH
    output.info("")
    output.info("[2/5] Testing SSH connectivity...")
    ssh_ok = ssh_connectivity_test(all_vms)
    total = len(all_vms)
    # G3: Fail-loud if too many VMs unreachable. Previously this was a warning
    # and the deploy continued to try to Ansible against unreachable hosts,
    # eventually "succeeding" with broken infrastructure.
    ssh_threshold = 0.9
    if ssh_ok < total * ssh_threshold:
        output.error(f"  FAIL: SSH reachable on only {ssh_ok}/{total} VMs "
                     f"(threshold {int(ssh_threshold*100)}%). Aborting.")
        return 1
    elif ssh_ok < total:
        output.info(f"  WARNING: SSH reachable on {ssh_ok}/{total} VMs (threshold met)")
    else:
        output.info(f"  All {ssh_ok} VMs reachable via SSH")

    inventory_path = run_dir / "inventory.ini"
    runner = AnsibleRunner(deploy_dir / "logs")

    # [3/5] Install GHOSTS API
    output.info("")
    output.info("[3/5] Installing GHOSTS API stack...")
    api_result = runner.run_playbook(
        "ghosts/install-ghosts-api.yaml",
        inventory_path,
        extra_vars={
            "ghosts_repo": config.ghosts_repo(),
            "ghosts_branch": config.ghosts_branch(),
        },
        on_event=default_event_handler,
    )
    # G1: Abort if API install failed. Previously this logged an error but
    # continued to install clients against a dead API — resulting in clients
    # that could never register, with zero indication in the deploy output.
    if api_result.rc != 0:
        output.error(f"  FAIL: GHOSTS API installation failed (rc={api_result.rc})")
        output.error(f"  Log: {api_result.log_path}")
        output.error("  Aborting deploy — clients would install against dead API.")
        return api_result.rc

    # [4/5] Install GHOSTS clients
    # Per-host ghosts_timeline_file is already in the inventory (injected by
    # _write_inventory above when timeline_mapping is non-empty).
    output.info("")
    output.info(f"[4/5] Installing GHOSTS clients ({client_count} VMs)...")
    client_result = runner.run_playbook(
        "ghosts/install-ghosts-clients.yaml",
        inventory_path,
        extra_vars={
            # Pin the CLIENT build to the same ref as the API. Without these the
            # client playbook fell back to its own `ghosts_branch: master`
            # default — so a pinned config.yaml pinned the API but the NPC
            # clients still cloned master (the part that actually runs Firefox).
            "ghosts_repo": config.ghosts_repo(),
            "ghosts_branch": config.ghosts_branch(),
            # Feedback deploys get a systemd drop-in capping the .NET client's
            # memory, mitigating the upstream cmu-sei/GHOSTS memleak. Controls
            # stay on the pure upstream unit (leaky-as-designed) so they
            # remain experimentally pristine. Now that ghosts-controls also
            # carries a behavior_source (PHASE-emitted controls timelines),
            # distinguish via the config_name shape: feedback runs deploy
            # under `ghosts-feedback-*`, controls under `ghosts-controls`.
            "is_feedback": "true" if config_name.startswith("ghosts-feedback-") else "false",
        },
        on_event=default_event_handler,
    )
    # G2: Abort if client install failed. Previously the final return was
    # `client_result.rc if api_result.rc == 0 else api_result.rc`, which
    # correctly surfaced API failures but also meant the deploy returned 0
    # from a successful API + failed clients (because client failures weren't
    # re-checked after confirming API was OK).
    if client_result.rc != 0:
        output.error(f"  FAIL: GHOSTS client installation failed (rc={client_result.rc})")
        output.error(f"  Log: {client_result.log_path}")
        return client_result.rc

    # [5/5] Finalize
    output.info("")
    output.info("[5/5] Finalizing...")

    (run_dir / "deployment_type").write_text("ghosts")

    snippet_path = run_dir / "ssh_config_snippet.txt"
    if snippet_path.exists():
        install_ssh_config(snippet_path, f"{deployment}/{run_id}")

    # P1: PHASE registration is fail-loud — consistent with spinup.py and
    # rampart.py. A registered-but-missing deploy means logs are invisible
    # to PHASE inference.
    phase_vms = [
        {"name": vm["name"], "ip": vm["ip"], "sup_config": None}
        for vm in all_vms
    ]
    if not register_phase_run(config, "ghosts", started_at, phase_vms):
        output.error("")
        output.error("ABORTING: PHASE deployment registration failed.")
        output.error("GHOSTS VMs are running but this fleet has no PHASE run record.")
        return 1

    output.info("")
    output.info(f"DONE: GHOSTS deployment {deployment}/{run_id}")
    output.info(f"  API:      ssh {api_vm['name']} (http://{api_vm['ip']}:5000)")
    output.info(f"  Frontend: http://{api_vm['ip']}:4200")
    output.info(f"  Grafana:  http://{api_vm['ip']}:3000")
    output.info(f"  Clients:  {len(client_vms)} NPCs")

    run_status.write_run_status(run_dir, run_status.OK, "deploy complete")
    return 0


# --- VM Provisioning ---

def _provision_vms(
    os_client: OpenStack,
    config: DeploymentConfig,
    g_prefix: str,
    run_dir: Path,
) -> dict | None:
    """Provision API + client VMs on OpenStack. Returns {api: {...}, clients: [...]}."""
    rc_file = Path.home() / "vxn3kr-bot-rc"
    os_image = "noble-amd64"
    os_network = "ext_net"
    os_keypair = "bot-desktop"
    os_security_group = "default"

    # Build VM list: 1 API + N clients
    vm_specs = []
    vm_specs.append({
        "name": f"{g_prefix}api-0",
        "flavor": config.ghosts_api_flavor(),
        "role": "api",
    })
    for i in range(config.ghosts_client_count()):
        vm_specs.append({
            "name": f"{g_prefix}npc-{i}",
            "flavor": config.ghosts_client_flavor(),
            "role": "client",
        })

    # Create VMs
    for spec in vm_specs:
        ts = time.strftime("%H:%M:%S")
        result = _openstack_cmd(
            rc_file,
            "server", "create",
            "--flavor", spec["flavor"],
            "--image", os_image,
            "--boot-from-volume", "200",
            "--network", os_network,
            "--key-name", os_keypair,
            "--security-group", os_security_group,
            "-f", "value", "-c", "id",
            spec["name"],
        )
        if result.returncode == 0:
            output.info(f"  [{ts}]    OK  {spec['name']}")
        else:
            output.info(f"  [{ts}]    FAIL  {spec['name']}  {result.stderr[:80]}")

    # Wait for ACTIVE — track which VMs succeed
    output.info("")
    output.info("  Waiting for VMs to reach ACTIVE state...")
    active_specs = []
    for spec in vm_specs:
        reached_active = False
        for attempt in range(60):
            result = _openstack_cmd(
                rc_file,
                "server", "show", spec["name"],
                "-f", "value", "-c", "status",
            )
            status = result.stdout.strip()
            if status == "ACTIVE":
                reached_active = True
                break
            elif status == "ERROR":
                output.error(f"  [{time.strftime('%H:%M:%S')}]    FAIL  {spec['name']} (ERROR state)")
                break
            time.sleep(5)
        if reached_active:
            active_specs.append(spec)
        else:
            output.error(f"  [{time.strftime('%H:%M:%S')}]    FAIL  {spec['name']} (never reached ACTIVE)")

    # G4 (part 1): fail loud if too many VMs didn't reach ACTIVE
    total_specs = len(vm_specs)
    if len(active_specs) < total_specs * 0.9:
        output.error(f"  FAIL: Only {len(active_specs)}/{total_specs} VMs reached ACTIVE "
                     f"(threshold 90%). Check OpenStack quota/network/image.")
        return None

    # Get IPs (only for ACTIVE VMs)
    vms_with_ips = []
    dropped_for_no_ip = []
    for spec in active_specs:
        result = _openstack_cmd(
            rc_file,
            "server", "show", spec["name"],
            "-f", "value", "-c", "addresses",
        )
        ip = ""
        if result.returncode == 0:
            # Parse "ext_net=10.x.x.x" format
            for part in result.stdout.strip().split(","):
                part = part.strip()
                if "=" in part:
                    ip = part.split("=", 1)[1].strip()
                    break
                # Fallback: just grab an IP-looking string
                import re
                ip_match = re.search(r"(\d+\.\d+\.\d+\.\d+)", part)
                if ip_match:
                    ip = ip_match.group(1)
                    break

        if ip:
            output.info(f"  {spec['name']} => {ip}")
            vms_with_ips.append({**spec, "ip": ip})
        else:
            output.error(f"  {spec['name']} => NO IP (server show rc={result.returncode})")
            dropped_for_no_ip.append(spec["name"])

    # G4 (part 2): fail loud if we silently dropped any VMs due to IP extraction
    # failures. Previously a VM that reached ACTIVE but had no extractable IP
    # was silently omitted from the inventory, reducing the intended deploy.
    if dropped_for_no_ip:
        output.error(f"  FAIL: {len(dropped_for_no_ip)} ACTIVE VMs had no extractable IP: "
                     f"{', '.join(dropped_for_no_ip)}")
        output.error(f"  Check OpenStack network attachment / 'openstack server show -c addresses'.")
        return None

    if not vms_with_ips:
        output.error("  FAIL: No VMs have IPs — cannot proceed.")
        return None

    # Split into API and clients
    api_vm = None
    client_vms = []
    for vm in vms_with_ips:
        if vm["role"] == "api":
            api_vm = vm
        else:
            client_vms.append(vm)

    if not api_vm:
        output.error("  API VM not found or has no IP")
        return None

    return {"api": api_vm, "clients": client_vms}


def _openstack_cmd(rc_file: Path, *args: str) -> subprocess.CompletedProcess:
    """Run an OpenStack CLI command with sourced credentials."""
    import shlex
    cmd = f"source {shlex.quote(str(rc_file))} && openstack {shlex.join(args)}"
    return subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
    )


# --- Inventory & SSH Config ---

def _write_inventory(
    api_vm: dict,
    client_vms: list[dict],
    run_dir: Path,
    deployment_name: str,
    timeline_mapping: dict[str, Path] | None = None,
) -> None:
    """Write Ansible inventory with two host groups.

    If timeline_mapping is provided, each client VM line gets a
    per-host ghosts_timeline_file variable pointing at its tuned
    PHASE timeline (matches the pre-existing per-host ghosts_api_ip
    pattern, consumed by install-ghosts-clients.yaml).
    """
    lines = [
        f"# Auto-generated inventory for {deployment_name} (GHOSTS)",
        f"# Generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        "",
        "[ghosts_api]",
        f"{api_vm['name']} ansible_host={api_vm['ip']}",
        "",
        "[ghosts_clients]",
    ]

    api_ip = api_vm["ip"]
    mapping = timeline_mapping or {}
    for vm in client_vms:
        vm_name = vm["name"]
        line = f"{vm_name} ansible_host={vm['ip']} ghosts_api_ip={api_ip}"
        if vm_name in mapping:
            line += f" ghosts_timeline_file={mapping[vm_name]}"
        lines.append(line)

    lines.extend([
        "",
        "[all:vars]",
        "ansible_user=ubuntu",
        "ansible_python_interpreter=/usr/bin/python3",
        "ansible_ssh_common_args=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        "",
    ])

    (run_dir / "inventory.ini").write_text("\n".join(lines))


def _write_ssh_config(all_vms: list[dict], run_dir: Path, deployment_name: str) -> None:
    """Write SSH config snippet."""
    lines = [
        f"############# GHOSTS - {deployment_name} #############",
        "",
        "Host g-*",
        "    User ubuntu",
        "    PreferredAuthentications publickey",
        "    IdentityFile ~/.ssh/id_ed25519",
        "    IdentitiesOnly yes",
        "    StrictHostKeyChecking no",
        "    UserKnownHostsFile /dev/null",
        "    ServerAliveInterval 120",
        "",
    ]

    for vm in all_vms:
        lines.append(f"Host {vm['name']}")
        lines.append(f"    HostName {vm['ip']}")
        lines.append("")

    lines.append("#############################################")

    (run_dir / "ssh_config_snippet.txt").write_text("\n".join(lines))


def _find_ghosts_config(config_name: str | None, deploy_dir: Path) -> Path | None:
    """Find GHOSTS config directory."""
    if config_name:
        d = deploy_dir / config_name
        if (d / "config.yaml").exists():
            return d
        return None

    # Auto-detect first ghosts config
    for d in sorted(deploy_dir.iterdir()):
        if d.is_dir() and (d / "config.yaml").exists():
            try:
                cfg = DeploymentConfig.load(d / "config.yaml")
                if cfg.is_ghosts():
                    return d
            except Exception:
                continue
    return None


def _make_dep_id(deployment_name: str, run_id: str) -> str:
    """Build dep_id from deployment name + run_id."""
    dep = deployment_name
    for prefix in ("decoy-", "ghosts-", "rampart-", "enterprise-"):
        if dep.startswith(prefix):
            dep = dep[len(prefix):]
    dep = dep.replace("-", "")
    return f"{dep}{run_id}"


def _ghosts_topology(cfg: DeploymentConfig) -> tuple:
    """Comparable signature of a GHOSTS deploy's VM topology + install target.

    Two runs under the same config_name are "the same logical deploy" iff
    these match. config_name already pins preset+dataset+scope, so this just
    catches a hand-edited ghosts block (e.g. client_count bumped, repo/branch
    changed) — in which case the prior run is left intact.
    """
    return (
        cfg.ghosts_client_count(),
        cfg.ghosts_api_flavor(),
        cfg.ghosts_client_flavor(),
        cfg.ghosts_repo(),
        cfg.ghosts_branch(),
    )


def _teardown_matching_prior_runs(
    os_client: OpenStack,
    config_dir: Path,
    new_run_dir: Path,
    new_config: DeploymentConfig,
) -> None:
    """Teardown VMs from any prior run of this config_name whose ghosts
    topology matches the new config.

    Two checks per prior run (both must match to count as "same deploy"):
      1. prior run_dir/config.yaml exists and parses (= got past snapshot)
      2. prior ghosts topology == new ghosts topology (_ghosts_topology)

    On match: delete every VM under the prior run's g- prefix, wait until zero,
    close that exact PHASE run, then remove the prior local run directory.

    On mismatch (ghosts block hand-edited): leave the prior run fully intact;
    the operator clearly meant something different by reusing the name and
    can clean it up with explicit ./teardown.
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

    from ..core.teardown_steps import safe_rmtree, wait_until_zero

    new_topo = _ghosts_topology(new_config)
    to_teardown: list[tuple[Path, str]] = []  # (prior_run_dir, vm_prefix)
    for prior in prior_run_dirs:
        try:
            prior_cfg = DeploymentConfig.load(prior / "config.yaml")
        except Exception as e:
            output.dim(f"  skipping prior run {prior.name}: can't parse config.yaml ({e})")
            continue
        if _ghosts_topology(prior_cfg) != new_topo:
            output.dim(
                f"  prior run {prior.name} has a different ghosts topology "
                f"(hand-edited) — leaving alone"
            )
            continue
        prior_dep_id = _make_dep_id(new_config.deployment_name, prior.name)
        prior_prefix = make_ghosts_vm_prefix(prior_dep_id)
        to_teardown.append((prior, prior_prefix))

    if not to_teardown:
        return

    output.info("")
    output.info(f"--- Refreshing {len(to_teardown)} matching prior run(s) ---")
    for prior_dir, prior_prefix in to_teardown:
        servers = os_client.server_list_with_ids(prefix=prior_prefix)
        if servers:
            output.info(f"  Deleting {len(servers)} VM(s) under {prior_prefix}*")
            os_client.server_delete_many([s["id"] for s in servers], wait=True)
            remaining = wait_until_zero(os_client, prior_prefix)
            if remaining:
                output.error(
                    f"  ERROR: {remaining} VM(s) under {prior_prefix}* still alive "
                    f"after teardown wait. Aborting before provisioning new VMs "
                    f"(avoid mixing old + new state)."
                )
                raise SystemExit(1)
        else:
            output.dim(f"  no live VMs under {prior_prefix}* — just dropping run_dir")
        try:
            close_deployment(
                new_config.deployment_name,
                prior_dir.name,
                ended_at=datetime.now(timezone.utc),
            )
        except PhaseRunRegistryError as exc:
            output.error(f"  ERROR: cannot close prior PHASE run: {exc}")
            raise SystemExit(1) from exc
        safe_rmtree(prior_dir)
        output.dim(f"  dropped prior run_dir {prior_dir.name}")


def _build_npc_timeline_mapping(
    source_path: Path,
    client_vms: list[dict],
    run_dir: Path,
) -> dict[str, Path]:
    """Route PHASE Stage 2 per-NPC timelines to client VMs.

    PHASE now writes one tuned timeline.json per NPC at
    behavior_source/npc-{N}/timeline.json. Each timeline has per-VM
    tuning (different DelayAfter values, handler mixes, lognormal sigmas)
    — the whole point of Stage 1 was to unblock this per-VM signal that
    was previously being averaged into a single shared timeline.

    This function:
      1. Walks source_path/npc-*/timeline.json to discover available
         per-NPC timelines.
      2. For each client VM, extracts the trailing npc-N from the VM
         name (e.g. g-14a6d-npc-2 -> npc-2) and looks up the
         corresponding PHASE timeline.
      3. Copies each matched timeline to run_dir/timelines/{vm_name}.json
         so the run_dir is self-contained for teardown and audit.
      4. Returns {vm_name: Path} mapping that the inventory writer
         uses to inject per-host ghosts_timeline_file variables.

    VMs whose name doesn't contain npc-N are skipped with a warning
    (they're either the API VM, which doesn't run install-ghosts-clients,
    or a topology mismatch).
    """
    import re
    import shutil

    # Discover per-NPC timeline files keyed by NPC id (e.g. "npc-0")
    npc_timelines: dict[str, Path] = {}
    for timeline_path in sorted(source_path.glob("npc-*/timeline.json")):
        npc_id = timeline_path.parent.name  # e.g. "npc-0"
        npc_timelines[npc_id] = timeline_path

    if not npc_timelines:
        return {}

    # Lineage assert (PHASE 2026-06): feedback sources live under a
    # {preset}_v{version} namespace dir, so source_path.parent.name IS the
    # expected lineage. Catch a --preset pointed at a source stamped for a
    # different lineage. Reads the SOURCE (mount) timeline — the on-VM .NET
    # client strips _phase_metadata, but the source copy still carries it. Only
    # namespaced feedback sources look like "{preset}_v{version}".
    expected_ns = source_path.parent.name
    if "_v" in expected_ns:
        import json
        for npc_id, tl in npc_timelines.items():
            try:
                pm = (json.loads(tl.read_text()) or {}).get("_phase_metadata") or {}
            except (OSError, ValueError):
                continue
            mp, mv = pm.get("model_preset"), pm.get("model_version")
            stamped = f"{mp}_v{mv}" if mp and mv else None
            if stamped and stamped != expected_ns:
                output.error(
                    f"  FAIL: lineage mismatch in {tl} — stamped {stamped!r} != "
                    f"deployed namespace {expected_ns!r}. --preset points at a "
                    f"source generated for a different lineage.")
                raise RuntimeError(f"lineage mismatch: {stamped} != {expected_ns}")

    # Stage per-host copies under run_dir/timelines/ for a self-contained run
    timelines_dir = run_dir / "timelines"
    timelines_dir.mkdir(parents=True, exist_ok=True)

    mapping: dict[str, Path] = {}
    unmatched_vms: list[str] = []   # VM name doesn't contain npc-N
    missing_timelines: list[str] = []  # no PHASE timeline for matching npc-N

    for vm in client_vms:
        vm_name = vm["name"]
        # Extract trailing npc-N from VM name (e.g. g-14a6d-npc-0 -> npc-0)
        match = re.search(r"(npc-\d+)$", vm_name)
        if not match:
            unmatched_vms.append(vm_name)
            continue
        npc_id = match.group(1)

        source_timeline = npc_timelines.get(npc_id)
        if source_timeline is None:
            missing_timelines.append(f"{vm_name} ({npc_id})")
            continue

        dest = timelines_dir / f"{vm_name}.json"
        shutil.copy2(source_timeline, dest)
        mapping[vm_name] = dest

    # G6: fail loud if any VMs didn't get routed a timeline. Previously these
    # were logged as WARNINGs and the VM silently fell back to the default
    # timeline, losing PHASE per-NPC tuning. Caller already fails loud when the
    # whole mapping is empty (behavior_source with no npc-* dirs) — this
    # extends that discipline to partial coverage.
    if unmatched_vms:
        output.error(
            f"  FAIL: {len(unmatched_vms)} client VMs don't match npc-N naming "
            f"convention: {', '.join(unmatched_vms)}. Deploy topology/naming mismatch."
        )
        raise RuntimeError(f"unmatched VMs: {unmatched_vms}")
    if missing_timelines:
        available = sorted(npc_timelines.keys())
        output.error(
            f"  FAIL: {len(missing_timelines)} VMs have no PHASE timeline: "
            f"{', '.join(missing_timelines)}. Available in source: {available}. "
            f"PHASE feedback generator output is incomplete for this dataset."
        )
        raise RuntimeError(f"missing timelines: {missing_timelines}")

    return mapping
