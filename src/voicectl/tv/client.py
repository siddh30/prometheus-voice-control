"""The TV, over the Android TV Remote protocol (the one the Google TV phone app uses). Keys and app launches only.

The TV's address and the pairing key are per-computer settings: `TV_HOST` in `.env` and `TV_CERT_DIR` (default
`~/.voicectl/tv`), never in the repo. Pair once with `scripts/tv_pair.py`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from voicectl.tv.commands import TvCommand

log = logging.getLogger(__name__)

KEY_GAP_S = 0.12             # between repeated presses (volume steps)
MAX_VOLUME_PRESSES = 100     # the most presses "set volume to N" will make
CONNECT_TIMEOUT_S = 8


class TvUnavailable(Exception):
    """The TV cannot be reached or is not paired. The message is safe to say aloud."""


def cert_dir() -> Path:
    return Path(os.environ.get("TV_CERT_DIR") or Path.home() / ".voicectl" / "tv").expanduser()


class TvClient:
    def __init__(self, host: str | None = None, directory: Path | None = None) -> None:
        self.host = host if host is not None else os.environ.get("TV_HOST", "")
        self.dir = directory or cert_dir()
        self._remote = None

    async def _connected(self):
        if self._remote is not None:
            return self._remote
        if not self.host:
            raise TvUnavailable("No TV is set up. Put TV_HOST in the settings file, then pair it.")
        try:
            from androidtvremote2 import AndroidTVRemote, CannotConnect, InvalidAuth
        except ImportError as e:
            raise TvUnavailable("The TV library is not installed.") from e
        cert, key = self.dir / "cert.pem", self.dir / "key.pem"
        if not (cert.exists() and key.exists()):
            raise TvUnavailable("The TV is not paired yet. Run the pairing script once.")
        remote = AndroidTVRemote("voicectl", str(cert), str(key), self.host)
        try:
            await asyncio.wait_for(remote.async_connect(), CONNECT_TIMEOUT_S)
        except InvalidAuth as e:
            raise TvUnavailable("The TV no longer trusts this computer. Pair it again.") from e
        except (CannotConnect, TimeoutError, OSError) as e:
            raise TvUnavailable("I can't reach the TV. It may be off or on another network.") from e
        self._remote = remote
        return remote

    async def run(self, cmd: TvCommand) -> str:
        """Send one allow-listed command and return what to say. Only TvCommand values can get here."""
        if cmd.kind == "wait":
            await asyncio.sleep(min(max(cmd.repeat, 0), 15))
            return ""
        remote = await self._connected()
        try:
            if cmd.kind == "power":
                if (cmd.want == "on") == bool(remote.is_on):
                    return f"The TV is already {cmd.want}."
                remote.send_key_command("POWER")
            elif cmd.kind in ("launch", "link"):
                remote.send_launch_app_command(cmd.package)
            elif cmd.kind == "volume_to":
                await self._volume_to(remote, cmd.repeat)
            else:
                for i in range(cmd.repeat):
                    if i:
                        await asyncio.sleep(KEY_GAP_S)
                    remote.send_key_command(cmd.key)
        except Exception as e:                              # a dropped link: forget it, so the next command reconnects
            self.close()
            raise TvUnavailable("I lost the TV. Try again.") from e
        log.info("tv command: %s %s x%d", cmd.kind, cmd.key or cmd.package, cmd.repeat)     # never the address
        return cmd.label

    async def _volume_to(self, remote, target: int) -> None:
        """Press volume up or down until the TV reports `target`, reading the level after every press."""
        for _ in range(20):                                  # the TV reports its level a moment after connecting
            if remote.volume_info:
                break
            await asyncio.sleep(0.1)
        else:
            raise TvUnavailable("I can't read the TV's volume.")
        stuck = 0
        for _ in range(MAX_VOLUME_PRESSES):
            level = remote.volume_info["level"]
            if level == target:
                return
            remote.send_key_command("VOLUME_UP" if level < target else "VOLUME_DOWN")
            await asyncio.sleep(KEY_GAP_S + 0.05)
            stuck = stuck + 1 if remote.volume_info["level"] == level else 0
            if stuck >= 3:                                   # muted, or the TV ignores the key: stop rather than keep pressing
                return

    def close(self) -> None:
        if self._remote is not None:
            try:
                self._remote.disconnect()
            finally:
                self._remote = None
