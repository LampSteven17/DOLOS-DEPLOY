"""RAMPART enterprise deployment command."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..core import output
from ..core.ansible_runner import AnsibleRunner, default_event_handler
from ..core.config import DeploymentConfig
from ..core.ssh_config import install_ssh_config
from ..core.vm_naming import make_ent_vm_prefix
from ..core.deploy_steps import (
    infrastructure_vms_from_ssh_config,
    register_phase_run,
)
from ..core.phase_run_registry import run_id_from_started_at, utc_deployment_start
from ..core import run_status


def run_rampart_spinup(
    config_name: str | None,
    deploy_dir: Path,
    behavior_source: str | None = None,
    configs_spec: str | None = None,
) -> int:
    """Deploy RAMPART enterprise network."""
    config_name = config_name or "rampart-controls"

    # Feedback args + rampart-controls base → derive a feedback config dir.
    # Skip when behavior_source is None — that's the controls path
    # (rampart-controls/config.yaml's own behavior_source is picked up below
    # as effective_source). Keeps controls deploys named rampart-controls
    # instead of getting a verbose derived dir.
    if behavior_source and config_name == "rampart-controls":
        from ..core.feedback import generate_rampart_feedback_config
        config_name = generate_rampart_feedback_config(
            Path(behavior_source), configs_spec or "all", deploy_dir,
        )

    config_dir = _find_rampart_config(config_name, deploy_dir)
    if not config_dir:
        output.error("ERROR: No RAMPART deployment config found")
        return 1

    deployment = config_dir.name
    config = DeploymentConfig.load(config_dir / "config.yaml")
    wdir = config.enterprise_workflow_dir()

    # Source dispatch: explicit CLI behavior_source (feedback dataset) wins;
    # else fall back to the deployment's own config.yaml (controls path).
    effective_source = behavior_source or config.behavior_source

    started_at = utc_deployment_start()
    run_id = run_id_from_started_at(started_at)
    run_dir = config_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Stamp FAILED up front; flipped to OK only at the final return below.
    # Any early return / exception / kill leaves this run marked failed,
    # which is what `./teardown --failed` targets. See core/run_status.py.
    run_status.write_run_status(run_dir, run_status.FAILED, "in_progress")

    ent_log = run_dir / "enterprise.log"

    # Build hash-based VM prefix (5-char MD5 for NetBIOS limit)
    dep_id = _make_dep_id(deployment, run_id)
    ent_prefix = make_ent_vm_prefix(dep_id)
    ent_hash = ent_prefix[2:-1]  # strip 'r-' / '-' for downstream consumers that need just the hash

    # Header
    output.banner(f"DEPLOY: RAMPART ({deployment})")
    output.info(f"  Workflow:  {wdir}")
    output.info(f"  Run ID:    {run_id}")
    output.info(f"  VM prefix: {ent_prefix}*")
    if effective_source:
        output.info(f"  Source:    {effective_source}")
    output.info("")

    # Snapshot config
    shutil.copy2(config_dir / "config.yaml", run_dir / "config.yaml")

    # Create prefixed enterprise config + per-deployment cloud config
    _create_prefixed_config(config, ent_prefix, wdir, run_dir)
    enterprise_url = _create_prefixed_cloud_config(config, ent_hash, wdir, run_dir)
    output.info(f"  DNS zone:  {enterprise_url}")

    # Save zone name for scoped teardown
    (run_dir / "dns_zone.txt").write_text(enterprise_url)

    rc_file = Path.home() / "vxn3kr-bot-rc"

    # Step 1: venv
    output.info("[1/5] Setting up environment...")
    if not _ensure_venv(wdir):
        output.error("FAIL: could not set up venv")
        return 1
    output.info("  venv ready")

    # Step 2: Provision VMs
    output.info("[2/5] Provisioning VMs (deploy-nodes.py)...")
    ok = _ent_run(
        wdir, rc_file,
        ["python3", "deploy-nodes.py",
         "-c", str(run_dir / "cloud-config-prefixed.json"),
         "-e", str(run_dir / "enterprise-config-prefixed.json")],
        log_file=ent_log,
    )
    if not ok:
        output.error("FAIL: VM provisioning failed")
        return 1

    # Copy deploy output + generate SSH config
    _copy_if_exists(wdir / "deploy-output.json", run_dir / "deploy-output.json")
    deploy_output = run_dir / "deploy-output.json"
    if deploy_output.exists():
        _generate_ssh_config(deploy_output, deployment, run_id, run_dir, deploy_dir)

    # Step 3: Configure VMs
    output.info("[3/5] Configuring VMs (post-deploy.py)...")
    ok = _ent_run(
        wdir, rc_file,
        ["python3", "post-deploy.py", "deploy-output.json"],
        log_file=ent_log,
    )
    if not ok:
        output.error("FAIL: VM configuration failed")
        return 1

    # Generate PHASE-informed user roles if feedback source provided
    user_roles_file = config.enterprise_user_roles()
    enterprise_config_file = config.enterprise_config_file()

    if effective_source:
        output.info("  Generating PHASE-informed user roles...")
        feedback_result = _generate_feedback_user_roles(
            behavior_source=Path(effective_source),
            baseline_user_roles=wdir / config.enterprise_user_roles(),
            enterprise_config=run_dir / "enterprise-config-prefixed.json",
            output_dir=run_dir,
        )
        if feedback_result:
            # Use absolute paths (simulate-logins runs from wdir, not run_dir)
            user_roles_file = str(feedback_result["user_roles_path"])
            enterprise_config_file = str(feedback_result["enterprise_config_path"])
            output.info(f"  Generated {feedback_result['role_count']} per-node roles")
        else:
            output.info("  WARNING: Could not generate feedback roles, using baseline")

    # Step 4: Generate login schedule
    output.info("[4/5] Generating login schedule (simulate-logins.py)...")
    ok = _ent_run(
        wdir, rc_file,
        ["python3", "simulate-logins.py",
         user_roles_file,
         enterprise_config_file,
         "post-deploy-output.json"],
        log_file=ent_log,
    )
    if not ok:
        output.error("FAIL: Login schedule generation failed")
        return 1

    # Copy outputs before step 5 (needed for inventory generation)
    _copy_if_exists(wdir / "post-deploy-output.json", run_dir / "post-deploy-output.json")
    _copy_if_exists(wdir / "logins.json", run_dir / "logins.json")
    (run_dir / "deployment_type").write_text("rampart")

    # Step 5: Deploy autonomous emulation services on endpoint VMs
    output.info("[5/5] Deploying emulation services...")
    emulation_inventory = run_dir / "rampart-emulation-inventory.ini"
    endpoint_count = _generate_emulation_inventory(
        run_dir, ent_prefix, emulation_inventory,
    )

    if endpoint_count > 0 and emulation_inventory.exists():
        # Linux endpoints: Ansible playbook (systemd service)
        runner = AnsibleRunner(deploy_dir / "logs")
        emu_result = runner.run_playbook(
            "rampart/install-rampart-emulation.yaml",
            emulation_inventory,
            on_event=default_event_handler,
        )
        if emu_result.rc != 0:
            output.error(f"  FAIL: Linux emulation playbook rc={emu_result.rc}")
            output.info(f"  Log: {emu_result.log_path}")
            return 1

        # Windows endpoints: direct SSH (Ansible mangles PowerShell $ variables)
        win_ok, win_total = _deploy_windows_emulation(
            run_dir, ent_prefix,
            chrome_reaper=deployment.startswith("rampart-feedback"),
        )
        if win_total > 0:
            # C2: Fail deploy if too many Windows endpoints didn't deploy.
            # Threshold is 90% — below that the deploy is not usable for
            # experiments. Previously failures were silently logged as warnings
            # and the deploy reported "DONE" despite 100% Windows failure rate.
            win_threshold_pct = 90
            actual_pct = 100.0 * win_ok / win_total
            if actual_pct < win_threshold_pct:
                output.error(
                    f"  FAIL: Windows emulation below {win_threshold_pct}% threshold "
                    f"({win_ok}/{win_total} = {actual_pct:.0f}%)"
                )
                output.error(
                    f"  Deploy aborted — see failure breakdown above. "
                    f"Check domain join, admin password, WinRM/SSH connectivity."
                )
                return 1
            elif win_ok < win_total:
                output.info(
                    f"  WARNING: Windows emulation partial success: {win_ok}/{win_total} "
                    f"(threshold {win_threshold_pct}% met)"
                )
            else:
                output.info(f"  Windows emulation started on all {win_ok} VMs")
    else:
        # Shouldn't happen in normal deploys — if emulation_inventory is empty,
        # something went wrong upstream. Fail loud rather than "skipping".
        output.error("  FAIL: No endpoint VMs with users found — check simulate-logins.py output")
        return 1

    # P1: PHASE registration is fail-loud and uses the final actual VM roster.
    snippet_path = run_dir / "ssh_config_snippet.txt"
    phase_vms = infrastructure_vms_from_ssh_config(snippet_path)
    if not register_phase_run(config, "rampart", started_at, phase_vms):
        output.error("")
        output.error("ABORTING: PHASE deployment registration failed.")
        output.error("RAMPART VMs are running but this fleet has no PHASE run record.")
        return 1

    run_status.write_run_status(run_dir, run_status.OK, "deploy complete")
    output.info("")
    output.info(f"DONE: RAMPART deployment {deployment}/{run_id}")
    output.info(f"  {endpoint_count} endpoints running autonomous emulation")
    output.info(f"  Service: rampart-human (systemd on Linux, scheduled task on Windows)")
    output.info(f"  Check:   ssh {ent_prefix}<node> \"systemctl status rampart-human\"")
    output.info(f"  Log:     {ent_log}")
    return 0


# --- Helpers ---

def _find_rampart_config(config_name: str | None, deploy_dir: Path) -> Path | None:
    """Find RAMPART config directory."""
    if config_name:
        d = deploy_dir / config_name
        if (d / "config.yaml").exists():
            return d
        return None

    # Auto-detect first rampart config
    for d in sorted(deploy_dir.iterdir()):
        if d.is_dir() and (d / "config.yaml").exists():
            try:
                cfg = DeploymentConfig.load(d / "config.yaml")
                if cfg.is_rampart():
                    return d
            except Exception:
                continue
    return None


def _make_dep_id(deployment_name: str, run_id: str) -> str:
    dep = deployment_name
    for prefix in ("decoy-", "ghosts-", "rampart-", "enterprise-"):
        if dep.startswith(prefix):
            dep = dep[len(prefix):]
    dep = dep.replace("-", "")
    return f"{dep}{run_id}"


def _ensure_venv(wdir: Path) -> bool:
    """Ensure venv exists in workflow directory."""
    venv_activate = wdir / ".venv" / "bin" / "activate"
    if venv_activate.exists():
        return True

    try:
        subprocess.run(
            ["python3", "-m", "venv", str(wdir / ".venv")],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(wdir / ".venv" / "bin" / "pip"), "install", "-r", str(wdir / "requirements.txt")],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


# Lines matching any of these patterns are suppressed from terminal output
# but still written to the log file.
_NOISE_PATTERNS = [
    "is deprecated in favor of",
    "DeprecationWarning",
    "InsecureRequestWarning",
    "CryptographyDeprecationWarning",
    "warnings.warn(",
]


def _is_noise(line: str) -> bool:
    """Return True if a line should be suppressed from terminal output."""
    if not line.strip():
        return False
    return any(pat in line for pat in _NOISE_PATTERNS)


def _ent_run(
    wdir: Path,
    rc_file: Path,
    cmd: list[str],
    log_file: Path | None = None,
) -> bool:
    """Run a command in the enterprise venv with OpenStack credentials.
    Streams filtered output to terminal, full output to log file."""
    shell_cmd = (
        f"cd {wdir} && source .venv/bin/activate && source {rc_file} && "
        + " ".join(cmd)
    )

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONWARNINGS"] = "ignore"

    try:
        proc = subprocess.Popen(
            ["bash", "-c", shell_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )

        log_fh = open(log_file, "a") if log_file else None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if log_fh:
                    log_fh.write(line)
                    log_fh.flush()
                if not _is_noise(line):
                    output.info(f"    {line.rstrip()}")
        finally:
            if log_fh:
                log_fh.close()

        proc.wait()
        return proc.returncode == 0

    except Exception as e:
        output.info(f"  Command failed: {e}")
        return False


def _create_prefixed_config(
    config: DeploymentConfig, ent_prefix: str, wdir: Path, run_dir: Path,
) -> None:
    """Create enterprise config with hash-based VM name prefix."""
    ent_config_path = wdir / config.enterprise_config_file()
    if not ent_config_path.exists():
        return

    data = json.loads(ent_config_path.read_text())
    for node in data.get("nodes", data.get("servers", [])):
        if "name" in node:
            node["name"] = ent_prefix + node["name"]

    with open(run_dir / "enterprise-config-prefixed.json", "w") as f:
        json.dump(data, f, indent=2)


def _create_prefixed_cloud_config(
    config: DeploymentConfig, ent_hash: str, wdir: Path, run_dir: Path,
) -> str:
    """Create cloud config with per-deployment enterprise_url for DNS isolation.

    Removes any stale enterprise_url from the original config and injects a
    per-deployment URL derived from the OpenStack project name + hash.

    Returns the enterprise_url that will be used for DNS zone creation.
    """
    cloud_config_path = wdir / config.enterprise_cloud_config()
    if not cloud_config_path.exists():
        return f"{ent_hash}.os"

    data = json.loads(cloud_config_path.read_text())

    # Derive base URL from OpenStack project name (always authoritative)
    project_name = os.environ.get("OS_PROJECT_NAME", "")
    if not project_name:
        # Try sourcing the RC file referenced in cloud config
        rc_file = data.get("os_env_file", "")
        if rc_file:
            project_name = "vxn3kr-bot-project"  # known default
    base_url = f"{project_name.lower()}.os" if project_name else "openstack.os"

    enterprise_url = f"{ent_hash}.{base_url}"
    data["enterprise_url"] = enterprise_url

    with open(run_dir / "cloud-config-prefixed.json", "w") as f:
        json.dump(data, f, indent=2)

    return enterprise_url


def _generate_ssh_config(
    deploy_output: Path, deployment: str, run_id: str, run_dir: Path, deploy_dir: Path,
) -> None:
    """Generate and install SSH config from enterprise deploy output.

    The snippet is also the final RAMPART VM roster source for PHASE
    registration, so a missing snippet fails registration loudly.
    """
    from ..core.enterprise_ssh_config import generate_ssh_config

    snippet_path = run_dir / "ssh_config_snippet.txt"
    try:
        with open(deploy_output) as f:
            data = json.load(f)
        snippet = generate_ssh_config(data, deployment, run_id)
        snippet_path.write_text(snippet + "\n")
        install_ssh_config(snippet_path, f"{deployment}/{run_id}")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        # Don't fail the deploy — operators can still SSH by IP — but warn
        # loudly so they know SSH config block isn't installed AND PHASE
        # registration will be skipped downstream.
        output.error(f"  WARNING: SSH config generation failed: {e}")
        output.error(f"  You'll need to SSH by IP for this deployment.")


def _start_emulation(
    wdir: Path, rc_file: Path, config: DeploymentConfig, run_dir: Path,
) -> int | None:
    """Start emulation in background, save PID."""
    shell_cmd = (
        f"cd {wdir} && source .venv/bin/activate && source {rc_file} && "
        f"nohup python3 emulate-logins.py "
        f"post-deploy-output.json logins.json "
        f"--seed {config.emulate_seed()} "
        f"--logfile enterprise.ndjson "
        f"> emulate.log 2>&1 & echo $!"
    )

    try:
        result = subprocess.run(
            ["bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            pid = int(result.stdout.strip())
            (run_dir / "emulate.pid").write_text(str(pid))
            return pid
    except (ValueError, subprocess.CalledProcessError):
        pass
    return None



def _generate_emulation_inventory(
    run_dir: Path, ent_prefix: str, inventory_path: Path,
) -> int:
    """Generate Ansible inventory for emulation service deployment.

    Reads logins.json (user credentials) and post-deploy-output.json (VM IPs)
    to produce an inventory with per-host variables for the emulation playbook.

    Returns the number of endpoint VMs with assigned users.
    """
    logins_path = run_dir / "logins.json"
    post_deploy_path = run_dir / "post-deploy-output.json"

    if not logins_path.exists() or not post_deploy_path.exists():
        output.info("  Missing logins.json or post-deploy-output.json")
        return 0

    logins_data = json.loads(logins_path.read_text())
    post_deploy = json.loads(post_deploy_path.read_text())

    # Build user map: bare node name → user info
    user_map = {}
    for user in logins_data.get("users", []):
        home_node = user["home_node"]["name"]
        # Strip r-{hash}- prefix if present (enterprise config uses prefixed names)
        if home_node.startswith(ent_prefix):
            home_node = home_node[len(ent_prefix):]
        lp = user["login_profile"]

        # PHASE hour-of-day fields (UTC-indexed). Pulled verbatim from
        # per-node user-roles.json. PHASE pre-projects any window logic onto
        # day_start_hour_* + activity_daily_*_hours; RAMPART consumes those
        # fields directly.
        #
        # Emit as Python list literal (e.g. "[2,4,4,4,4,4,2]") not CSV: Ansible's
        # INI inventory parser runs ast.literal_eval on values, and a quoted CSV
        # like "2,4,4,4,4,4,2" gets coerced to a tuple, which Jinja then renders
        # as "[2, 4, 4, ...]" — pyhuman then sees space-split tokens like "[2"
        # and crashes. List-literal form round-trips cleanly through literal_eval
        # and is converted back to CSV by `| join(",")` in the playbook.
        def _list_literal(v):
            if isinstance(v, list) and v:
                return "[" + ",".join(str(int(x)) for x in v) + "]"
            return "[]"

        user_map[home_node] = {
            "username": user["user_profile"]["username"],
            "password": user["user_profile"]["password"],
            "domain": user["domain"],
            "workflows": " ".join(lp["workflows"]),
            "clustersize": lp.get("clustersize", "5"),
            "clustersize_sigma": lp.get("clustersize_sigma", "0"),
            "taskinterval": lp.get("taskinterval", "10"),
            "taskinterval_sigma": lp.get("taskinterval_sigma", "0"),
            "taskgroupinterval": lp.get("taskgroupinterval", "500"),
            "day_start_hour_min": int(lp.get("day_start_hour_min", 0)),
            "day_start_hour_max": int(lp.get("day_start_hour_max", 0)),
            "activity_daily_min_hours": _list_literal(lp.get("activity_daily_min_hours")),
            "activity_daily_max_hours": _list_literal(lp.get("activity_daily_max_hours")),
        }

    # Extract domain admin password (needed for Windows SSH)
    domain_leaders = (
        post_deploy
        .get("enterprise_built", {})
        .get("setup", {})
        .get("setup_domains", {})
        .get("domain_leaders", {})
    )
    # Use the first domain's admin password + build FQDN for auth
    domain_admin_pass = ""
    enterprise_url = post_deploy.get("backend_config", {}).get("enterprise_url", "")
    domain_fqdn = ""
    for domain, info in domain_leaders.items():
        domain_admin_pass = info.get("admin_pass", "")
        domain_fqdn = f"{domain}.{enterprise_url}" if enterprise_url else domain
        break

    # Build node map: bare name → {ip, is_windows}
    node_map = {}
    nodes = post_deploy.get("enterprise_built", {}).get("deployed", {}).get("nodes", [])
    for node in nodes:
        prefixed_name = node["name"]
        # Strip the r-{hash}- prefix to get bare name
        bare_name = prefixed_name
        if bare_name.startswith(ent_prefix):
            bare_name = bare_name[len(ent_prefix):]

        ip = node["addresses"][0]["addr"] if node.get("addresses") else None
        roles = node.get("enterprise_description", {}).get("roles", [])
        is_windows = "windows" in roles
        is_endpoint = "endpoint" in roles

        if ip and is_endpoint:
            node_map[bare_name] = {
                "prefixed_name": prefixed_name,
                "ip": ip,
                "is_windows": is_windows,
            }

    # Generate inventory
    seed = logins_data.get("seed", 42)
    linux_lines = []
    windows_lines = []
    count = 0

    for bare_name, node_info in sorted(node_map.items()):
        if bare_name not in user_map:
            continue

        user = user_map[bare_name]
        vm_seed = seed + count
        count += 1

        host_line = (
            f"{node_info['prefixed_name']} "
            f"ansible_host={node_info['ip']} "
            f"rampart_username={user['username']} "
            f"rampart_password={user['password']} "
            f"rampart_domain={user['domain']} "
            f"rampart_workflows=\"{user['workflows']}\" "
            f"rampart_seed={vm_seed} "
            f"rampart_clustersize={user['clustersize']} "
            f"rampart_clustersize_sigma={user['clustersize_sigma']} "
            f"rampart_taskinterval={user['taskinterval']} "
            f"rampart_taskinterval_sigma={user['taskinterval_sigma']} "
            f"rampart_taskgroupinterval={user['taskgroupinterval']} "
            f"rampart_day_start_hour_min={user['day_start_hour_min']} "
            f"rampart_day_start_hour_max={user['day_start_hour_max']} "
            f"rampart_activity_daily_min_hours={user['activity_daily_min_hours']} "
            f"rampart_activity_daily_max_hours={user['activity_daily_max_hours']}"
        )

        if node_info["is_windows"]:
            windows_lines.append(host_line)
        else:
            linux_lines.append(host_line)

    lines = [
        f"# Auto-generated Rampart emulation inventory",
        f"# Generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        "",
    ]

    lines.append("[rampart_linux]")
    lines.extend(linux_lines)
    lines.append("")

    lines.append("[rampart_windows]")
    lines.extend(windows_lines)
    lines.append("")

    lines.extend([
        "[rampart_linux:vars]",
        "ansible_user=ubuntu",
        "ansible_python_interpreter=/usr/bin/python3",
        "ansible_ssh_private_key_file=~/.ssh/id_rsa",
        "ansible_ssh_common_args=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes",
        "",
        "[rampart_windows:vars]",
        f"ansible_user=Administrator@{domain_fqdn}" if domain_fqdn else "ansible_user=Administrator",
        f"ansible_ssh_pass={domain_admin_pass}" if domain_admin_pass else "",
        "ansible_ssh_common_args=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password",
        "",
    ])

    inventory_path.write_text("\n".join(lines))
    output.info(f"  {count} endpoints ({len(linux_lines)} linux, {len(windows_lines)} windows)")
    return count


def _deploy_windows_emulation(
    run_dir: Path, ent_prefix: str, chrome_reaper: bool = False,
) -> tuple[int, int]:
    """Deploy emulation on Windows endpoints via direct SSH.

    Uses sshpass for password auth — Ansible raw module can't handle
    PowerShell $ variables without mangling them.

    chrome_reaper=True (feedback deploys only — controls stay pristine
    baseline) additionally registers an hourly RampartChromeReaper task
    that kills chrome.exe processes older than 2 hours. pyhuman browse
    workflows on Windows orphan their browsers on exception paths;
    orphans accumulate over days until the VM wedges (sshd can't fork,
    CLR can't start). The 2h age gate means a live workflow's browser
    (minutes-old) is never touched.

    Returns (ok_count, total_count).
    """
    import concurrent.futures

    logins_data = json.loads((run_dir / "logins.json").read_text())
    post_deploy = json.loads((run_dir / "post-deploy-output.json").read_text())

    # Get domain admin password + FQDN for auth
    domain_leaders = (
        post_deploy.get("enterprise_built", {})
        .get("setup", {}).get("setup_domains", {}).get("domain_leaders", {})
    )
    enterprise_url = post_deploy.get("backend_config", {}).get("enterprise_url", "")
    domain_name = ""
    admin_pass = ""
    for d, info in domain_leaders.items():
        domain_name = f"{d}.{enterprise_url}" if enterprise_url else d
        admin_pass = info.get("admin_pass", "")
        break

    if not admin_pass:
        return 0, 0

    # Build per-VM configs (strip prefix from node names in logins.json)
    user_map = {}
    for user in logins_data.get("users", []):
        node_name = user["home_node"]["name"]
        if node_name.startswith(ent_prefix):
            node_name = node_name[len(ent_prefix):]
        user_map[node_name] = user

    nodes = post_deploy.get("enterprise_built", {}).get("deployed", {}).get("nodes", [])
    seed = logins_data.get("seed", 42)

    win_vms = []
    idx = 0
    for node in nodes:
        prefixed = node["name"]
        bare = prefixed.removeprefix(ent_prefix)
        roles = node.get("enterprise_description", {}).get("roles", [])
        if "windows" not in roles or "endpoint" not in roles:
            continue
        if bare not in user_map:
            continue
        ip = node["addresses"][0]["addr"] if node.get("addresses") else None
        if not ip:
            continue
        u = user_map[bare]
        lp = u["login_profile"]

        def _csv_or_empty(v):
            if isinstance(v, list) and v:
                return ",".join(str(int(x)) for x in v)
            return ""

        win_vms.append({
            "name": prefixed,
            "ip": ip,
            "username": u["user_profile"]["username"],
            "password": u["user_profile"]["password"],
            "workflows": " ".join(lp["workflows"]),
            "seed": seed + idx,
            "clustersize": lp.get("clustersize", "5"),
            "clustersize_sigma": lp.get("clustersize_sigma", "0"),
            "taskinterval": lp.get("taskinterval", "10"),
            "taskinterval_sigma": lp.get("taskinterval_sigma", "0"),
            "taskgroupinterval": lp.get("taskgroupinterval", "500"),
            "day_start_hour_min": int(lp.get("day_start_hour_min", 0)),
            "day_start_hour_max": int(lp.get("day_start_hour_max", 0)),
            "activity_daily_min_hours": _csv_or_empty(lp.get("activity_daily_min_hours")),
            "activity_daily_max_hours": _csv_or_empty(lp.get("activity_daily_max_hours")),
        })
        idx += 1

    if not win_vms:
        return 0, 0

    ssh_base = [
        "sshpass", "-p", admin_pass,
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "PreferredAuthentications=password",
        # Disable pubkey auth: if any SSH key is offered before password,
        # Windows sshd counts it against MaxAuthTries and rejects the
        # password attempt. This is an extra safeguard — the real fix is
        # SSH_AUTH_SOCK="" in env (already set below), but that doesn't
        # stop ssh from trying explicit IdentityFiles from ~/.ssh/config.
        "-o", "PubkeyAuthentication=no",
        "-o", "ConnectTimeout=15",
        # No BatchMode=yes — sshpass needs to feed stdin, and BatchMode
        # disables that in some ssh builds.
    ]
    ssh_user = f"Administrator@{domain_name}"

    def _ssh_step(vm: dict, step_name: str, cmd: str) -> None:
        """Run one SSH command; raise with real error on nonzero or timeout.
        Silent-failure-resistant: every subprocess call checked, stderr included."""
        r = subprocess.run(
            ssh_base + [f"{ssh_user}@{vm['ip']}", cmd],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "SSH_AUTH_SOCK": ""},
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:200]
            raise RuntimeError(f"[{step_name}] rc={r.returncode}: {err}")

    def _setup_one(vm: dict) -> tuple[bool, str]:
        """Deploy emulation to one Windows VM. Returns (success, error_message)."""
        ts = time.strftime("%H:%M:%S")
        try:
            # 1) Write passfile
            _ssh_step(vm, "passfile",
                f'powershell -Command "New-Item -Path C:\\tmp -ItemType Directory -Force | Out-Null; '
                f"Set-Content -Path C:\\tmp\\shib_login.{vm['username']} "
                f"-Value '{vm['username']}`n{vm['password']}' -Encoding ASCII\""
            )

            # 2) Write run-emulation.ps1 using $l array
            script_cmd = (
                f'powershell -Command "'
                f"$l = @(); "
                f"$l += '$env:PYTHONUNBUFFERED = 1'; "
                f"$l += 'while ($true) {{'; "
                f"$l += '    try {{'; "
                f"$l += '        & C:\\Python\\python.exe -u C:\\human\\human.py "
                f"--clustersize {vm['clustersize']} --clustersize-sigma {vm['clustersize_sigma']} "
                f"--taskinterval {vm['taskinterval']} --taskinterval-sigma {vm['taskinterval_sigma']} "
                f"--taskgroupinterval {vm['taskgroupinterval']} "
                f"--seed {vm['seed']} --workflows {vm['workflows']} "
                f"--day-start-hour-min {vm['day_start_hour_min']} "
                f"--day-start-hour-max {vm['day_start_hour_max']} "
                # Wrap CSV in PowerShell single quotes so the comma list isn't
                # parsed as an array literal when run-emulation.ps1 invokes
                # human.py via `&`. The doubled '' inside the outer $l += '...'
                # collapses to a literal ' in the emitted script line.
                f"--activity-daily-min-hours ''{vm['activity_daily_min_hours']}'' "
                f"--activity-daily-max-hours ''{vm['activity_daily_max_hours']}'' "
                f"--extra passfile C:\\tmp\\shib_login.{vm['username']}'; "
                f"$l += '    }} catch {{'; "
                f"$l += '        Write-Host human.py_crashed_restarting'; "
                f"$l += '    }}'; "
                f"$l += '    Start-Sleep -Seconds 30'; "
                f"$l += '}}'; "
                f'$l | Set-Content C:\\tmp\\run-emulation.ps1 -Encoding ASCII"'
            )
            _ssh_step(vm, "script", script_cmd)

            # 3) Create + register scheduled task
            task_cmd = (
                f'powershell -ExecutionPolicy Bypass -Command "'
                f"Unregister-ScheduledTask -TaskName RampartHuman -Confirm:$false -ErrorAction SilentlyContinue; "
                f"$a = New-ScheduledTaskAction -Execute powershell.exe "
                f"-Argument '-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\\tmp\\run-emulation.ps1'; "
                f"$t = New-ScheduledTaskTrigger -AtStartup; "
                f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                f"-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1); "
                f"Register-ScheduledTask -TaskName RampartHuman -Action $a -Trigger $t -Settings $s "
                f'-User SYSTEM -RunLevel Highest -Force"'
            )
            _ssh_step(vm, "register_task", task_cmd)

            # 4) Start task
            _ssh_step(vm, "start_task",
                'powershell -Command "Start-ScheduledTask -TaskName RampartHuman"'
            )

            # 5) Feedback only: hourly reaper for orphaned chrome processes.
            # Same $l-array technique as run-emulation.ps1 — the script file
            # sidesteps ssh→cmd→powershell quote nesting for $_ etc.
            if chrome_reaper:
                reaper_script = (
                    f'powershell -Command "'
                    f"$l = @(); "
                    f"$l += 'Get-Process chrome, chromedriver -ErrorAction SilentlyContinue | "
                    f"Where-Object {{ $_.StartTime -lt (Get-Date).AddHours(-2) }} | "
                    f"Stop-Process -Force'; "
                    f'$l | Set-Content C:\\tmp\\chrome-reaper.ps1 -Encoding ASCII"'
                )
                _ssh_step(vm, "reaper_script", reaper_script)

                # -Once + RepetitionInterval (not AtStartup + loop): no
                # resident powershell.exe eating RAM on the very VMs this
                # protects. Repetition window 3650d survives reboots.
                reaper_task = (
                    f'powershell -ExecutionPolicy Bypass -Command "'
                    f"Unregister-ScheduledTask -TaskName RampartChromeReaper -Confirm:$false -ErrorAction SilentlyContinue; "
                    f"$a = New-ScheduledTaskAction -Execute powershell.exe "
                    f"-Argument '-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\\tmp\\chrome-reaper.ps1'; "
                    f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date) "
                    f"-RepetitionInterval (New-TimeSpan -Minutes 60) "
                    f"-RepetitionDuration (New-TimeSpan -Days 3650); "
                    f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
                    f"Register-ScheduledTask -TaskName RampartChromeReaper -Action $a -Trigger $t -Settings $s "
                    f'-User SYSTEM -RunLevel Highest -Force"'
                )
                _ssh_step(vm, "reaper_task", reaper_task)

            output.info(f"  [{ts}]    OK  {vm['name']} (Windows)")
            return True, ""
        except subprocess.TimeoutExpired as e:
            msg = f"timeout during {getattr(e, 'cmd', '?')} — network/firewall/auth issue"
            output.info(f"  [{ts}]    FAIL  {vm['name']}  {msg}")
            return False, msg
        except RuntimeError as e:
            msg = str(e)
            output.info(f"  [{ts}]    FAIL  {vm['name']}  {msg}")
            return False, msg
        except Exception as e:
            msg = f"unexpected {type(e).__name__}: {e}"
            output.info(f"  [{ts}]    FAIL  {vm['name']}  {msg}")
            return False, msg

    results: list[tuple[str, bool, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_setup_one, vm): vm for vm in win_vms}
        for f in concurrent.futures.as_completed(futures):
            vm = futures[f]
            success, err = f.result()
            results.append((vm["name"], success, err))

    ok = sum(1 for _, s, _ in results if s)
    total = len(results)

    # C2: Aggregate failure — if too many fail, surface a clear error
    # so the caller can abort. Also summarize common error patterns so operator
    # sees "19/19 Authentication failed" instead of 19 individual warnings.
    if ok < total:
        from collections import Counter
        err_patterns = Counter()
        for name, success, err in results:
            if not success:
                # Bucket by first 60 chars of error for pattern detection
                err_patterns[err[:60]] += 1
        output.info("")
        output.error(f"  WINDOWS EMULATION FAILURES ({total - ok}/{total}):")
        for pattern, count in err_patterns.most_common():
            output.error(f"    {count}x  {pattern}")

    return ok, total


def _generate_feedback_user_roles(
    behavior_source: Path,
    baseline_user_roles: Path,
    enterprise_config: Path,
    output_dir: Path,
) -> dict | None:
    """Build user-roles-feedback.json + enterprise-config-feedback.json from
    PHASE Stage 2 per-node files.

    PHASE now writes target-native configs: behavior_source contains
    {bare_node}/user-roles.json files, each a self-contained pyhuman config
    whose first role is the tuned {bare_node}_user role and whose remaining
    roles are baseline clones (standard/power/admin user).

    This function:
      1. Walks behavior_source/*/user-roles.json to discover processed nodes.
      2. Extracts the first (tuned) role from each per-node file.
      3. Renames each tuned role from "{bare_node}_user" to the deployment-
         prefixed form "{r-hash-prefixed_node_name}_user" so concurrent
         deployments of different hashes can coexist without role name
         collisions.
      4. Walks the enterprise config, strips the r-{hash}- prefix from each
         node name to get its bare form, looks up the corresponding tuned
         role, and rewrites the node's "user" field to point at the
         renamed role.
      5. Nodes with user: null in the enterprise config (dc1-3, linep1) are
         left unchanged — PHASE does not write feedback for them.
      6. Writes the combined user-roles-feedback.json (all tuned roles +
         the 3 baseline roles for any unfed nodes) and the rewritten
         enterprise-config-feedback.json into output_dir.
    """
    import copy
    import json
    import re

    # Load the baseline roles file — we need the standard/power/admin user
    # roles to keep in the output as fallbacks for any unfed nodes.
    try:
        baseline_data = json.loads(baseline_user_roles.read_text())
    except (OSError, json.JSONDecodeError) as e:
        output.info(f"  ERROR: failed to read baseline {baseline_user_roles}: {e}")
        return None

    baseline_role_names = {"standard user", "power user", "admin user"}
    baseline_trio = [
        r for r in baseline_data.get("roles", [])
        if r.get("name") in baseline_role_names
    ]

    # Load the enterprise config (prefixed node names)
    try:
        enterprise = json.loads(enterprise_config.read_text())
    except (OSError, json.JSONDecodeError) as e:
        output.info(f"  ERROR: failed to read enterprise config {enterprise_config}: {e}")
        return None

    # Discover per-node feedback files: behavior_source/{bare_node}/user-roles.json
    per_node_files = sorted(behavior_source.glob("*/user-roles.json"))
    if not per_node_files:
        output.info(
            f"  No per-node user-roles.json found in {behavior_source} — "
            f"expected Stage 2 layout with {{bare_node}}/user-roles.json files."
        )
        return None

    # Lineage assert (PHASE 2026-06): feedback sources live under a
    # {preset}_v{version} namespace dir, so behavior_source.parent.name IS the
    # expected lineage. Only namespaced (feedback) sources look like
    # "{preset}_v{version}"; skip otherwise.
    expected_ns = behavior_source.parent.name
    check_lineage = "_v" in expected_ns

    # Extract the tuned role (first entry in roles array) for each node,
    # keyed by bare node name.
    #
    # PHASE writes _phase_metadata.mode in {"feedback", "controls"} on every
    # per-node file. The runtime treats both modes identically — same loading,
    # same role-rename, same enterprise-config rewrite. The mode field is a
    # FATAL gate against schema regression: any unknown value (or missing
    # _phase_metadata) means PHASE bumped its contract and RAMPART hasn't
    # caught up. Fail loud rather than silently doing the wrong thing.
    tuned_by_bare: dict[str, dict] = {}
    for f in per_node_files:
        bare_name = f.parent.name  # e.g. "linep9"
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as e:
            output.info(f"  WARNING: failed to parse {f}: {e}")
            continue
        mode = (data.get("_phase_metadata") or {}).get("mode")
        if mode not in ("feedback", "controls"):
            output.error("")
            output.error("=" * 70)
            output.error(f"FATAL: unknown _phase_metadata.mode in {f}")
            output.error(f"  got: {mode!r}")
            output.error(f"  expected: 'feedback' or 'controls'")
            output.error("  This indicates a PHASE schema bump that RAMPART has not")
            output.error("  caught up with. Aborting rather than risk silent miswiring.")
            output.error("=" * 70)
            sys.exit(1)
        if check_lineage:
            pm = data.get("_phase_metadata") or {}
            mp, mv = pm.get("model_preset"), pm.get("model_version")
            stamped = f"{mp}_v{mv}" if mp and mv else None
            if stamped and stamped != expected_ns:
                output.error("")
                output.error("=" * 70)
                output.error(f"FATAL: lineage mismatch in {f}")
                output.error(f"  stamped {stamped!r} != deployed namespace {expected_ns!r}")
                output.error("  --preset points at a source generated for a different lineage.")
                output.error("=" * 70)
                sys.exit(1)
        roles = data.get("roles", [])
        if not roles:
            output.info(f"  WARNING: {f} has empty roles array, skipping")
            continue
        tuned_by_bare[bare_name] = roles[0]

    if not tuned_by_bare:
        output.info(f"  No valid tuned roles could be extracted from {behavior_source}")
        return None

    # Walk the enterprise config nodes, rewriting each fed node's user field
    # and collecting the tuned roles (renamed to include the r-{hash}- prefix).
    modified_enterprise = copy.deepcopy(enterprise)
    per_node_roles_out: list[dict] = []
    nodes_processed: list[str] = []

    for i, node in enumerate(modified_enterprise.get("nodes", [])):
        node_name = node.get("name", "")  # e.g. "r-14a6d-linep9"
        if node.get("user") is None:
            continue  # dc1/dc2/dc3/linep1 — no user, skip

        # Strip the r-{5char_hex}- prefix to get the bare node name
        bare_name = re.sub(r"^r-[a-f0-9]+-", "", node_name)

        tuned = tuned_by_bare.get(bare_name)
        if tuned is None:
            # No PHASE feedback for this node — leave its user field pointing
            # at the baseline role (which is in baseline_trio). The enterprise
            # config node keeps its current user value unchanged.
            continue

        # Clone the tuned role and rename it to the deployment-unique form
        renamed = copy.deepcopy(tuned)
        new_role_name = f"{node_name}_user"  # e.g. "r-14a6d-linep9_user"
        renamed["name"] = new_role_name
        per_node_roles_out.append(renamed)

        modified_enterprise["nodes"][i]["user"] = new_role_name
        nodes_processed.append(node_name)

    # Combined output: tuned per-node roles first, then the baseline trio
    # (for any node whose "user" field wasn't rewritten above).
    all_roles = per_node_roles_out + baseline_trio
    output_roles = {"roles": all_roles}

    roles_path = output_dir / "user-roles-feedback.json"
    enterprise_path = output_dir / "enterprise-config-feedback.json"
    roles_path.write_text(json.dumps(output_roles, indent=2) + "\n")
    enterprise_path.write_text(json.dumps(modified_enterprise, indent=2) + "\n")

    return {
        "user_roles_path": roles_path,
        "enterprise_config_path": enterprise_path,
        "role_count": len(per_node_roles_out),
        "nodes_processed": nodes_processed,
    }


def _copy_if_exists(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copy2(src, dst)
