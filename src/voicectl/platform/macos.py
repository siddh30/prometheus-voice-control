from __future__ import annotations

import asyncio
import subprocess


class MacOS:
    def run(self, cmd: list[str], *, input: bytes | None = None, text: bool = True, timeout: float | None = None):
        return subprocess.run(cmd, capture_output=True, text=text, input=input, timeout=timeout)

    def osascript(self, script: str):
        return self.run(["osascript", "-e", script])

    async def osascript_async(self, script: str, *args: str, timeout: float = 25.0) -> tuple[int, str, str]:
        """-> (return code, stdout, stderr). Raises asyncio.TimeoutError (after killing the process) if it takes too long."""
        proc = await asyncio.create_subprocess_exec("osascript", "-e", script, *args,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return proc.returncode, out.decode(), err.decode()

    def clipboard_read(self) -> bytes:
        return subprocess.run(["pbpaste"], capture_output=True).stdout

    def clipboard_write(self, data: bytes) -> None:
        subprocess.run(["pbcopy"], input=data, check=True)

    def audio_devices_json(self) -> str:
        return subprocess.run(["system_profiler", "SPAudioDataType", "-json"], capture_output=True, text=True, timeout=20).stdout

    def process_rss_kb(self, pid: int) -> int:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return int(out) if out.isdigit() else 0

    def find_folders(self, name: str, under: str) -> list[str]:
        return subprocess.run(["mdfind", "-onlyin", under, f'kMDItemFSName == "{name}"c && kMDItemContentType == "public.folder"'],
                              capture_output=True, text=True).stdout.splitlines()

    def open_application(self, app: str, target: str) -> None:
        subprocess.run(["open", "-a", app, target], check=False)

    def to_wav16k(self, src: str, dst: str) -> None:
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, dst], check=True, capture_output=True, timeout=10)

    async def start_playback(self, path: str):
        """Begin playing a sound file; returns a process-like object with wait() and terminate()."""
        return await asyncio.create_subprocess_exec("afplay", path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def say_to_file(self, text: str, path: str) -> None:
        p = await asyncio.create_subprocess_exec("say", "-o", path, text)
        await p.wait()
