from __future__ import annotations

import time
import unittest
from pathlib import Path

import yaml

from deployment_engine.core.ansible_runner import _LineParser


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "INSTALL_SUP.sh"
INSTALL_PLAYBOOK = (
    ROOT / "deployment_engine" / "playbooks" / "decoy" / "install-sups.yaml"
)


class InstallerSimplificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = INSTALLER.read_text(encoding="utf-8")

    def _pip_line(self, marker: str) -> str:
        return next(
            line.strip()
            for line in self.source.splitlines()
            if marker in line and "pip install" in line
        )

    def test_cpu_controls_have_no_model_or_gpu_install_path(self):
        self.assertIn('["scripted-cpu"]="scripted:none:none:none"', self.source)
        self.assertIn('["mchp-cpu"]="mchp:none:none:none"', self.source)
        self.assertIn('if [[ "$MODEL" != "none" ]]; then\n        install_nvidia_driver', self.source)
        self.assertIn('if [[ "$MODEL" != "none" ]]; then\n            install_ollama', self.source)

    def test_gpu_controls_retain_driver_reboot_ollama_and_pinned_model(self):
        self.assertIn('["browseruse-gpu"]="browseruse:none:gemma:none"', self.source)
        self.assertIn('["smolagents-gpu"]="smolagents:none:gemma:none"', self.source)
        self.assertIn('["gemma"]="gemma4:26b"', self.source)
        self.assertIn("nvidia-driver-580", self.source)
        self.assertIn("NVIDIA_DRIVER_INSTALLED=true", self.source)
        self.assertIn("exit 100", self.source)
        self.assertIn("install_ollama", self.source)

        play = yaml.safe_load(INSTALL_PLAYBOOK.read_text(encoding="utf-8"))[0]
        reboot = next(
            task
            for task in play["tasks"]
            if task["name"] == "Reboot for NVIDIA drivers (exit code 100)"
        )
        self.assertEqual(reboot["when"], "stage1_result.rc == 100")

    def test_cuda_toolkit_and_duplicate_uv_installation_are_absent(self):
        self.assertNotIn("cuda-toolkit", self.source)
        self.assertNotIn("/usr/local/cuda", self.source)
        self.assertNotIn("uvx", self.source)
        self.assertNotIn("astral.sh/uv", self.source)
        browser = self._pip_line("browser-use==0.12.7")
        self.assertNotIn(" uv ", f" {browser} ")
        self.assertIn("playwright install --with-deps chromium", self.source)
        self.assertNotIn("playwright install-deps chromium", self.source)

    def test_explicit_python_dependencies_match_runtime_import_audit(self):
        mchp = self._pip_line("beautifulsoup4")
        for required in ("selenium", "beautifulsoup4", "lxml", "pyautogui", "lorem", "requests"):
            self.assertIn(required, mchp)
        for removed in (
            "webdriver-manager",
            "certifi",
            "chardet",
            "colorama",
            "configparser",
            "crayons",
            "idna",
            "urllib3",
        ):
            self.assertNotIn(removed, mchp)

        smol = self._pip_line("smolagents==1.25.0")
        for required in ("litellm", "requests", "markdownify", "ddgs", "yt-dlp"):
            self.assertIn(required, smol)
        for removed in ("torch", "transformers", "datasets", "numpy", "pandas", "duckduckgo-search"):
            self.assertNotIn(removed, smol)

        browser = self._pip_line("browser-use==0.12.7")
        self.assertIn("langchain-ollama", browser)
        self.assertIn("playwright", browser)

    def test_skipped_reboot_does_not_emit_active_reboot_step(self):
        parser = _LineParser(time.time())
        self.assertIsNone(
            parser.parse("TASK [Reboot for NVIDIA drivers (exit code 100)] ****")
        )
        self.assertIsNone(parser.parse("skipping: [d-cpu-0]"))

        parser = _LineParser(time.time())
        self.assertIsNone(
            parser.parse("TASK [Reboot for NVIDIA drivers (exit code 100)] ****")
        )
        event = parser.parse("changed: [d-gpu-0]")
        self.assertEqual(event.kind, "host_ok")
        self.assertEqual(event.task, "Rebooting VM for NVIDIA drivers")

    def test_installer_uses_existing_ansible_and_service_logs_only(self):
        for forbidden in ("timing.json", "install-timing", "provisioning-cache"):
            self.assertNotIn(forbidden, self.source)

    def test_canonical_installs_disable_automatic_apt_before_manual_apt(self):
        units = (
            "apt-daily.service",
            "apt-daily.timer",
            "apt-daily-upgrade.service",
            "apt-daily-upgrade.timer",
            "unattended-upgrades.service",
        )
        play = yaml.safe_load(INSTALL_PLAYBOOK.read_text(encoding="utf-8"))[0]
        tasks = play["tasks"]
        names = [task["name"] for task in tasks]
        disable = tasks[names.index(
            "Stop and mask automatic APT activity for canonical SUPs"
        )]
        self.assertEqual(
            disable["when"], "sup_behavior in canonical_workflow_configs"
        )
        self.assertEqual(disable["command"]["argv"][:3], [
            "systemctl", "mask", "--now",
        ])
        self.assertEqual(tuple(disable["command"]["argv"][3:]), units)
        self.assertLess(
            names.index("Stop and mask automatic APT activity for canonical SUPs"),
            names.index("Update apt cache"),
        )

        install_agent = self.source.index("install_agent() {")
        disable_call = self.source.index(
            "        disable_automatic_apt", install_agent
        )
        first_manual_apt = self.source.index(
            "        install_system_deps", install_agent
        )
        self.assertLess(disable_call, first_manual_apt)

    def test_manual_apt_remains_and_automatic_apt_is_verified_before_start(self):
        for command in (
            "sudo apt-get update -y",
            "sudo apt-get install -y python3-pip",
            'sudo systemctl mask --now "${AUTOMATIC_APT_UNITS[@]}"',
        ):
            self.assertIn(command, self.source)

        play = yaml.safe_load(INSTALL_PLAYBOOK.read_text(encoding="utf-8"))[0]
        tasks = play["tasks"]
        names = [task["name"] for task in tasks]
        verify_name = (
            "Verify automatic APT activity is disabled before service startup"
        )
        verify = tasks[names.index(verify_name)]
        self.assertEqual(
            verify["when"], "sup_behavior in canonical_workflow_configs"
        )
        self.assertIn('"$enabled_state" != masked', verify["shell"])
        self.assertIn('"$active_state" != inactive', verify["shell"])
        self.assertLess(
            names.index(verify_name),
            names.index("Start canonical workflow service after Stage 2"),
        )

        install_agent = self.source.index("install_agent() {")
        verify_call = self.source.index(
            "            verify_automatic_apt_disabled", install_agent
        )
        service_start = self.source.index(
            '            sudo systemctl start "${service_name}.service"',
            install_agent,
        )
        self.assertLess(verify_call, service_start)

    def test_all_four_canonical_paths_share_apt_suppression(self):
        play = yaml.safe_load(INSTALL_PLAYBOOK.read_text(encoding="utf-8"))[0]
        self.assertEqual(
            tuple(play["vars"]["canonical_workflow_configs"]),
            (
                "scripted-cpu",
                "mchp-cpu",
                "browseruse-gpu",
                "smolagents-gpu",
            ),
        )
        for config in play["vars"]["canonical_workflow_configs"]:
            self.assertIn(f'["{config}"]=', self.source)


if __name__ == "__main__":
    unittest.main()
