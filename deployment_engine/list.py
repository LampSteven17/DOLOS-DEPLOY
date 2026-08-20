"""List active deployments."""

from __future__ import annotations

from .core.vm_naming import (
    make_ent_vm_prefix, make_ghosts_vm_prefix, make_run_dep_id, make_vm_prefix,
)
import re
from pathlib import Path

from .core import output
from .core.config import DeploymentConfig
from .core.openstack import OpenStack
from .core.phase_run_registry import PhaseRunRegistryError, validate_run_id


def run_list(deploy_dir: Path) -> int:
    """Display all active deployments grouped by type."""
    output.info("")
    output.banner("ACTIVE DEPLOYMENTS")
    output.info("")

    output.dim("  Querying OpenStack...")
    os_client = OpenStack()
    server_statuses = os_client.server_status_map()

    # Collect rows grouped by deployment type
    groups: dict[str, list[list[str]]] = {
        "decoy": [],
        "rampart": [],
        "ghosts": [],
        "other": [],
    }

    for config_dir in sorted(deploy_dir.iterdir()):
        config_file = config_dir / "config.yaml"
        if not config_file.exists() or not config_dir.is_dir():
            continue

        name = config_dir.name
        runs_dir = config_dir / "runs"
        if not runs_dir.is_dir():
            continue

        try:
            config = DeploymentConfig.load(config_file)
        except Exception as e:
            output.error(f"  WARNING: skipping {config_file.parent.name}/config.yaml: "
                         f"{type(e).__name__}: {e}")
            continue

        if config.is_rampart():
            group = "rampart"
        elif config.is_ghosts():
            group = "ghosts"
        elif config.deployment_type == "decoy":
            group = "decoy"
        else:
            group = "other"

        for run_dir in sorted(runs_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            rid = run_dir.name
            try:
                validate_run_id(rid)
            except PhaseRunRegistryError:
                continue

            is_active = has_exact_run_vm(
                name, rid, config, server_statuses,
            )
            if not is_active:
                continue

            vm_summary = _get_vm_summary(run_dir, config)
            expected = _get_expected_count(run_dir, config)
            active, bad_statuses, nbhd_status = _count_live_vms(
                name, rid, config, server_statuses,
            )
            active_col = f"{active}/{expected}" if expected > 0 else "?"
            status_col = _format_status_col(bad_statuses, nbhd_status, expected, active)
            date_col = _format_run_date(rid)
            target = f"{name}-{rid}"

            groups[group].append([target, vm_summary, active_col, status_col, date_col])

    total = sum(len(rows) for rows in groups.values())
    if total == 0:
        output.dim("  No active deployments.")
        output.info("")
        return 0

    GROUP_LABELS = {
        "decoy": "DECOY SUPs",
        "rampart": "RAMPART Enterprise",
        "ghosts": "GHOSTS NPCs",
        "other": "Other",
    }

    # Compute global column widths across all groups for alignment
    headers = ["Target", "VMs", "Active", "Status", "Date"]
    all_rows = [row for rows in groups.values() for row in rows]
    col_widths = [len(h) for h in headers]
    for row in all_rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    for key in ("decoy", "rampart", "ghosts", "other"):
        rows = groups[key]
        if not rows:
            continue
        output.header(GROUP_LABELS[key])
        output.table(headers, rows, col_widths=col_widths)
        output.info("")

    return 0


def has_exact_run_vm(
    name: str,
    rid: str,
    config: DeploymentConfig,
    server_statuses: dict[str, str],
) -> bool:
    """Return whether OpenStack contains a VM for this exact run prefix."""
    prefix = _prefix_for(config, make_run_dep_id(name, rid))
    return any(vm_name.startswith(prefix) for vm_name in server_statuses)


def _get_expected_count(run_dir: Path, config: DeploymentConfig) -> int:
    """Get expected VM count from inventory or config."""
    if config.is_rampart():
        summary = _get_enterprise_vm_count(run_dir)
        # Parse "23 (3 infra + 20 ep)" → 23
        try:
            return int(summary.split()[0])
        except (ValueError, IndexError):
            return 0

    if config.is_ghosts():
        return 1 + config.ghosts_client_count()

    # SUP: count from inventory or config
    inv_path = run_dir / "inventory.ini"
    if inv_path.exists():
        return sum(1 for line in inv_path.read_text().splitlines()
                   if re.search(r"sup_behavior=\S+", line))
    return config.vm_count()


def _count_live_vms(
    name: str,
    rid: str,
    config: DeploymentConfig,
    server_statuses: dict[str, str],
) -> tuple[int, dict[str, int], str | None]:
    """Inspect OpenStack VMs for this deployment.

    Returns:
        active: count of ACTIVE primary VMs (SUP/RAMPART/GHOSTS, EXCLUDES the
            DECOY neighborhood sidecar).
        bad_statuses: {status: count} for any primary VM not in ACTIVE state
            (ERROR, SHUTOFF, BUILD, etc.) — these show as a visible problem.
        nbhd_status: status string of the DECOY neighborhood sidecar if one
            exists, else None. Reported separately because it's not part of
            the SUP expected count.
    """
    prefix = _prefix_for(config, make_run_dep_id(name, rid))
    is_decoy = not config.is_rampart() and not config.is_ghosts()
    active = 0
    bad: dict[str, int] = {}
    nbhd_status: str | None = None
    for vm_name, status in server_statuses.items():
        if not vm_name.startswith(prefix):
            continue
        if is_decoy and vm_name.endswith("-neighborhood-0"):
            nbhd_status = status
            continue
        if status == "ACTIVE":
            active += 1
        else:
            bad[status] = bad.get(status, 0) + 1
    return active, bad, nbhd_status


def _format_status_col(
    bad_statuses: dict[str, int],
    nbhd_status: str | None,
    expected: int,
    active: int,
) -> str:
    """Compact status annotation: errored VMs, sidecar presence, missing VMs."""
    parts: list[str] = []
    for status in sorted(bad_statuses):
        parts.append(f"{bad_statuses[status]} {status}")
    accounted = active + sum(bad_statuses.values())
    if expected > 0 and accounted < expected:
        parts.append(f"{expected - accounted} missing")
    if nbhd_status is not None:
        parts.append("+nbhd" if nbhd_status == "ACTIVE" else f"+nbhd:{nbhd_status}")
    if not parts:
        return "OK"
    return ", ".join(parts)


def _prefix_for(config: DeploymentConfig, dep_id: str) -> str:
    """Dispatch on deploy type to the right prefix builder."""
    if config.is_rampart():
        return make_ent_vm_prefix(dep_id)
    if config.is_ghosts():
        return make_ghosts_vm_prefix(dep_id)
    return make_vm_prefix(dep_id)


def _get_vm_summary(run_dir: Path, config: DeploymentConfig) -> str:
    """Get VM count summary from inventory or config."""
    if config.is_rampart():
        return _get_enterprise_vm_count(run_dir)

    if config.is_ghosts():
        client_count = config.ghosts_client_count()
        return f"{1 + client_count} ghosts (1 api + {client_count} npc)"

    # Try inventory.ini first (actual deployed)
    inv_path = run_dir / "inventory.ini"
    if inv_path.exists():
        return _count_brains_from_inventory(inv_path)

    # Fall back to config
    return config.brain_summary()


def _count_brains_from_inventory(inv_path: Path) -> str:
    """Count brains from inventory.ini sup_behavior= fields."""
    counts = {"C": 0, "M": 0, "B": 0, "S": 0, "total": 0}
    for line in inv_path.read_text().splitlines():
        match = re.search(r"sup_behavior=(\S+)", line)
        if match:
            b = match.group(1)
            counts["total"] += 1
            if b.startswith("C"):
                counts["C"] += 1
            elif b.startswith("M"):
                counts["M"] += 1
            elif b.startswith("B"):
                counts["B"] += 1
            elif b.startswith("S"):
                counts["S"] += 1

    parts = []
    if counts["C"]:
        parts.append(f"{counts['C']}c")
    if counts["M"]:
        parts.append(f"{counts['M']}m")
    if counts["B"]:
        parts.append(f"{counts['B']}b")
    if counts["S"]:
        parts.append(f"{counts['S']}s")

    if parts:
        return f"{counts['total']} ({' '.join(parts)})"
    return str(counts["total"])


def _get_enterprise_vm_count(run_dir: Path) -> str:
    """Get VM count from enterprise deploy-output.json."""
    import json

    nodes = None
    for fname in ("enterprise-config-prefixed.json", "deploy-output.json"):
        fpath = run_dir / fname
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text())
            if isinstance(data, list):
                nodes = data
            elif isinstance(data, dict):
                # enterprise-config-prefixed.json: {"nodes": [...]}
                # deploy-output.json: {"enterprise_built": {"deployed": {"nodes": [...]}}}
                nodes = data.get("nodes")
                if nodes is None:
                    nodes = (
                        data.get("enterprise_built", {})
                        .get("deployed", {})
                        .get("nodes", [])
                    )
            if nodes:
                break
        except (json.JSONDecodeError, TypeError):
            continue

    if not nodes:
        return "?"

    total = len(nodes)
    endpoints = sum(
        1 for n in nodes
        if "endpoint" in n.get("roles", [])
    )
    infra = total - endpoints
    return f"{total} ({infra} infra + {endpoints} ep)"


def _format_run_date(rid: str) -> str:
    """Format a validated Phase 3 UTC run ID for the table."""
    return f"{rid[5:7]}/{rid[8:10]} {rid[11:13]}:{rid[13:15]}"
