import asyncio

import numpy as np

from voicectl.core.ears import Ears
from voicectl.input.mic import Mic

SR = 16000


def feed(mic, samples, value):
    """Push audio into the Mic as the audio thread would (without opening a device)."""
    block = np.full(samples, value, "float32")
    mic._callback(block.reshape(-1, 1), samples, None, None)


def test_absolute_indices_survive_trimming():
    m = Mic(SR)
    for i in range(10):
        feed(m, 1000, float(i))            # block i holds the value i
    assert m.tail(2500)[1] == 10000
    m.trim_before(4500)                    # drops whole blocks 0..3 (those ending <= 4500)
    assert m.total_samples == 10000
    s = m.slice(6200, 7300)
    assert len(s) == 1100 and s[0] == 6.0 and s[-1] == 7.0   # still addressed by absolute position
    assert len(m.slice(0, 1000)) == 0                        # trimmed away: nothing, no crash


def test_tail_returns_most_recent():
    m = Mic(SR)
    for i in range(5):
        feed(m, 1000, float(i))
    t, total = m.tail(1500)
    assert total == 5000 and len(t) == 1500 and t[-1] == 4.0 and t[0] == 3.0


class FakeMic:
    sample_rate = SR

    def __init__(self):
        self.total, self.started, self.trimmed = 0, False, []
        self.data = np.full(SR * 60, 0.01, dtype="float32")      # faint constant: stands in for echo

    def start(self): self.started = True
    def stop(self): self.started = False
    def tail(self, n):
        self.total += SR // 8                       # each poll, 0.125 s more audio has arrived
        return self.data[max(0, self.total - n):self.total], self.total
    def slice(self, a, b): return self.data[a:b]
    def trim_before(self, i): self.trimmed.append(i)


class FakeVad:
    """Speech from 1.0 s to 2.0 s (absolute), then done after 0.9 s of quiet."""
    def __init__(self, base, start=SR, end=2 * SR, silence=int(0.9 * SR)):
        self.base, self.s, self.e, self.sil = base, start, end, silence
        self.heard_speech, self.first_speech_start, self.last_speech_end = False, None, 0
        self.gates = []

    def update(self, tail, total, gate=None):
        self.gates.append(gate)
        if total > self.s and self.base < self.e:
            self.heard_speech, self.first_speech_start = True, max(self.s, self.base)
            self.last_speech_end = min(total, self.e)
        return "done" if self.heard_speech and total >= self.e + self.sil else "continue"


async def test_ears_emits_utterance_and_signals_speech_start_immediately():
    mic, started_at = FakeMic(), []
    vads = []

    def factory(base):
        vads.append(FakeVad(base)); return vads[-1]
    ears = Ears(mic, factory, lambda: False, on_speech_start=lambda: started_at.append(mic.total), poll_s=0.001)
    task = asyncio.create_task(ears.run())
    audio = await asyncio.wait_for(ears.next(), 3)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert started_at and started_at[0] < 2 * SR                  # fired while the user was still talking
    assert abs(len(audio.audio) - (1.0 * SR + 0.3 * SR + 0.25 * SR)) < SR * 0.2   # speech + preroll + tail
    assert mic.started is False                                   # mic released at the end
    assert len(vads) >= 2 and vads[1].base > 0                    # a fresh detector took over for the next utterance


async def test_gate_is_applied_only_while_the_assistant_speaks():
    mic, vads, speaking = FakeMic(), [], {"v": True}

    def factory(base):
        vads.append(FakeVad(base)); return vads[-1]
    ears = Ears(mic, factory, lambda: speaking["v"], min_gate=0.04, poll_s=0.001)
    task = asyncio.create_task(ears.run())
    await asyncio.sleep(0.05)
    speaking["v"] = False
    await asyncio.sleep(0.05)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert vads[0].gates[0] == 0.04                      # speaking: only speech louder than the floor counts
    assert vads[-1].gates[-1] is None                    # not speaking: normal sensitivity


