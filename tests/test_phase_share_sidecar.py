from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from deployment_engine import list as list_command
from deployment_engine.core import feedback
from deployment_engine.core.ansible_runner import _LineParser, _STEP_TASKS
from deployment_engine.core.config import DeploymentConfig
from deployment_engine.core.deploy_steps import share_sidecar_vms
from deployment_engine.core.vm_naming import make_run_dep_id, make_vm_prefix
from deployment_engine.decoy import spinup, teardown


ROOT = Path(__file__).resolve().parents[1]
CONTROL_FIXTURES = Path(
    "/home/ubuntu/PHASE/plans/feedback-v2-rewrite/fixtures/controls"
)
CANONICAL = tuple(feedback.DECOY_PLAN_FILENAMES)
SHARE_PLAYBOOK = ROOT / "deployment_engine/playbooks/decoy/prepare-share.yaml"


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
                if "instruction" in entry:
                    entry["instruction"] = instructions[entry["workflow"]]
        if include_share and sup_config == "scripted-cpu":
            document["schedule"][0]["sequence"].append({
                "offset_minutes": 45,
                "workflow": "NetworkShareAccess",
                "resource_id": "share_team_notes",
            })
        (generation / filename).write_text(json.dumps(document) + "\n")
    return generation


class ShareSidecarTests(unittest.TestCase):
    @staticmethod
    def sidecar_tasks():
        play = yaml.safe_load(SHARE_PLAYBOOK.read_text())[0]
        return play["tasks"], {task["name"]: task for task in play["tasks"]}

    @staticmethod
    def client_tasks():
        play = yaml.safe_load(SHARE_PLAYBOOK.read_text())[1]

        def flatten(tasks):
            for task in tasks:
                yield task
                for section in ("block", "rescue", "always"):
                    yield from flatten(task.get(section, []))

        tasks = list(flatten(play["tasks"]))
        return tasks, {task["name"]: task for task in tasks}

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
        path = SHARE_PLAYBOOK
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
            "winbind",
            "dns forwarder",
            "SAMBA_INTERNAL",
            "DNSStubListener=no",
            "/run/systemd/resolve/resolv.conf",
            "Synchronize fleet-local administrator password",
            "Create or reset fleet-local share client account",
            "Wait for authoritative Samba DNS",
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

    def test_sidecar_installs_winbind_with_existing_server_packages(self):
        _tasks, by_name = self.sidecar_tasks()
        packages = by_name[
            "Install Samba AD and SMB/Kerberos packages"
        ]["apt"]["name"]
        self.assertEqual(
            packages,
            ["samba", "winbind", "smbclient", "krb5-user", "dnsutils", "python3"],
        )

    def test_port_53_is_released_before_samba_and_final_resolver_is_local(self):
        tasks, by_name = self.sidecar_tasks()
        names = [task["name"] for task in tasks]
        retain = by_name[
            "Retain OpenStack DHCP DNS forwarder before resolver changes"
        ]["shell"]
        self.assertIn("/etc/samba/smb.conf", retain)
        self.assertIn("/run/systemd/resolve/resolv.conf", retain)
        self.assertIn("$2 !~ /^127\\./", retain)

        release = by_name[
            "Release port 53 while retaining the OpenStack DNS forwarder"
        ]["copy"]["content"]
        self.assertIn("DNS={{ openstack_dns.stdout | trim }}", release)
        self.assertIn("DNSStubListener=no", release)
        link = by_name["Use the non-stub systemd-resolved resolver file"]["file"]
        self.assertEqual(link["src"], "/run/systemd/resolve/resolv.conf")
        self.assertEqual(link["dest"], "/etc/resolv.conf")
        self.assertEqual(link["state"], "link")
        self.assertTrue(link["force"])
        self.assertLess(
            names.index("Apply bootstrap resolver configuration"),
            names.index("Start Samba AD DC"),
        )

        final = by_name[
            "Configure sidecar to use its authoritative Samba DNS"
        ]["copy"]["content"]
        self.assertIn("DNS=127.0.0.1", final)
        self.assertIn("Domains=~.", final)
        self.assertIn("DNSStubListener=no", final)

    def test_samba_dns_readiness_precedes_every_dns_command(self):
        tasks, by_name = self.sidecar_tasks()
        names = [task["name"] for task in tasks]
        wait = by_name["Wait for authoritative Samba DNS"]["wait_for"]
        self.assertEqual(wait, {
            "host": "127.0.0.1",
            "port": 53,
            "timeout": 60,
        })
        self.assertLess(
            names.index("Start Samba AD DC"),
            names.index("Wait for authoritative Samba DNS"),
        )
        self.assertLess(
            names.index("Wait for authoritative Samba DNS"),
            names.index("Map share.ruse.test to the assigned OpenStack address"),
        )

    def test_partial_failure_retry_preserves_domain_and_refreshes_credentials(self):
        tasks, by_name = self.sidecar_tasks()
        names = [task["name"] for task in tasks]
        self.assertEqual(
            by_name["Check for an existing fleet-local Samba domain"]["stat"]["path"],
            "/var/lib/samba/private/sam.ldb",
        )
        remove_config = by_name[
            "Remove package-default Samba configuration before AD provisioning"
        ]
        self.assertEqual(remove_config["when"], "not samba_domain_db.stat.exists")
        provision = by_name["Provision fixed Samba AD identity"]
        self.assertEqual(provision["args"]["creates"], "/var/lib/samba/private/sam.ldb")
        self.assertTrue(provision["no_log"])

        admin = by_name["Synchronize fleet-local administrator password"]
        self.assertNotIn("when", admin)
        self.assertIn("setpassword", admin["command"]["argv"])
        self.assertTrue(admin["no_log"])

        account = by_name["Create or reset fleet-local share client account"]
        self.assertIn("samba-tool user show ruse-share", account["shell"])
        self.assertIn("samba-tool user setpassword ruse-share", account["shell"])
        self.assertIn("samba-tool user create ruse-share", account["shell"])
        self.assertTrue(account["no_log"])

        spn = by_name["Register exact SMB service principal"]["shell"]
        self.assertIn("samba-tool spn list 'SHARE$'", spn)
        self.assertIn("grep -Eq", spn)
        self.assertIn("samba-tool spn add cifs/share.ruse.test 'SHARE$'", spn)

        remove_keytab = by_name[
            "Remove prior fleet-local client keytab before export"
        ]
        export_keytab = by_name["Export fresh fleet-local client keytab"]
        self.assertLess(
            names.index("Remove prior fleet-local client keytab before export"),
            names.index("Export fresh fleet-local client keytab"),
        )
        self.assertEqual(remove_keytab["file"]["state"], "absent")
        self.assertTrue(remove_keytab["no_log"])
        self.assertNotIn("args", export_keytab)
        self.assertTrue(export_keytab["no_log"])

    def test_smoke_network_operations_are_separate_bounded_and_noninteractive(self):
        tasks, by_name = self.client_tasks()
        names = [task["name"] for task in tasks]
        expected = [
            "Resolve share DNS",
            "Acquire Kerberos ticket",
            "List assigned SMB directory",
            "Download assigned seed",
            "Verify downloaded seed",
            "Upload smoke probe",
            "Download uploaded SMB smoke probe",
            "Verify uploaded SMB smoke probe",
            "Remove remote smoke probe",
            "Remove local smoke files",
        ]
        self.assertEqual(
            [name for name in names if name in expected], expected
        )

        network_tasks = [
            "Resolve share DNS",
            "Acquire Kerberos ticket",
            "List assigned SMB directory",
            "Download assigned seed",
            "Upload smoke probe",
            "Download uploaded SMB smoke probe",
            "Remove remote smoke probe",
        ]
        for name in network_tasks:
            with self.subTest(name=name):
                task = by_name[name]
                self.assertEqual(task["timeout"], 35)
                self.assertIn(
                    "timeout --signal=TERM --kill-after=3s 30s",
                    task["shell"],
                )
                self.assertNotIn("no_log", task)

        for name in (
            "List assigned SMB directory",
            "Download assigned seed",
            "Upload smoke probe",
            "Download uploaded SMB smoke probe",
            "Remove remote smoke probe",
        ):
            command = by_name[name]["shell"]
            self.assertIn("--use-kerberos=required --no-pass", command)

        kerberos = by_name["Acquire Kerberos ticket"]["shell"]
        self.assertIn('kinit -kt "{{ share_keytab_path }}"', kerberos)

    def test_uploaded_probe_is_round_tripped_and_compared_exactly(self):
        _, by_name = self.client_tasks()
        playbook_source = SHARE_PLAYBOOK.read_text()
        self.assertNotIn("allinfo", playbook_source)

        download = by_name["Download uploaded SMB smoke probe"]
        self.assertIn(
            '-c \'get "{{ share_smoke_remote }}" "{{ share_smoke_roundtrip }}"\'',
            download["shell"],
        )
        self.assertEqual(download["timeout"], 35)
        self.assertIn(
            "timeout --signal=TERM --kill-after=3s 30s",
            download["shell"],
        )
        self.assertIn("--use-kerberos=required --no-pass", download["shell"])

        verify = by_name["Verify uploaded SMB smoke probe"]
        self.assertIn('test -f "{{ share_smoke_roundtrip }}"', verify["shell"])
        self.assertIn(
            'stat -c %s "{{ share_smoke_roundtrip }}"', verify["shell"]
        )
        self.assertIn(' -eq 16', verify["shell"])
        self.assertIn(
            'cmp -s "{{ share_smoke_probe }}" "{{ share_smoke_roundtrip }}"',
            verify["shell"],
        )

    def test_smoke_cleanup_is_an_always_block_after_failure(self):
        play = yaml.safe_load(SHARE_PLAYBOOK.read_text())[1]
        verification = next(
            task for task in play["tasks"]
            if task["name"] == "Verify fleet-local share from scripted-cpu"
        )
        self.assertEqual(
            [task["name"] for task in verification["always"]],
            ["Clean smoke probes after verification"],
        )
        cleanup = verification["always"][0]
        self.assertEqual(
            cleanup["block"][0]["when"],
            "share_probe_upload is defined",
        )
        self.assertEqual(
            cleanup["always"][0]["file"]["state"], "absent"
        )
        self.assertIn(
            "{{ share_smoke_roundtrip }}",
            cleanup["always"][0]["loop"],
        )
        self.assertNotIn("rescue", verification)

    def test_prepare_share_steps_are_visible_with_host_results_and_failures(self):
        visible_names = (
            "Start Samba AD DC",
            "Wait for authoritative Samba DNS",
            "Resolve share DNS",
            "Acquire Kerberos ticket",
            "List assigned SMB directory",
            "Download assigned seed",
            "Verify downloaded seed",
            "Upload smoke probe",
            "Download uploaded SMB smoke probe",
            "Verify uploaded SMB smoke probe",
            "Remove remote smoke probe",
            "Remove local smoke files",
        )
        self.assertTrue(all(name in _STEP_TASKS for name in visible_names))
        self.assertEqual(len({_STEP_TASKS[name] for name in visible_names}), len(visible_names))
        self.assertEqual(
            _STEP_TASKS["Download uploaded SMB smoke probe"],
            "Downloading uploaded SMB smoke probe",
        )
        self.assertEqual(
            _STEP_TASKS["Verify uploaded SMB smoke probe"],
            "Verifying uploaded SMB smoke probe",
        )

        parser = _LineParser(time.time())
        task_event = parser.parse("TASK [Download assigned seed] ****")
        ok_event = parser.parse("ok: [d-fleet-scripted-cpu-0]")
        failure_event = parser.parse(
            'fatal: [d-fleet-scripted-cpu-0]: FAILED! => {"msg": "command timed out"}'
        )
        self.assertEqual(task_event.task, "Downloading assigned SMB seed")
        self.assertEqual((ok_event.kind, ok_event.host), (
            "host_ok", "d-fleet-scripted-cpu-0"
        ))
        self.assertEqual((failure_event.kind, failure_event.host), (
            "host_fail", "d-fleet-scripted-cpu-0"
        ))
        self.assertEqual(failure_event.detail, "command timed out")

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
        smoke = next(
            task for task in playbook[1]["tasks"]
            if task["name"] == "Verify fleet-local share from scripted-cpu"
        )
        self.assertEqual(smoke["when"], "sup_behavior == 'scripted-cpu'")
        self.assertIn("Resolve share DNS", [task["name"] for task in smoke["block"]])
        self.assertIn(
            "Remove local smoke files",
            [task["name"] for task in self.client_tasks()[0]],
        )

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
