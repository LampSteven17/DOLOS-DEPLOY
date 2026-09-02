import os
import sys
import random
import subprocess
import tempfile
import pyautogui
from lorem.text import TextLorem
from pathlib import Path
from time import sleep
from ..utility.base_workflow import BaseWorkflow
from ..utility.libreoffice_gui import (
    remove_profile,
    wait_for_focused_window,
    wait_for_stable_artifact,
)


# Platform detection
IS_WINDOWS = sys.platform == 'win32'
IS_LINUX = sys.platform.startswith('linux')


# LLM augmentation - only used for M4/M5 configurations
def _use_llm_augmentation():
    """Check if LLM augmentation should be used (M4/M5 configs)."""
    return os.environ.get("HYBRID_LLM_BACKEND") is not None


def _get_sentence():
    """Get a sentence - uses LLM for M4/M5, TextLorem for M1."""
    if _use_llm_augmentation():
        from augmentations.content import llm_sentence
        return llm_sentence()
    return TextLorem().sentence()


def _get_word():
    """Get a word - uses LLM for M4/M5, TextLorem for M1."""
    if _use_llm_augmentation():
        from augmentations.content import llm_word
        return llm_word()
    return TextLorem()._word()


def _get_filename():
    """Get a filename - uses LLM for M4/M5, TextLorem for M1."""
    if _use_llm_augmentation():
        from augmentations.content import llm_filename
        return llm_filename()
    return TextLorem(wsep='-', srange=(1,3)).sentence()[:-1]


WORKFLOW_NAME = 'SpreadsheetEditor'
WORKFLOW_DESCRIPTION = 'Create spreadsheets with LibreOffice Calc (Linux) or OpenOffice Calc (Windows)'
DEFAULT_WAIT_TIME = 2
OPEN_OFFICE_PATH = r"C:\Program Files (x86)\OpenOffice 4\program\soffice"
LIBREOFFICE_CMD = "libreoffice"

def load():
    return SpreadsheetEditor()

