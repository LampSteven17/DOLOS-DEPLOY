import os
from selenium import webdriver

from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.firefox import GeckoDriverManager

from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

from .base_driver import BaseDriverHelper

class WebDriverHelper(BaseDriverHelper):

    def __init__(self):

        if (os.path.exists("geckodriver")):
            try:
                DRIVER_NAME = 'geckowebdriver' 
                self.options = webdriver.FirefoxOptions()
                self.options.add_argument("--headless=new")

                super().__init__(name=DRIVER_NAME)
                self._driver_path = FirefoxService(executable_path="geckodriver")
                self._driver = webdriver.Firefox(service=self._driver_path, options=self.options) 
            
            
            except Exception as e:
                print(e)
                print("FOUND GECKO DRIVER, BUT FAILED TO INSTANTIATE WEBDRIVER")
                return False

        else:
            print("USING CHROME FOR MAIN WEBDRIVER")
            try:
                DRIVER_NAME = 'chromewebdriver'
                self.options = webdriver.ChromeOptions()
                self.options.add_argument("--headless=new")
                

                super().__init__(name=DRIVER_NAME)
                self._driver_path = ChromeService(ChromeDriverManager().install())
                self._driver = webdriver.Chrome(service=self._driver_path, options=self.options)
            
            except Exception as e:
                print(e)
                print("CHROME NOT FOUND OR INSTALLED PROPERLY")
                return False




    @property
    def driver(self):
        return self._driver

    def cleanup(self):
        self._driver.quit()

    """ PRIVATE """

    def check_valid_driver_connection(self):
        try:
            driver = webdriver.Firefox(self._driver_path)
            driver.quit()
            return True
        except Exception as e:
            print('Could not load driver: %s'.format(e))
            return False
