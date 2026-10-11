import asyncio

from voicectl.core.config import Config
from voicectl.core.echo import EchoReference
from voicectl.core.events import EventBus
from voicectl.modes.base import ModeContext
from voicectl.modes.conversation import ConversationMode


class Spk:
    def __init__(self, with_reference=True):
        self.active, self.spoken, self._quiet = False, [], 99.0
        if with_reference:
            self.reference = EchoReference()
    def quiet_for(self): return self._quiet
    def stop(self): ...
    def wake(self): ...


def mode(output, barge_in=True, spk=None, monkeypatch=None, headphones=False):
    cfg = Config({"conversation": {"output": output, "barge_in": barge_in}})
    m = ConversationMode(ModeContext(cfg, EventBus(), spk if spk is not None else Spk()))
    if monkeypatch:
        import voicectl.modes.conversation as c
        monkeypatch.setattr(c, "wearing_headphones", lambda: headphones)
    return m


async def test_headphones_means_full_duplex_and_never_deaf():
    m = mode("headphones"); await m._decide_duplex()
    assert m._duplex == "full" and m._detector is None
    m.ctx.speaker.active = True
    assert m._deaf() is False                                  # talking over it is free


async def test_speakers_use_the_echo_detector_by_default():
    m = mode("speakers"); await m._decide_duplex()
    assert m._duplex == "echo" and m._detector is not None
    m.ctx.speaker.active, m.ctx.speaker._quiet = True, 0.0
    assert m._deaf() is False                                  # it listens WHILE we speak (that is the point)
    m.ctx.speaker.active, m.ctx.speaker._quiet = False, 0.3
    assert m._deaf() is True                                   # ...but ignores the fading echo tail just after
    m.ctx.speaker._quiet = 2.0
    assert m._deaf() is False


async def test_speakers_with_barge_in_off_are_strictly_half_duplex():
    m = mode("speakers", barge_in=False); await m._decide_duplex()
    assert m._duplex == "half" and m._detector is None
    m.ctx.speaker.active = True
    assert m._deaf() is True                                   # deaf the whole time it speaks
    m.ctx.speaker.active, m.ctx.speaker._quiet = False, 5.0
    assert m._deaf() is False


async def test_auto_picks_by_what_the_machine_reports(monkeypatch):
    m = mode("auto", monkeypatch=monkeypatch, headphones=True); await m._decide_duplex(); assert m._duplex == "full"
    m = mode("auto", monkeypatch=monkeypatch, headphones=False); await m._decide_duplex(); assert m._duplex == "echo"


async def test_a_speaker_that_cannot_describe_what_it_plays_falls_back_to_the_safe_style():
    m = mode("speakers", spk=Spk(with_reference=False)); await m._decide_duplex()
    assert m._duplex == "half" and m._detector is None


def test_headphone_names_are_recognised_and_a_usb_dac_is_not_guessed():
    import voicectl.core.audio_output as ao
    for name in ["AirPods Pro", "Sony WH-1000XM5", "Jabra Evolve2", "Beats Studio", "Bose QC45", "Logitech Headset H390"]:
        assert ao.HEADPHONES.search(name), name
    assert not ao.HEADPHONES.search("USBAudio2.0") and not ao.HEADPHONES.search("MacBook Pro Speakers")


def test_a_thing_we_just_said_is_recognised_as_our_own_echo_and_real_speech_is_not():
    import time
    spk = Spk(); now = time.monotonic()
    spk.spoken = [(now - 5, "Obvious by Chris Brown is now playing, enjoy the song.")]
    cfg = Config({"conversation": {"output": "headphones"}})
    m = ConversationMode(ModeContext(cfg, EventBus(), spk))
    assert m._is_echo("Obvious by Chris Brown is now playing enjoy the song")             # the exact thing that happened to you
    assert m._is_echo("by Chris Brown is now playing")                                    # a fragment of it
    assert not m._is_echo("play something by Drake instead")                              # genuinely new
    assert not m._is_echo("stop")                                                         # short real commands are never discarded
    spk.spoken = []
    assert not m._is_echo("Obvious by Chris Brown is now playing")                        # nothing recently said: it must be you


# ---- self-diagnosis -------------------------------------------------------------------------------

async def test_hearing_itself_twice_steps_down_headphones_to_echo_aware_to_half_duplex():
    notes = []
    m = mode("headphones")
    m.ctx.bus.publish = lambda t, **d: notes.append((t, d))
    await m._decide_duplex()
    assert m._duplex == "full"
    m._note_self_heard()
    assert m._duplex == "full"                                  # once could be a fluke
    m._note_self_heard()
    assert m._duplex == "echo" and m._detector is not None      # twice: the "headphones" assumption is wrong
    assert any(t == "notice" and "kept hearing myself" in d["text"] for t, d in notes)
    m._note_self_heard(); m._note_self_heard()
    assert m._duplex == "half" and m._detector is None          # still: stop listening while it speaks
    m._note_self_heard(); m._note_self_heard()
    assert m._duplex == "half"                                  # nowhere safer to go; no crash


async def test_two_false_interruptions_on_speakers_step_down_to_half_duplex():
    m = mode("speakers"); await m._decide_duplex()
    assert m._duplex == "echo"
    m._on_false_barge()
    assert m._duplex == "echo"
    m._on_false_barge()
    assert m._duplex == "half"


async def test_stepping_down_rewires_a_running_ears_object():
    m = mode("speakers"); await m._decide_duplex()
    class E: talk_over = m._detector
    m._ears = E()
    m._step_down("test")
    assert m._ears.talk_over is None                            # the live listener stops using the detector immediately


def test_mic_problems_are_reported_in_plain_words():
    out = []
    m = mode("headphones")
    m.ctx.bus.publish = lambda t, **d: out.append((t, d))
    m._on_mic_problem("restarted"); m._on_mic_problem("silent"); m._on_mic_problem("unavailable")
    assert out[0] == ("notice", {"text": "The microphone changed, so I reconnected to it."})
    assert "Microphone permission" in out[1][1]["text"]
    assert out[2][0] == "error" and "can't reach the microphone" in out[2][1]["message"]


def test_a_silent_microphone_is_not_a_warning_while_you_are_typing():
    out = []
    m = mode("headphones")
    m.ctx.bus.publish = lambda t, **d: out.append((t, d))
    import time
    m._last_typed = time.monotonic()                             # what submit_text records
    m._on_mic_problem("silent")
    assert out == []                                             # typing needs no microphone
    m._last_typed = 0.0
    m._on_mic_problem("silent")
    assert [t for t, _ in out] == ["notice"]                     # but a silent mic is still reported when you are speaking
