from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from deployment_engine import list as list_command
from deployment_engine.core import feedback
from deployment_engine.core.config import DeploymentConfig
from deployment_engine.core.deploy_steps import share_sidecar_vms
from deployment_engine.core.vm_naming import make_run_dep_id, make_vm_prefix
from deployment_engine.decoy import spinup, teardown


ROOT = Path(__file__).resolve().parents[1]
CONTROL_FIXTURES = Path(
    "/home/ubuntu/PHASE/plans/feedback-v2-rewrite/fixtures/controls"
)
CANONICAL = tuple(feedback.DECOY_PLAN_FILENAMES)


def write_generation(root: Path, *, include_share: bool) -> Path:
    generation = root / "2026-08-24_1456Z"
    generation.mkdir(parents=True)
    capabilities = json.loads(feedback.WORKFLOW_CAPABILITIES_PATH.read_text())
    instructions = capabilities["instructions"]["feedback-v2"]
    replacements = {
        "WebResearch": "wikipedia_compiler",
        "VideoViewing": "video_cpp_course",
        "DocumentCreation": "document_team_meeting_notes",
    }
    for sup_config, filename in feedback.DECOY_PLAN_FILENAMES.items():
        document = json.loads((CONTROL_FIXTURES / filename).read_text())
        document["resource_profile"] = "feedback-v2"
        for window in document["schedule"]:
            for entry in window["sequence"]:
                entry["resource_id"] = replacements[entry["workflow"]]
                if "instruction" in entry["brain"]:
                    entry["brain"]["instruction"] = instructions[entry["workflow"]]
        if include_share and sup_config == "scripted-cpu":
            brain = {"profile": "scripted-v1"}
            document["schedule"][0]["sequence"].append({
                "offset_minutes": 45,
                "workflow": "NetworkShareAccess",
                "resource_id": "share_team_notes",
                "brain": brain,
            })
        (generation / filename).write_text(json.dumps(document) + "\n")
    return generation


