from __future__ import annotations

import asyncio
from collections.abc import Callable

from pynput import keyboard


def _parse(name: str):
    return getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)


class HotkeyListener:
    """Push-to-talk: calls on_down(mode)/on_up(mode) on the asyncio loop. Key-repeat is ignored."""

    def __init__(self, loop: asyncio.AbstractEventLoop, mode_keys: dict[str, str], cancel_key: str,
                 on_down: Callable[[str], None], on_up: Callable[[str], None],
                 on_cancel: Callable[[], None]) -> None:
        self.loop = loop
        self.key_to_mode = {_parse(k): m for m, k in mode_keys.items()}
        self.cancel = _parse(cancel_key)
        self.on_down, self.on_up, self.on_cancel = on_down, on_up, on_cancel
        self._held: set = set()
        self._ctrl = False                    # mode keys are Control+key, like the app's keys
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def _press(self, key) -> None:
        if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl = True
        elif key == self.cancel:
            self.loop.call_soon_threadsafe(self.on_cancel)
        elif self._ctrl and key in self.key_to_mode and key not in self._held:
            self._held.add(key)
            self.loop.call_soon_threadsafe(self.on_down, self.key_to_mode[key])

    def _release(self, key) -> None:
        if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl = False
        if key in self._held:
            self._held.discard(key)
            self.loop.call_soon_threadsafe(self.on_up, self.key_to_mode[key])
