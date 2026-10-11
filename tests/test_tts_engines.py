import pathlib
import re

from voicectl import platform
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.tts.engines import SystemEngine, engine_for, EdgeEngine
from voicectl.tts.speaker import Speaker


class FakeEngine:
    ext, cacheable = "mp3", True

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def synth(self, text, path):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("offline")
        path.write_bytes(b"audio")
        return path


class FakeOS:
    def __init__(self): self.said = []
    async def say_to_file(self, text, path): self.said.append(text); pathlib.Path(path).write_bytes(b"aiff")


def test_the_config_picks_the_engine():
    assert isinstance(engine_for(Config({"tts": {"backend": "edge"}})), EdgeEngine)
    assert isinstance(engine_for(Config({"tts": {"backend": "say"}})), SystemEngine)


async def test_short_phrases_are_synthesised_once_then_played_from_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(Speaker, "CACHE", tmp_path / "cache")
    engine = FakeEngine()
    sp = Speaker(Config({"tts": {"voice": "v"}}), EventBus(), prefs_path=tmp_path / "p.json", engine=engine)
    first = await sp._synth("Opening LinkedIn.", tmp_path)
    second = await sp._synth("Opening LinkedIn.", tmp_path)
    assert first == second and engine.calls == ["Opening LinkedIn."]


async def test_when_the_online_voice_fails_the_system_voice_speaks_instead(tmp_path, monkeypatch):
    monkeypatch.setattr(Speaker, "CACHE", tmp_path / "cache")
    fake_os = FakeOS()
    platform.set_platform(fake_os)
    sp = Speaker(Config({}), EventBus(), prefs_path=tmp_path / "p.json", engine=FakeEngine(fail=True))
    out = await sp._synth("Hello there.", tmp_path)
    assert out.suffix == ".aiff" and fake_os.said == ["Hello there."]


def test_the_online_voice_library_is_imported_only_by_its_engine():
    offenders = [str(p) for p in pathlib.Path("src/voicectl").rglob("*.py")
                 if p.name != "engines.py" and re.search(r"^\s*(from|import) edge_tts\b", p.read_text(), re.M)]
    assert not offenders, offenders
