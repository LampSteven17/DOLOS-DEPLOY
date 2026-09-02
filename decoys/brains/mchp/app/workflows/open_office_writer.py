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


def _get_paragraph():
    """Get a paragraph - uses LLM for M4/M5, TextLorem for M1."""
    if _use_llm_augmentation():
        from augmentations.content import llm_paragraph
        return llm_paragraph()
    return TextLorem().paragraph()


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


WORKFLOW_NAME = 'DocumentEditor'
WORKFLOW_DESCRIPTION = 'Create documents with LibreOffice Writer (Linux) or OpenOffice Writer (Windows)'
DEFAULT_WAIT_TIME = 2
OPEN_OFFICE_PATH = r"C:\Program Files (x86)\OpenOffice 4\program\soffice"
LIBREOFFICE_CMD = "libreoffice"

def load():
    return DocumentEditor()

class DocumentEditor(BaseWorkflow):

    def __init__(self, default_wait_time=DEFAULT_WAIT_TIME, open_office_path=OPEN_OFFICE_PATH):
        super().__init__(name=WORKFLOW_NAME, description=WORKFLOW_DESCRIPTION)
        self.default_wait_time = default_wait_time
        self.open_office_path = open_office_path
        self._process = None
        self._profile_dir = None

    def action(self, extra=None, logger=None):
        self._create_document(logger=logger)

    def create_assigned(self, resource, workspace, logger=None):
        """Create one exact assigned document through LibreOffice Writer."""
        artifact = Path(workspace) / resource["filename"]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if logger:
            logger.step_start(
                "open_application", category="office", message="LibreOffice Writer"
            )
        self._new_document(artifact)
        pyautogui.hotkey("ctrl", "home")
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")
        if logger:
            logger.step_success("open_application")

        if logger:
            logger.step_start(
                "edit_content", category="office", message="Typing assigned content"
            )
        pyautogui.write(str(resource["title"]), interval=0.01)
        pyautogui.press("enter", presses=2)
        for heading, values in resource["sections"].items():
            pyautogui.write(str(heading), interval=0.01)
            pyautogui.press("enter")
            for value in values:
                pyautogui.write(str(value), interval=0.01)
                pyautogui.press("enter")
        sleep(self.default_wait_time)
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

    def _create_document(self, logger=None):
        app_name = "LibreOffice Writer" if IS_LINUX else "OpenOffice Writer"

        if logger:
            logger.step_start("open_application", category="office",
                              message=app_name)
        self._new_document()
        if logger:
            logger.step_success("open_application")

        if logger:
            logger.step_start("edit_content", category="office",
                              message="Typing paragraphs and sentences")
        # Type random paragraphs and sentences
        for i in range(0, random.randint(2,10)):
            random.choice([pyautogui.typewrite(_get_paragraph()), pyautogui.typewrite(_get_sentence())])
            pyautogui.press('enter')
        sleep(self.default_wait_time)
        if logger:
            logger.step_success("edit_content")

        if logger:
            logger.step_start("edit_content", category="office",
                              message="Performing random document actions")
        # Randomly perform actions
        for i in range(0, random.randint(6,15)):
            random.choice([self._save_pdf,
                           self._write_sentence,
                           self._write_paragraph,
                           self._copy_paste,
                           self._insert_comment,
                           self._find,
                           self._delete_text,
                           self._format_text])()
            sleep(self.default_wait_time)
        if logger:
            logger.step_success("edit_content")

        if logger:
            logger.step_start("save_document", category="office",
                              message="Saving and closing document")
        # Save and quit the document
        self._save_quit()
        if logger:
            logger.step_success("save_document")

    def _insert_comment(self):
        pyautogui.hotkey('ctrl', 'alt', 'c') # insert comment
        pyautogui.typewrite(_get_sentence()) # type random sentence
        pyautogui.press('esc') # finish commenting
        sleep(self.default_wait_time)

    def _find(self):
        pyautogui.hotkey('ctrl', 'f') # open Find & Replace
        sleep(self.default_wait_time)
        pyautogui.typewrite(_get_word()) # type random word
        sleep(self.default_wait_time)
        pyautogui.press('enter')
        sleep(self.default_wait_time)
        pyautogui.hotkey('alt','y') # close pop up box that may appear
        sleep(self.default_wait_time)
        pyautogui.hotkey('alt','c') # close Find & Replace
        sleep(self.default_wait_time)

    def _copy_paste(self):
        self._select_text()
        sleep(self.default_wait_time)
        pyautogui.hotkey('ctrl', 'c') # copy to clipboard
        sleep(self.default_wait_time)
        pyautogui.press('backspace') # delete text
        sleep(self.default_wait_time)
        pyautogui.typewrite(_get_paragraph()) # write text
        sleep(self.default_wait_time)
        pyautogui.press('enter') # insert new line
        pyautogui.press('enter') # insert new line
        pyautogui.hotkey('ctrl', 'v') # paste from clipboard
        sleep(self.default_wait_time)

    def _select_text(self):
        selection_params = [
            ['ctrl'  , 'home'], # go to beginning of document
            ['shift' , 'left'], # move cursor & select to left
            ['shift' , 'up'] # move cursor & select up
        ]
        pyautogui.hotkey(*random.choice(selection_params))

    def _format_text(self):
        self._select_text()
        sleep(self.default_wait_time)
        formatting_params = [['ctrl','1'], # Apply heading 1 style
                             ['ctrl','2'], # Apply heading 2 style
                             ['ctrl','3'], # Apply heading 3 style
                             ['ctrl','d'], # Double underline
                             ['ctrl','e'], # Center
                             ['ctrl','5']] # Set 1.5 line spacing
        pyautogui.hotkey(*random.choice(formatting_params))
        sleep(self.default_wait_time)

    def _delete_text(self):
        pyautogui.hotkey('ctrl', 'shift', 'delete') # Delete text to beginning of line
        pyautogui.hotkey('ctrl', 'backspace') # Delete text to beginning of word

    def _save_pdf(self):
        # Export a pdf
        pyautogui.hotkey('alt','f') # select to File
        pyautogui.hotkey('alt','d') # select to Export as PDF
        pyautogui.press('enter') # choose Export as PDF
        pyautogui.hotkey('alt','x') # choose Export
        pyautogui.typewrite(_get_filename()) # type random file name
        sleep(self.default_wait_time)
        pyautogui.press('enter') # press enter
        sleep(self.default_wait_time)
        pyautogui.hotkey('alt','y') # choose "yes" if a popup asks if you'd like to overwrite another file

    def _new_document(self, artifact=None):
        if IS_LINUX:
            self._profile_dir = Path(tempfile.mkdtemp(prefix="ruse-lo-writer-"))
            self._process = subprocess.Popen(
                [
                    LIBREOFFICE_CMD,
                    f"-env:UserInstallation={self._profile_dir.resolve().as_uri()}",
                    "--writer",
                    "--norestore",
                    "--nofirststartwizard",
                    "private:factory/swriter",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            wait_for_focused_window(
                "LibreOffice Writer",
                process=self._process,
                artifact=artifact,
                blocking_dialog_action=self._dismiss_tip_dialog,
            )
        else:
            # Windows: Use OpenOffice start menu
            os.startfile(self.open_office_path)
            sleep(self.default_wait_time)
            pyautogui.press('d')  # choose document editing
            sleep(self.default_wait_time)

    @staticmethod
    def _dismiss_tip_dialog():
        pyautogui.press("esc")

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
        # Always open Save As for a new assigned document, explicitly replace
        # the filename field, and give Writer time to flush the ODT before the
        # process is closed and the strict validator opens it.
        pyautogui.hotkey("ctrl", "shift", "s")
        sleep(self.default_wait_time)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.write(str(artifact), interval=0.01)
        pyautogui.press("enter")
        wait_for_stable_artifact(Path(artifact))
        pyautogui.hotkey("ctrl", "q")
        sleep(self.default_wait_time)

    def _write_paragraph(self):
        pyautogui.typewrite(_get_paragraph())

    def _write_sentence(self):
        pyautogui.typewrite(_get_sentence())

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
