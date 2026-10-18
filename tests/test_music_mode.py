import asyncio
import json

from voicectl.agents.runner import Finished
from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes.base import ModeContext
from voicectl.modes.prometheus import PrometheusMode


class FakeBrowser:
    def __init__(self):
        self.last_title, self.has = "Queen – Bohemian Rhapsody - YouTube", True
        self.state = {"playing": True, "muted": False, "at": 5, "of": 359}
        self.calls = []
    def has_page(self): return self.has
    async def media(self, action):
        self.calls.append(action)
        if action == "pause":
            self.state = {**self.state, "playing": False}
        if action == "play":
            self.state = {**self.state, "playing": True}
        return json.dumps(self.state) if self.state is not None else "NO_MEDIA"


class FakeRunner:
    mode_name = "x"; pending_approval = False; played_media = False
    def __init__(self, browser): self.browser = browser
    async def stream_turn(self, text, context="", hints="", complexity="", publish_result=True): yield Finished("Playing it now.")
    async def warm_up(self): ...
    async def interrupt(self): ...
    def status(self): return ""


class Spk:
    active = False
    def __init__(self): self.said = []
    async def speak(self, t): self.said.append(t)
    async def speak_many(self, chunks, **kw):
        async for c in chunks: self.said.append(c)
    def stop(self): ...
    def warm_output(self): ...
    def quiet_for(self): return 99.0


def make():
    cfg = Config({"tts": {"enabled": True}})
    bus = EventBus()
    ctx = ModeContext(cfg, bus, Spk())
    browser = FakeBrowser()
    ctx.shared["agent_runner"] = FakeRunner(browser)
    m = PrometheusMode(ctx)
    events = []
    bus.publish = lambda t, **d: events.append((t, d))
    return m, browser, events, ctx


async def test_an_audio_request_that_ends_with_a_song_playing_enters_music_mode():
    m, browser, events, ctx = make()
    assert await m._agent_turn("play bohemian rhapsody on youtube") == "Playing it now."
    assert m.music and m.holding()                                              # the music is NOT stopped
    assert "pause" not in browser.calls
    assert ("music", {"on": True, "title": "Queen – Bohemian Rhapsody - YouTube"}) in events
    assert ("state", {"state": "music", "mode": "prometheus"}) in events
    m._music_watch.cancel()


async def test_sound_that_merely_happens_to_be_playing_is_not_music_mode():
    m, browser, events, ctx = make()                                       # playing + unmuted + long, but nobody asked for audio
    await m._agent_turn("find product manager jobs on linkedin")
    assert m.music is None and browser.calls == []                         # not even checked


async def test_the_agent_pressing_play_counts_as_an_audio_request_even_without_the_word_play():
    m, browser, events, ctx = make()
    m._runner().played_media = True
    await m._agent_turn("Obvious by Chris Brown")
    assert m.music is not None
    m._music_watch.cancel()


async def test_after_a_command_during_music_the_widget_returns_to_playing_not_idle():
    m, browser, events, ctx = make()
    await m._agent_turn("play it"); m._music_watch.cancel()
    m._state("thinking")                                                    # the command's turn moves the widget off "playing"...
    events.clear()
    m._turn = asyncio.create_task(m._run_turn(None, None, "Done."))
    await asyncio.sleep(0.05)
    states = [d["state"] for t, d in events if t == "state"]
    assert states and states[-1] == "music"                                 # ...and when it ends it returns to "playing", not idle


async def test_when_the_song_ends_the_widget_goes_back_to_idle_by_itself(monkeypatch):
    import voicectl.modes.prometheus as pm
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pm.asyncio, "sleep", lambda s: real_sleep(0.01))
    m, browser, events, ctx = make()
    await m._agent_turn("play it")
    assert m.music
    browser.state = {**browser.state, "playing": False}                    # ended, or you paused it yourself
    await real_sleep(0.2)
    assert m.music is None and ctx.shared["music_playing"] is False
    assert ("state", {"state": "idle", "mode": "prometheus"}) in events


async def test_a_muted_or_short_clip_is_not_music_even_after_an_audio_request():
    for state in ({"playing": True, "muted": True, "at": 1, "of": 300},     # autoplaying muted preview
                  {"playing": True, "muted": False, "at": 1, "of": 12},     # a short loop / ad
                  {"playing": False, "muted": False, "at": 0, "of": 300},   # paused
                  None):                                                    # no media element at all
        m, browser, events, ctx = make()
        browser.state = state
        await m._agent_turn("play something")
        assert m.music is None


async def test_no_page_open_means_no_check_and_no_blank_tab_is_created():
    m, browser, events, ctx = make()
    browser.has = False
    await m._agent_turn("play some music")
    assert browser.calls == [] and m.music is None


async def test_music_mode_keeps_the_conversation_open_instead_of_closing_it():
    m, browser, events, ctx = make()
    await m._agent_turn("play it")
    assert m.holding()                          # ConversationMode.run() does not close an idle conversation while a song plays
    m._music_watch.cancel()


def test_emoji_never_reach_the_captions():
    from voicectl.tts.speaker import clean_for_speech, strip_emoji
    assert strip_emoji("Enjoy the song 🎵 😊!") == "Enjoy the song  !"
    assert "note" not in clean_for_speech("Playing it now 🎵").lower() and "face" not in clean_for_speech("Done 😊").lower()


async def test_pressing_the_key_during_music_records_as_usual_and_never_pauses_the_song():
    m, browser, events, ctx = make()
    await m._agent_turn("play it"); m._music_watch.cancel()
    m._on_record_start()                                                    # Ctrl+F10: the recorder starts
    assert "pause" not in browser.calls and m.music is not None             # the song keeps playing
    assert ("state", {"state": "listening", "mode": "prometheus"}) in events
