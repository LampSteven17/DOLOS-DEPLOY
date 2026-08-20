import os
from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService

from .base_driver import BaseDriverHelper


class WebDriverUnavailableError(Exception):
    """Raised when Firefox WebDriver cannot be initialized."""
    pass


class WebDriverHelper(BaseDriverHelper):
    """Firefox-only WebDriver helper for MCHP workflows."""

    def __init__(self):
        DRIVER_NAME = 'geckowebdriver'
        self._driver = None

        # Look for geckodriver in current dir or PATH
        geckodriver_path = self._find_geckodriver()
        if not geckodriver_path:
            raise WebDriverUnavailableError(
                "geckodriver not found. Install Firefox and geckodriver, "
                "or place geckodriver in the working directory."
            )

        try:
            self.options = webdriver.FirefoxOptions()
            # Run inside Xvfb (xvfb-run -a in INSTALL_SUP.sh) instead of
            # headless — headless silences service workers, push channels,
            # third-party trackers, and most page-level ambient traffic.
            self.options.add_argument("--no-sandbox")
            self.options.add_argument("--disable-dev-shm-usage")
            # Reduce startup overhead
            self.options.set_preference("browser.sessionstore.resume_from_crash", False)
            self.options.set_preference("browser.shell.checkDefaultBrowser", False)
            self.options.set_preference("browser.startup.homepage_override.mstone", "ignore")
            # Allow video autoplay so BrowseYouTube actually streams the video
            # (Firefox blocks autoplay by default -> the player sits unstarted and
            # no media flows; confirmed 2026-06-04 the pref makes it play).
            self.options.set_preference("media.autoplay.default", 0)
            self.options.set_preference("media.autoplay.blocking_policy", 0)

            super().__init__(name=DRIVER_NAME)
            self._driver_path = FirefoxService(executable_path=geckodriver_path)
            self._driver = webdriver.Firefox(service=self._driver_path, options=self.options)

        except Exception as e:
            raise WebDriverUnavailableError(f"Firefox WebDriver failed to initialize: {e}")

    def _find_geckodriver(self):
        """Find geckodriver in current directory or PATH."""
        # Check current directory
        if os.path.exists("geckodriver"):
            return "geckodriver"
        # Check common paths
        for path in ["/usr/local/bin/geckodriver", "/usr/bin/geckodriver"]:
            if os.path.exists(path):
                return path
        # Check if it's in PATH (will work if geckodriver is installed system-wide)
        import shutil
        return shutil.which("geckodriver")

    @property
    def driver(self):
        return self._driver

    def cleanup(self):
        try:
            if self._driver:
                self._driver.quit()
        finally:
            self._driver = None
            # BaseDriverHelper is a legacy singleton. A quit WebDriver cannot be
            # reused by the next canonical scheduled workflow.
            type(self)._instances.pop(type(self), None)

    def check_valid_driver_connection(self):
        try:
            driver = webdriver.Firefox(service=self._driver_path, options=self.options)
            driver.quit()
            return True
        except Exception as e:
            print(f'Could not load driver: {e}')
            return False