async def test_transcription_starts_before_the_full_silence_and_is_reused():
    """Speculative transcription: begun after 0.3 s of quiet, used as-is when you really did stop."""
    mic, calls, vads = FakeMic(), [], []

    async def transcribe(audio):
        calls.append(len(audio)); await asyncio.sleep(0); return "hello"

    def factory(base):
        vads.append(FakeVad(base, silence=int(0.9 * SR))); return vads[-1]
    ears = Ears(mic, factory, lambda: False, poll_s=0.001, transcribe=transcribe, tentative_s=0.3)
    task = asyncio.create_task(ears.run())
    utt = await asyncio.wait_for(ears.next(), 3)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert utt.text_task is not None and await utt.text_task == "hello"
    assert len(calls) == 1                         # one transcription served both the early start and the final result


async def test_speculative_result_is_discarded_if_you_keep_talking():
    class TalksAgain(FakeVad):
        """Speech 1.0-2.0 s, a 0.5 s pause, then more speech 2.5-3.5 s."""
        def update(self, tail, total, gate=None):
            self.gates.append(gate)
            if total > SR and self.base < 3.5 * SR:
                self.heard_speech, self.first_speech_start = True, max(SR, self.base)
                self.last_speech_end = int(2.0 * SR) if total < 2.5 * SR else min(total, int(3.5 * SR))
            return "done" if self.heard_speech and total >= self.last_speech_end + int(0.9 * SR) and total >= 3.5 * SR else "continue"
    mic, seen = FakeMic(), []

    async def transcribe(audio):
        seen.append(len(audio)); await asyncio.sleep(0); return f"{len(audio)}"
    ears = Ears(mic, lambda base: TalksAgain(base), lambda: False, poll_s=0.001, transcribe=transcribe, tentative_s=0.3)
    task = asyncio.create_task(ears.run())
    utt = await asyncio.wait_for(ears.next(), 3)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert len(seen) >= 2 and seen[0] < seen[-1]                  # an early guess on the partial audio, then a new one
    assert await utt.text_task == str(len(utt.audio))              # the answer used matches the FULL utterance


# ---- speakers: the ears use the echo detector instead of deafness -----------------------------------

class RelVad:
    """Speech starts 0.3 s after wherever this detector begins and ends 1.0 s later (relative, so a fast fake clock can't outrun it)."""
    def __init__(self, base):
        self.base, self.heard_speech, self.first_speech_start, self.last_speech_end = base, False, None, 0
    def update(self, tail, total, gate=None):
        el = (total - self.base) / SR
        if el >= 0.3:
            self.heard_speech, self.first_speech_start, self.last_speech_end = True, self.base, self.base + int(0.3 * SR)
        return "done" if el >= 1.3 else "continue"

class SnapMic(FakeMic):
    def snapshot(self): return self.total, 1000.0 + self.total / SR
    def slice(self, a, b): return self.data[max(0, a):b]


class FakeDetector:
    """Says 'you are talking over it' once told to."""
    def __init__(self): self.over, self.fed, self.resets = False, 0, 0
    def feed(self, block, t_end): self.fed += len(block)
    def talking_over(self): return self.over
    def reset(self, keep_calibration=True): self.over = False; self.resets += 1


async def test_on_speakers_its_own_voice_is_not_treated_as_speech_but_you_are():
    mic, vads, fired = SnapMic(), [], []
    speaking = {"v": True}

    def factory(base):
        vads.append(RelVad(base)); return vads[-1]
    det = FakeDetector()
    ears = Ears(mic, factory, lambda: speaking["v"], poll_s=0.002, talk_over=det, on_speech_start=lambda: fired.append(mic.total))
    task = asyncio.create_task(ears.run())
    await asyncio.sleep(0.2)
    assert not fired and det.fed > 0                    # echo only: nothing fires, but the detector is watching the mic
    assert all(not v.heard_speech for v in vads)       # speech detection never ran on our own voice
    det.over = True                                    # the detector hears you on top of the echo
    for _ in range(300):
        if fired:
            break
        await asyncio.sleep(0.005)
    speaking["v"] = False                              # ...and we shut up
    utt = await asyncio.wait_for(ears.next(), 5)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert len(fired) == 1                             # exactly one cut-off signal
    assert len(utt.audio) > 0 and utt.engaged          # your sentence is captured, and it is marked as a talk-over
    assert det.resets >= 1                             # the detector reset for the next time