class SpreadsheetEditor(BaseWorkflow):

    def __init__(self, default_wait_time=DEFAULT_WAIT_TIME, open_office_path=OPEN_OFFICE_PATH):
        super().__init__(name=WORKFLOW_NAME, description=WORKFLOW_DESCRIPTION)
        self.default_wait_time = default_wait_time
        self.open_office_path = open_office_path
        self._process = None
        self._profile_dir = None

    def action(self, extra=None, logger=None):
        self._create_spreadsheet(logger=logger)

    def create_assigned(self, resource, workspace, logger=None):
        """Create one exact assigned spreadsheet through LibreOffice Calc."""
        artifact = Path(workspace) / resource["filename"]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if logger:
            logger.step_start(
                "open_application", category="office", message="LibreOffice Calc"
            )
        self._new_spreadsheet(artifact)
        pyautogui.hotkey("ctrl", "home")
        if logger:
            logger.step_success("open_application")

        if logger:
            logger.step_start(
                "edit_content", category="office", message="Typing assigned table"
            )
        rows = [resource["columns"], *resource["rows"]]
        for row_index, row in enumerate(rows, start=1):
            for column_index, value in enumerate(row):
                column = chr(ord("A") + column_index)
                self._set_cell([column, row_index], value)
        if logger:
            logger.step_success("edit_content")

        if logger:
            logger.step_start(
                "save_document", category="office", message=str(artifact)
            )
        self._save_assigned(artifact)
        if logger:
            logger.step_success("save_document")
        return artifact

    def _create_spreadsheet(self, logger=None):
        app_name = "LibreOffice Calc" if IS_LINUX else "OpenOffice Calc"

        # Open application
        if logger:
            logger.step_start("open_application", category="office", message=app_name)
        try:
            self._new_spreadsheet()
            if logger:
                logger.step_success("open_application")
        except Exception as e:
            if logger:
                logger.step_error("open_application", str(e), exception=e)
            raise

        # Navigate and insert table
        if logger:
            logger.step_start("edit_content", category="office", message="Inserting table data")
        try:
            self._move_to_cell([random.choice('abcde'),random.randrange(6)]) # move to random cell, given column & row parameters
            sleep(1)
            self._insert_table()
            sleep(1)
            if logger:
                logger.step_success("edit_content")
        except Exception as e:
            if logger:
                logger.step_error("edit_content", str(e), exception=e)
            raise

        # Insert comment
        if logger:
            logger.step_start("add_comment", category="office", message="Adding cell comment")
        try:
            self._move_to_cell([random.choice('abcdefghijkl'),random.randrange(15)]) # move to random cell, given column & row parameters
            self._insert_comment()
            sleep(3)
            if logger:
                logger.step_success("add_comment")
        except Exception as e:
            if logger:
                logger.step_error("add_comment", str(e), exception=e)
            raise

        # Save and quit
        if logger:
            logger.step_start("save_document", category="office", message="Saving spreadsheet")
        try:
            self._save_quit()
            if logger:
                logger.step_success("save_document")
        except Exception as e:
            if logger:
                logger.step_error("save_document", str(e), exception=e)
            raise

    def _insert_comment(self):
        pyautogui.hotkey('ctrl', 'alt', 'c') # insert comment
        pyautogui.typewrite(_get_sentence()) # type random sentence
        pyautogui.press('esc') # finish commenting
        sleep(self.default_wait_time)

    def _new_spreadsheet(self, artifact=None):
        if IS_LINUX:
            self._profile_dir = Path(tempfile.mkdtemp(prefix="ruse-lo-calc-"))
            self._process = subprocess.Popen(
                [
                    LIBREOFFICE_CMD,
                    f"-env:UserInstallation={self._profile_dir.resolve().as_uri()}",
                    "--calc",
                    "--norestore",
                    "--nofirststartwizard",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            wait_for_focused_window(
                "LibreOffice Calc", process=self._process, artifact=artifact
            )
        else:
            # Windows: Use OpenOffice start menu
            os.startfile(self.open_office_path)
            sleep(self.default_wait_time)
            pyautogui.press('s')  # choose new spreadsheet
            sleep(self.default_wait_time)

    def _save_quit(self):
        pyautogui.hotkey('ctrl', 's') # save
        sleep(self.default_wait_time)
        pyautogui.typewrite(_get_filename()) # type random file name
        sleep(self.default_wait_time)
        pyautogui.press('enter')
        pyautogui.hotkey('alt','y') # choose "yes" if a popup asks if you'd like to overwrite another file
        sleep(self.default_wait_time)
        pyautogui.hotkey('ctrl','q') # quit

    def _save_assigned(self, artifact):
        pyautogui.hotkey("ctrl", "shift", "s")
        sleep(self.default_wait_time)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.write(str(artifact), interval=0.01)
        pyautogui.press("enter")
        wait_for_stable_artifact(Path(artifact))
        pyautogui.hotkey("ctrl", "q")

    def _set_cell(self, cell_coordinate, value):
        self._move_to_cell(cell_coordinate)
        pyautogui.press("f2")
        pyautogui.hotkey("ctrl", "a")
        pyautogui.write(str(value), interval=0.01)
        pyautogui.press("enter")

    def _move_to_cell(self, cell_coordinate):
        # Use Ctrl+G for Go To dialog (works in both LibreOffice and OpenOffice)
        pyautogui.hotkey('ctrl', 'g') if IS_LINUX else pyautogui.press('f5')
        sleep(0.5)
        # Type cell reference directly (e.g., "A1")
        cell_ref = f"{cell_coordinate[0].upper()}{cell_coordinate[1]}"
        pyautogui.typewrite(cell_ref)
        pyautogui.press('enter')
        sleep(0.5)
        pyautogui.press('esc')  # close dialog

    def _insert_table(self):
        row_length = random.randint(3,10)
        for i in range(0, row_length): # create header row for a table
            pyautogui.write(_get_word()) # type a random word
            pyautogui.press('tab')
        for j in range(0, random.randint(3,10)):
            pyautogui.press('enter')
            for k in range(0, row_length):
                pyautogui.write(str(random.randint(0,10000))) # type a random number
                pyautogui.press('tab')

    def cleanup(self):
        """Clean up any running processes."""
        if self._process:
            try:
                self._process.terminate()
                wait = getattr(self._process, "wait", None)
                if wait is not None:
                    wait(timeout=5)
            except Exception:
                kill = getattr(self._process, "kill", None)
                if kill is not None:
                    try:
                        kill()
                    except Exception:
                        pass
            finally:
                self._process = None
        remove_profile(self._profile_dir)
        self._profile_dir = None