class ShareSidecarTests(unittest.TestCase):
    def test_sidecar_is_conditional_on_validated_network_share_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            absent = write_generation(root / "absent", include_share=False)
            present = write_generation(root / "present", include_share=True)
            self.assertFalse(feedback.decoy_generation_uses_network_share(
                absent, purpose="feedback"
            ))
            self.assertTrue(feedback.decoy_generation_uses_network_share(
                present, purpose="feedback"
            ))

    def test_provision_uses_exact_prefixed_name_flavor_defaults_and_assigned_ip(self):
        class Cloud:
            def __init__(self):
                self.created = []

            def create_server(self, name, **values):
                self.created.append((name, values))
                return "server-id"

            def wait_server_active(self, name):
                self.waited = name
                return {"status": "ACTIVE", "addresses": "ext_net=10.2.3.4"}

            def server_ipv4(self, details):
                self.details = details
                return "10.2.3.4"

        cloud = Cloud()
        prefix = "d-decoy-feedback-x-2026-08-24_1456Z-"
        with mock.patch.object(spinup, "OpenStack", return_value=cloud):
            host = spinup._provision_share_sidecar("deployment-id", prefix)
        self.assertEqual(host, {
            "name": prefix + "share-0",
            "ip": "10.2.3.4",
            "flavor": "v1.small",
            "sup_config": None,
        })
        self.assertEqual(cloud.waited, prefix + "share-0")
        self.assertEqual(cloud.created, [(prefix + "share-0", {
            "flavor": "v1.small",
            "image": "noble-amd64",
            "network": "ext_net",
            "keypair": "bot-desktop",
            "security_group": "default",
            "deployment": "deployment-id",
            "boot_volume_gb": 200,
        })])

    def test_inventory_capture_registers_assigned_ip_with_null_sup(self):
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory.ini"
            inventory.write_text("[sup_hosts]\n")
            spinup._append_share_inventory(inventory, {
                "name": "d-fleet-run-share-0",
                "ip": "10.2.3.4",
                "flavor": "v1.small",
                "sup_config": None,
            })
            self.assertEqual(share_sidecar_vms(inventory.parent), [{
                "name": "d-fleet-run-share-0",
                "ip": "10.2.3.4",
                "sup_config": None,
            }])
            content = inventory.read_text()
        self.assertIn("[share_sidecar]", content)
        self.assertIn("share_sidecar=true", content)
        self.assertIn("sup_flavor=v1.small", content)

    def test_playbook_has_exact_identity_dns_principals_and_fleet_keytab(self):
        path = ROOT / "deployment_engine/playbooks/decoy/prepare-share.yaml"
        document = yaml.safe_load(path.read_text())
        serialized = path.read_text()
        first, second = document
        self.assertEqual(first["hosts"], "share_sidecar")
        self.assertEqual(second["hosts"], "sup_hosts")
        self.assertEqual(first["vars"], {
            **first["vars"],
            "share_fqdn": "share.ruse.test",
            "share_domain": "ruse.test",
            "share_netbios_domain": "RUSE",
            "share_realm": "RUSE.TEST",
            "share_name": "shared",
            "share_principal": "ruse-share@RUSE.TEST",
            "share_service_principal": "cifs/share.ruse.test@RUSE.TEST",
        })
        for required in (
            "dns forwarder",
            "SAMBA_INTERNAL",
            "//share.ruse.test/shared",
            "--use-kerberos=required",
            "hostvars[groups['share_sidecar'][0]].share_keytab_blob.content",
            "DNS={{ share_ip }}",
            "getent hosts \"{{ share_fqdn }}\"",
        ):
            self.assertIn(required, serialized)
        self.assertNotIn("/etc/hosts", serialized)
        self.assertNotIn("fixed_ip", serialized)
        self.assertTrue(all(
            task.get("no_log") is True
            for play in document
            for task in play["tasks"]
            if "password" in task["name"].lower() or "keytab" in task["name"].lower()
        ))

    def test_seed_generator_and_share_paths_are_exact(self):
        profile = json.loads(
            (ROOT / "contracts/phase-workflow-plan-v1/resource-profiles/feedback-v2.json")
            .read_text()
        )["resources"]
        self.assertEqual(
            {
                key: value["path"]
                for key, value in profile.items()
                if value["workflow"] == "NetworkShareAccess"
            },
            {
                "share_team_notes": "Team/meeting-notes.odt",
                "share_inventory": "Operations/inventory.ods",
                "share_project_status": "Projects/project-status.odt",
            },
        )
        generator_path = (
            ROOT / "deployment_engine/playbooks/decoy/files/create-share-seeds.py"
        )
        with tempfile.TemporaryDirectory() as temporary:
            share_root = Path(temporary) / "shared"
            subprocess.run(
                [
                    sys.executable,
                    str(generator_path),
                    str(ROOT / "contracts/phase-workflow-plan-v1/resource-profiles/feedback-v2.json"),
                    str(share_root),
                ],
                check=True,
            )
            self.assertTrue((share_root / "Team/meeting-notes.odt").is_file())
            self.assertTrue((share_root / "Operations/inventory.ods").is_file())
            self.assertTrue((share_root / "Projects/project-status.odt").is_file())

    def test_share_preparation_precedes_all_sup_service_installation(self):
        source = (ROOT / "deployment_engine/decoy/spinup.py").read_text()
        self.assertLess(
            source.index('"decoy/prepare-share.yaml"'),
            source.index("install_result = runner.run_playbook"),
        )
        playbook = yaml.safe_load(
            (ROOT / "deployment_engine/playbooks/decoy/prepare-share.yaml").read_text()
        )
        smoke_names = [task["name"] for task in playbook[1]["tasks"]]
        self.assertIn("Fleet smoke DNS and Kerberos from scripted-cpu", smoke_names)
        self.assertIn("Fleet smoke list and seed download from scripted-cpu", smoke_names)
        self.assertIn("Fleet smoke probe upload verify and remove from scripted-cpu", smoke_names)

    def test_exact_prefix_list_and_teardown_include_share_without_collision(self):
        config = DeploymentConfig(
            deployment_name="decoy-feedback-x",
            purpose="feedback",
            target="axes-summer24",
            deployments=[],
        )
        run_id = "2026-08-24_145600Z"
        prefix = make_vm_prefix(make_run_dep_id(config.deployment_name, run_id))
        statuses = {
            prefix + "share-0": "ACTIVE",
            prefix + "scripted-cpu-0": "ACTIVE",
            prefix.removesuffix("-") + "x-share-0": "ACTIVE",
        }
        self.assertTrue(list_command.has_exact_run_vm(
            config.deployment_name, run_id, config, statuses
        ))
        active, bad, sidecars = list_command._count_live_vms(
            config.deployment_name, run_id, config, statuses
        )
        self.assertEqual((active, bad, sidecars), (1, {}, {"share": "ACTIVE"}))

        cloud = mock.Mock()
        cloud.server_cohort.return_value = [
            {"id": "share-id", "name": prefix + "share-0", "status": "ACTIVE"},
            {"id": "sup-id", "name": prefix + "scripted-cpu-0", "status": "ACTIVE"},
        ]
        with mock.patch.object(teardown, "_remaining", return_value=10.0):
            cohort = teardown._query_servers(cloud, prefix, 10.0)
        self.assertEqual({item["id"] for item in cohort}, {"share-id", "sup-id"})


if __name__ == "__main__":
    unittest.main()