async def test_without_a_detector_the_old_loudness_path_is_unchanged_for_headphones():
    mic, vads = FakeMic(), []

    def factory(base):
        vads.append(FakeVad(base)); return vads[-1]
    ears = Ears(mic, factory, lambda: False, poll_s=0.001)
    task = asyncio.create_task(ears.run())
    utt = await asyncio.wait_for(ears.next(), 3)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert len(utt.audio) > 0


async def test_the_echo_tail_after_we_stop_is_thrown_away_not_heard_as_you():
    mic, vads = SnapMic(), []
    state = {"deaf": True}

    def factory(base):
        vads.append(RelVad(base)); return vads[-1]
    ears = Ears(mic, factory, lambda: False, poll_s=0.002, deaf=lambda: state["deaf"])
    task = asyncio.create_task(ears.run())
    await asyncio.sleep(0.2)
    assert ears._queue.empty() and all(not v.heard_speech for v in vads)   # deaf: nothing is ever "heard"
    state["deaf"] = False
    utt = await asyncio.wait_for(ears.next(), 5)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert len(utt.audio) > 0 and not utt.engaged


# ---- microphone health ------------------------------------------------------------------------------

class FakeStream:
    instances = []
    def __init__(self, **kw): self.kw, self.started, self.closed = kw, False, False; FakeStream.instances.append(self)
    def start(self): self.started = True
    def stop(self): self.started = False
    def close(self): self.closed = True


def health_mic(monkeypatch):
    import voicectl.input.mic as micmod
    FakeStream.instances.clear()
    monkeypatch.setattr(micmod.sd, "InputStream", FakeStream)
    clock = {"t": 1000.0}
    m = Mic(SR)
    m._clock = lambda: clock["t"]
    return m, clock


def push(m, clock, value=0.1, n=533):
    clock["t"] += n / SR
    feed(m, n, value)


def test_a_device_that_stops_delivering_audio_is_noticed_and_reopened(monkeypatch):
    m, clock = health_mic(monkeypatch)
    m.start()
    for _ in range(5):
        push(m, clock)
    assert not m.stalled()
    clock["t"] += 3.0                                   # Bluetooth profile switch / unplug: nothing arrives
    assert m.stalled()
    total = m.total_samples
    m.restart()
    assert len(FakeStream.instances) == 2 and FakeStream.instances[0].closed and FakeStream.instances[1].started
    assert m.total_samples == total                     # sample numbering survives, so positions already handed out stay valid
    assert not m.stalled()                              # fresh grace period


def test_a_device_that_never_delivers_anything_counts_as_stalled_too(monkeypatch):
    m, clock = health_mic(monkeypatch)
    m.start()
    clock["t"] += 3.0
    assert m.stalled()                                  # dead from the start (permission pending, device gone)


def test_pure_silence_is_reported_but_a_quiet_room_is_not(monkeypatch):
    m, clock = health_mic(monkeypatch)
    m.start()
    for _ in range(300):                                # ~10 s of exact zeros: muted input or revoked permission
        push(m, clock, 0.0)
    assert m.silent_for() > 6.0
    for _ in range(40):
        push(m, clock, 0.0005)                          # a very quiet room still has some signal
    assert m.silent_for() < 1.0


async def test_the_ears_restart_a_stalled_mic_and_say_so_but_only_a_few_times_a_minute():
    class M(SnapMic):
        def __init__(self): super().__init__(); self.restarts = 0; self.stall = True
        def stalled(self, t=1.5): return self.stall
        def restart(self): self.restarts += 1
        def silent_for(self): return 0.0
    mic, problems = M(), []
    ears = Ears(mic, lambda b: RelVad(b), lambda: False, poll_s=0.002, on_mic_problem=problems.append)
    task = asyncio.create_task(ears.run())
    await asyncio.sleep(0.4)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert mic.restarts == 3                            # bounded: not a restart storm
    assert problems.count("restarted") == 3 and "unavailable" in problems


