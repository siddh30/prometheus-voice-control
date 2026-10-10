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


class FakeEars:
    paused = False


class Spk:
    active = False; spoken = []; reference = None
    def __init__(self): self.said = []
    async def speak(self, t): self.said.append(t)
    async def speak_many(self, chunks, **kw):
        async for c in chunks: self.said.append(c)
    def stop(self): ...
    def wake(self): ...
    def quiet_for(self): return 99.0


def make():
    cfg = Config({"tts": {"enabled": True}, "conversation": {"output": "headphones"}, "wake_word": {"enabled": False}})
    bus = EventBus()
    ctx = ModeContext(cfg, bus, Spk())
    browser = FakeBrowser()
    ctx.shared["agent_runner"] = FakeRunner(browser)
    m = PrometheusMode(ctx)
    m._ears = FakeEars()
    events = []
    bus.publish = lambda t, **d: events.append((t, d))
    return m, browser, events, ctx


async def test_an_audio_request_that_ends_with_a_song_playing_enters_quiet_name_only_listening():
    m, browser, events, ctx = make()
    assert await m._agent_turn("play bohemian rhapsody on youtube") == "Playing it now."
    assert m.music and m.holding() and not m._ears.paused                  # the music is NOT stopped and the ears stay ON
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


async def test_while_music_plays_lyrics_are_ignored_and_only_the_name_gets_through():
    m, browser, events, ctx = make()
    await m._agent_turn("play it"); m._music_watch.cancel()
    assert await m.filter_utterance("you know how we used to live in the summer") is None      # lyrics: silently ignored
    assert await m.filter_utterance("la la la hold on to me tonight") is None
    assert await m.filter_utterance("Prometheus, skip to the next song") == "skip to the next song"
    assert await m.filter_utterance("Hey Prometheus pause it") == "pause it"
    assert await m.filter_utterance("Prometheus") is None                                       # just the name: acknowledged instead
    assert not any(t == "transcript" for t, _ in events)                                        # ignored speech never touches the screen


async def test_saying_just_the_name_opens_a_short_window_where_no_name_is_needed():
    m, browser, events, ctx = make()
    await m._agent_turn("play it"); m._music_watch.cancel()
    await m.filter_utterance("Prometheus")
    assert await m.filter_utterance("make it louder") == "make it louder"                       # the follow-up needs no name
    assert await m.filter_utterance("la la la") is None                                          # and the window closes after one use


async def test_pressing_the_key_during_music_means_the_next_thing_is_for_me_and_never_pauses():
    m, browser, events, ctx = make()
    await m._agent_turn("play it"); m._music_watch.cancel()
    assert m.interrupt() is True
    assert "pause" not in browser.calls and m.music is not None
    assert await m.filter_utterance("play something calmer") == "play something calmer"


async def test_speech_detected_during_music_does_not_flicker_the_widget_or_cut_a_reply():
    m, browser, events, ctx = make()
    await m._agent_turn("play it"); m._music_watch.cancel()
    events.clear()
    m._on_speech_start()                                                    # the song's vocals
    assert events == []                                                     # no 'listening' flash, nothing cancelled


async def test_after_a_command_during_music_the_widget_returns_to_playing_not_idle():
    m, browser, events, ctx = make()
    await m._agent_turn("play it"); m._music_watch.cancel()
    m._state("thinking")                                                    # the command's turn moves the widget off "playing"...
    events.clear()
    m._turn = asyncio.create_task(m._run_turn(None, None, "Done."))
    await asyncio.sleep(0.05)
    states = [d["state"] for t, d in events if t == "state"]
    assert states and states[-1] == "music"                                 # ...and when it ends it returns to "playing", not "listening"


async def test_when_the_song_ends_it_goes_back_to_normal_listening_by_itself(monkeypatch):
    import voicectl.modes.prometheus as pm
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pm.asyncio, "sleep", lambda s: real_sleep(0.01))
    m, browser, events, ctx = make()
    await m._agent_turn("play it")
    assert m.music
    browser.state = {**browser.state, "playing": False}                    # ended, or you paused it yourself
    await real_sleep(0.2)
    assert m.music is None and ctx.shared["music_playing"] is False
    assert ("state", {"state": "listening", "mode": "prometheus"}) in events


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


async def test_music_mode_keeps_the_conversation_open_instead_of_timing_out():
    m, browser, events, ctx = make()
    await m._agent_turn("play it")
    assert m.holding()                          # ConversationMode.run() skips its no-speech timeout while holding
    m._music_watch.cancel()


def test_emoji_never_reach_the_captions():
    from voicectl.tts.speaker import clean_for_speech, strip_emoji
    assert strip_emoji("Enjoy the song 🎵 😊!") == "Enjoy the song  !"
    assert "note" not in clean_for_speech("Playing it now 🎵").lower() and "face" not in clean_for_speech("Done 😊").lower()
