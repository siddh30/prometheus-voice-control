import numpy as np

from voicectl.core.endpoint import Endpointer

SR = 16000
BLOCK = SR // 30


def tone(amp):
    return (np.sin(np.linspace(0, 200, BLOCK)) * amp).astype("float32")


def run(ep, blocks):
    for i, b in enumerate(blocks):
        r = ep.feed(b)
        if r != "continue":
            return r, i
    return "continue", len(blocks)


def test_done_after_speech_then_silence():
    ep = Endpointer(SR, silence_s=0.6)
    blocks = [tone(0.002)] * 12 + [tone(0.2)] * 30 + [tone(0.002)] * 40
    r, i = run(ep, blocks)
    assert r == "done" and i > 42            # not before the speech ends


def test_pause_shorter_than_silence_does_not_end():
    ep = Endpointer(SR, silence_s=1.0)
    blocks = [tone(0.002)] * 12 + [tone(0.2)] * 20 + [tone(0.002)] * 15 + [tone(0.2)] * 20
    assert run(ep, blocks)[0] == "continue"


def test_timeout_when_nobody_speaks():
    ep = Endpointer(SR, no_speech_s=1.0)
    assert run(ep, [tone(0.002)] * 60)[0] == "timeout"


def test_blip_is_not_speech():
    ep = Endpointer(SR, silence_s=0.5, min_speech_s=0.25, no_speech_s=1.5)
    blocks = [tone(0.002)] * 12 + [tone(0.3)] * 2 + [tone(0.002)] * 60
    assert run(ep, blocks)[0] == "timeout"


def test_noisy_room_raises_threshold():
    ep = Endpointer(SR, silence_s=0.5, no_speech_s=1.5)
    noise = [tone(0.02)] * 80            # constant hum is background, not speech
    assert run(ep, noise)[0] == "timeout"


def test_loud_start_does_not_strand_the_threshold():
    """Regression: echo/speech inside the first moments used to calibrate a threshold above normal speech."""
    ep = Endpointer(SR, silence_s=0.6, no_speech_s=5)
    blocks = [tone(0.10)] * 4 + [tone(0.003)] * 40 + [tone(0.2)] * 25 + [tone(0.003)] * 40   # echo tail, quiet, then speech
    r, i = run(ep, blocks)
    assert r == "done" and ep.heard_speech


def test_speech_right_from_the_start_is_still_detected():
    ep = Endpointer(SR, silence_s=0.6, no_speech_s=5)
    blocks = [tone(0.2)] * 40 + [tone(0.003)] * 40
    assert run(ep, blocks)[0] == "done"


def test_threshold_is_capped():
    ep = Endpointer(SR, no_speech_s=5)
    run(ep, [tone(0.25)] * 90)               # a continuously loud room
    assert ep.threshold <= 0.05
