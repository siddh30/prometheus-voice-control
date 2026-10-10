from __future__ import annotations

import subprocess
import time

from pynput.keyboard import Controller, Key


class Typer:
    """Types text into whatever app has focus. `clipboard` pastes with Cmd+V (reliable for
    Unicode) and restores the previous text clipboard; `keys` sends synthetic key events."""

    def __init__(self, method: str = "clipboard", restore_clipboard: bool = True) -> None:
        self.method = method
        self.restore_clipboard = restore_clipboard
        self._kb = Controller()

    def type(self, text: str) -> None:
        if not text:
            return
        if self.method == "keys":
            self._kb.type(text)
        else:
            self._paste(text)

    def _paste(self, text: str) -> None:
        previous = subprocess.run(["pbpaste"], capture_output=True).stdout if self.restore_clipboard else None
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        with self._kb.pressed(Key.cmd):
            self._kb.press("v")
            self._kb.release("v")
        if previous is not None:
            time.sleep(0.25)  # let the target app read the clipboard before we restore it
            subprocess.run(["pbcopy"], input=previous, check=True)


class AppTyper:
    """Asks the Voice Control app to paste (it holds the Accessibility grant). Used when VOICECTL_HOTKEYS=app."""

    def __init__(self, bus) -> None:
        self.bus = bus

    def type(self, text: str) -> None:
        if text:
            self.bus.publish("paste", text=text)
