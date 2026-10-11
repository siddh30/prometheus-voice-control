from voicectl.channels import TextChannel, VoiceChannel
from voicectl.core.config import Config
from voicectl.core.events import EventBus


def test_the_voice_channel_stands_alone_with_no_conversation_or_agent():
    events = []
    bus = EventBus()
    bus.publish = lambda t, **d: events.append((t, d))
    v = VoiceChannel(Config({}), bus, None, on_speech_start=lambda: None)
    assert v.duplex == "half" and v.is_echo("anything") is False        # no speaker: nothing to be an echo of
    v.on_mic_problem("silent")
    assert [t for t, _ in events] == ["notice"]


def test_typing_silences_mic_warnings_through_the_text_channel_only():
    now = [1000.0]
    text = TextChannel(clock=lambda: now[0])
    events = []
    bus = EventBus()
    bus.publish = lambda t, **d: events.append(t)
    v = VoiceChannel(Config({}), bus, None, on_speech_start=lambda: None, quiet=text.recently_used)
    text.note_typed()
    v.on_mic_problem("silent")
    assert events == []
    now[0] += 301                                                       # five minutes without typing
    v.on_mic_problem("silent")
    assert events == ["notice"]


def test_typing_mode_pauses_a_running_listener_and_resumes_it():
    class Ears:
        paused = False
    v = VoiceChannel(Config({}), EventBus(), None, on_speech_start=lambda: None)
    v.make_ears = lambda mic, transcribe: Ears()
    ears = v.open(None, None, paused=True)
    assert ears.paused is True                                          # opened already deaf: the mic is never started
    v.pause(False)
    assert ears.paused is False
