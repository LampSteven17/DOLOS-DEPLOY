"""Deployment configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DeploymentConfig:
    """Parsed deployment configuration from config.yaml."""

    deployment_name: str
    deployment_type: str = "decoy"  # "decoy", "rampart", or "ghosts"
    purpose: str = ""
    target: str | None = None
    capture_interface: str = "eno2"
    flavor_capacity: dict[str, int] = field(default_factory=dict)
    deployments: list[dict] = field(default_factory=list)
    behavior_source: str | None = None
    behavior_configs: list[str] | str | None = None  # "all", list of filenames, or None
    enterprise: dict | None = None
    emulate: dict | None = None
    ghosts: dict | None = None
    gpu_tier: str | None = None  # "v100" | "rtx" | "rtx-a" — decoy feedback only

    @classmethod
    def load(cls, config_path: Path) -> DeploymentConfig:
        """Load and parse a config.yaml file."""
        with open(config_path) as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"deployment config must be a mapping: {config_path}")
        missing = [field for field in ("purpose", "target") if field not in raw]
        if missing:
            raise ValueError(
                f"deployment config missing required field(s): {', '.join(missing)}"
            )
        purpose = raw["purpose"]
        if purpose not in {"control", "feedback", "other"}:
            raise ValueError(
                "deployment config purpose must be control, feedback, or other"
            )
        target = raw["target"]
        if target is not None and (
            not isinstance(target, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", target) is None
        ):
            raise ValueError("deployment config target must be an identifier or null")
        if purpose == "feedback" and target is None:
            raise ValueError("feedback deployment config target must not be null")
        capture_interface = raw.get("capture_interface", "eno2")
        if not isinstance(capture_interface, str) or not capture_interface:
            raise ValueError("deployment config capture_interface must be non-empty")

        return cls(
            deployment_name=raw.get("deployment_name", config_path.parent.name),
            deployment_type=raw.get("type", "decoy"),
            purpose=purpose,
            target=target,
            capture_interface=capture_interface,
            flavor_capacity=raw.get("flavor_capacity", {}),
            deployments=raw.get("deployments", []),
            behavior_source=raw.get("behavior_source"),
            behavior_configs=raw.get("behavior_configs"),
            enterprise=raw.get("enterprise"),
            emulate=raw.get("emulate"),
            ghosts=raw.get("ghosts"),
            gpu_tier=raw.get("gpu_tier"),
        )

    def vm_count(self) -> int:
        return sum(d.get("count", 1) for d in self.deployments)

    def has_behavior_configs(self) -> bool:
        return self.behavior_source is not None

    def is_rampart(self) -> bool:
        return self.deployment_type == "rampart"

    def is_ghosts(self) -> bool:
        return self.deployment_type == "ghosts"


    def count_brains(self) -> dict[str, int]:
        """Count VMs by brain category. Returns {C, M, B, S, total}."""
        counts = {"C": 0, "M": 0, "B": 0, "S": 0, "total": 0}
        for d in self.deployments:
            b = d["behavior"]
            count = d.get("count", 1)
            counts["total"] += count
            if b.startswith("C"):
                counts["C"] += count
            elif b.startswith("M"):
                counts["M"] += count
            elif b.startswith("B"):
                counts["B"] += count
            elif b.startswith("S"):
                counts["S"] += count
        return counts

    def brain_summary(self) -> str:
        """Human-readable brain count string like '3c 1m 4b 4s'."""
        c = self.count_brains()
        parts = []
        if c["C"]:
            parts.append(f"{c['C']}c")
        if c["M"]:
            parts.append(f"{c['M']}m")
        if c["B"]:
            parts.append(f"{c['B']}b")
        if c["S"]:
            parts.append(f"{c['S']}s")
        return f"{c['total']} ({' '.join(parts)})" if parts else str(c["total"])

    # --- Enterprise helpers ---

    def enterprise_workflow_dir(self) -> Path:
        if not self.enterprise:
            raise ValueError("Not an enterprise config")
        return Path(os.path.expanduser(self.enterprise.get("workflow_dir", "~/uva-cs-workflow")))

    def enterprise_cloud_config(self) -> str:
        return (self.enterprise or {}).get("cloud_config", "")

    def enterprise_config_file(self) -> str:
        return (self.enterprise or {}).get("enterprise_config", "")

    def enterprise_user_roles(self) -> str:
        return (self.enterprise or {}).get("user_roles", "")

    def emulate_seed(self) -> int:
        return (self.emulate or {}).get("seed", 42)

    def emulate_duration_days(self) -> int:
        return (self.emulate or {}).get("duration_days", 7)

    # --- GHOSTS helpers ---

    def ghosts_api_flavor(self) -> str:
        return (self.ghosts or {}).get("api_flavor", "v1.14vcpu.28g")

    def ghosts_client_flavor(self) -> str:
        return (self.ghosts or {}).get("client_flavor", "v1.14vcpu.28g")

    def ghosts_client_count(self) -> int:
        return (self.ghosts or {}).get("client_count", 5)

    def ghosts_repo(self) -> str:
        return (self.ghosts or {}).get("ghosts_repo", "https://github.com/cmu-sei/GHOSTS.git")

    def ghosts_branch(self) -> str:
        return (self.ghosts or {}).get("ghosts_branch", "master")