async def test_a_mic_that_returns_only_silence_is_reopened_at_once_and_reported_once_if_it_stays_silent():
    class M(SnapMic):
        def __init__(self): super().__init__(); self.restarts = 0
        def silent_for(self): return 9.0                 # still pure silence even after reopening
        def restart(self): self.restarts += 1
    mic, problems = M(), []
    ears = Ears(mic, lambda b: RelVad(b), lambda: False, poll_s=0.002, on_mic_problem=problems.append)
    task = asyncio.create_task(ears.run())
    await asyncio.sleep(0.3)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert mic.restarts == 1                              # tried reopening it, once, straight away (a USB mic usually comes back)
    assert problems == ["restarted", "silent"]            # then, still silent: one clear report, not a stream of them


async def test_a_mic_that_is_only_briefly_quiet_is_left_alone():
    class M(SnapMic):
        def __init__(self): super().__init__(); self.restarts = 0
        def silent_for(self): return 1.8
        def restart(self): self.restarts += 1
    mic, problems = M(), []
    ears = Ears(mic, lambda b: RelVad(b), lambda: False, poll_s=0.002, on_mic_problem=problems.append)
    task = asyncio.create_task(ears.run())
    await asyncio.sleep(0.2)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert mic.restarts == 0 and problems == []


async def test_a_talk_over_that_turns_out_to_be_nothing_is_dropped_and_counted():
    class NeverSpeech:
        def __init__(self, base): self.base, self.heard_speech, self.first_speech_start, self.last_speech_end = base, False, None, 0
        def update(self, tail, total, gate=None): return "continue"
    mic, false_barges, fired = SnapMic(), [], []
    det = FakeDetector(); det.over = True
    clock = {"t": 5000.0}
    ears = Ears(mic, lambda b: NeverSpeech(b), lambda: True, poll_s=0.002, talk_over=det, clock=lambda: clock["t"],
                on_speech_start=lambda: fired.append(1), on_false_barge=lambda: false_barges.append(1))
    task = asyncio.create_task(ears.run())
    await asyncio.sleep(0.1)
    assert fired == [1] and not false_barges           # it cut us off once...
    clock["t"] += 3.0                                   # ...and nobody spoke
    await asyncio.sleep(0.1)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert len(false_barges) >= 1                           # recorded as a false alarm: the self-diagnosis uses this


def test_the_mic_recovers_when_the_device_changed_under_it(monkeypatch):
    """AirPods off / USB mic in: the first open fails on a stale device list; refresh PortAudio and it works. If the new device refuses
    16 kHz, record at its own rate and convert."""
    import numpy as np
    from voicectl.input import mic as micmod
    events = []

    class Boom(micmod.sd.PortAudioError): pass

    class FakeStream:
        def __init__(self, samplerate, callback, **kw):
            self.rate, self.cb = samplerate, callback
            events.append(("open", samplerate))
            if (self.rate == 16000 and not state["refreshed"]) or (state["mode"] == "no16k" and self.rate == 16000):
                raise Boom("Audio Unit: Invalid Property Value")
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    state = {"refreshed": False, "mode": "stale"}
    monkeypatch.setattr(micmod.sd, "InputStream", FakeStream)
    monkeypatch.setattr(micmod.sd, "_terminate", lambda: events.append(("terminate",)), raising=False)
    monkeypatch.setattr(micmod.sd, "_initialize", lambda: state.update(refreshed=True) or events.append(("initialize",)), raising=False)
    monkeypatch.setattr(micmod.sd, "query_devices", lambda kind=None: {"default_samplerate": 48000.0})
    micmod.Mic._open_streams = 0

    m = micmod.Mic(16000)
    m.start()                                                             # stale list -> refresh -> ok at 16 kHz
    assert ("terminate",) in events and events[-1] == ("open", 16000) and m._stream is not None
    m.stop()

    events.clear(); state.update(mode="no16k")
    m2 = micmod.Mic(16000)
    m2.start()                                                            # device refuses 16 kHz -> native 48 kHz, converted
    assert events[-1] == ("open", 48000)
    m2._stream.cb(np.ones((1600, 1), "float32") * 0.5, 1600, None, None)
    assert abs(sum(len(f) for f in m2._frames) - 533) <= 1 and abs(float(m2._frames[0].mean()) - 0.5) < 0.05
    m2.stop()
    micmod.Mic._open_streams = 0
