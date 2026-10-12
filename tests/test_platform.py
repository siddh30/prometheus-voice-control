import pathlib
import re
import types

from voicectl import platform


class FakeOS:
    """Records what would have happened on the machine; no real commands run."""

    def __init__(self):
        self.calls = []
        self.clip = b"before"

    def run(self, cmd, **k):
        self.calls.append(("run", cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def osascript(self, script):
        self.calls.append(("osascript", script))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def clipboard_read(self): self.calls.append(("clip_read",)); return self.clip
    def clipboard_write(self, data): self.calls.append(("clip_write", data)); self.clip = data
    def process_rss_kb(self, pid): return 2048
    def find_folders(self, name, under): return [f"{under}/Work/{name}", f"{under}/Library/x/{name}", f"{under}/{name}"]
    def open_application(self, app, target): self.calls.append(("open", app, target))


def with_fake(monkeypatch):
    fake = FakeOS()
    platform.set_platform(fake)
    return fake


def test_actions_reach_the_os_only_through_the_platform(monkeypatch):
    fake = with_fake(monkeypatch)
    try:
        from voicectl.actions._sys import osascript, sh
        assert osascript("set volume output volume 30").ok
        assert sh(["open", "-a", "Safari"]).ok
        assert ("osascript", "set volume output volume 30") in fake.calls and ("run", ["open", "-a", "Safari"]) in fake.calls
    finally:
        platform.set_platform(None)


def test_memory_and_folders_come_from_the_platform_too(monkeypatch):
    fake = with_fake(monkeypatch)
    try:
        from voicectl.core.maintenance import rss_mb
        assert rss_mb() == 2.0
        from voicectl.actions.open_folder import _spotlight
        assert str(_spotlight("Work")).endswith("/Work")                 # filters ~/Library, prefers the shortest path
    finally:
        platform.set_platform(None)


def test_pasting_restores_the_clipboard_through_the_platform(monkeypatch):
    fake = with_fake(monkeypatch)
    try:
        from voicectl.output.typer import Typer
        t = Typer.__new__(Typer)
        t.restore_clipboard = True
        kb = types.SimpleNamespace(pressed=lambda k: __import__("contextlib").nullcontext(), press=lambda c: None, release=lambda c: None)
        t._kb = kb
        monkeypatch.setattr("time.sleep", lambda s: None)
        t._paste("hello")
        writes = [c[1] for c in fake.calls if c[0] == "clip_write"]
        assert writes == [b"hello", b"before"]                           # typed text in, your old clipboard back
    finally:
        platform.set_platform(None)


def test_nothing_outside_the_platform_package_calls_the_operating_system():
    allowed = pathlib.Path("src/voicectl/platform")
    pattern = re.compile(r"^\s*(import subprocess|from subprocess import)|create_subprocess_exec|subprocess\.(run|Popen|call|check_output)", re.M)
    offenders = [str(p) for p in pathlib.Path("src/voicectl").rglob("*.py") if allowed not in p.parents and pattern.search(p.read_text())]
    assert not offenders, f"call the OS through voicectl.platform instead: {offenders}"
