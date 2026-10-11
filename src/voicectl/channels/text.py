from __future__ import annotations

import time
from collections.abc import Callable


class TextChannel:
    """Typing. While `exclusive` (the type box is open) the voice channel hears nothing at all; closing the box
    brings voice back. It also remembers when you last typed, so mic warnings stay quiet while you use the keyboard."""

    QUIET_WINDOW_S = 300

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.exclusive = False
        self.last_typed = 0.0

    def note_typed(self) -> None:
        self.last_typed = self._clock()

    def recently_used(self) -> bool:
        return self._clock() - self.last_typed < self.QUIET_WINDOW_S
