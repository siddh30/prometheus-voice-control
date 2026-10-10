from __future__ import annotations

"""Hearing YOU over its own voice, on speakers.

The assistant knows exactly what it is playing. So instead of guessing from loudness, we keep the loudness envelope of the
audio we play (EchoReference) and learn how it shows up in the microphone: the delay (speaker + room + device latency) and
the gain. Echo is then PREDICTED, and a person talking over it is the energy left over. This only needs 20 ms envelopes, so
it is robust to reverb, clipping and unknown latency, where true echo cancellation is fragile.
"""

import time
from collections import deque

import numpy as np

SR = 16000
FRAME = 320                       # 20 ms
FRAME_S = FRAME / SR


def frame_rms(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME
    if n == 0:
        return np.zeros(0, dtype="float32")
    return np.sqrt(np.mean(x[: n * FRAME].reshape(n, FRAME).astype("float64") ** 2, axis=1)).astype("float32")


class EchoReference:
    """The loudness envelope of everything we play, on the wall clock (time.monotonic)."""

    def __init__(self) -> None:
        self._segs: deque[tuple[float, np.ndarray]] = deque(maxlen=80)

    def add(self, t_start: float, pcm: np.ndarray) -> None:
        self._segs.append((t_start, frame_rms(pcm)))

    def clear(self) -> None:
        self._segs.clear()

    def env_at(self, t: float) -> float:
        for t0, env in reversed(self._segs):
            i = int((t - t0) / FRAME_S)
            if 0 <= i < len(env):
                return float(env[i])
        return 0.0

    def active_at(self, t: float) -> bool:
        return self.env_at(t) > 0.0 or any(t0 <= t < t0 + len(env) * FRAME_S for t0, env in self._segs)

    def playing(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return any(t0 <= now < t0 + len(env) * FRAME_S + 0.6 for t0, env in self._segs)


def nnls(X: np.ndarray, y: np.ndarray, sweeps: int = 120) -> np.ndarray:
    """Non-negative least squares by coordinate descent (small problems only; avoids needing scipy)."""
    n = X.shape[1]
    G = X.T @ X + 1e-9 * np.eye(n)
    b = X.T @ y
    w = np.zeros(n)
    for _ in range(sweeps):
        for j in range(n):
            w[j] = max(0.0, (b[j] - G[j] @ w + G[j, j] * w[j]) / G[j, j])
    return w


class TalkOverDetector:
    """Feed it microphone audio while the assistant is speaking; ask talking_over().

    It learns the echo as a short non-negative filter over the loudness envelope: how much of the reference level at each delay
    (direct sound, then the room's reverb tail) shows up in the mic. Lag/filter persist across utterances (the device latency
    does not change), so after the first sentence it is calibrated from the start."""

    TAPS = 30                                                     # 0.6 s of echo shape

    def __init__(self, ref: EchoReference, max_lag_s: float = 0.7, window_s: float = 3.0, need: int = 5, of: int = 10,
                 margin: float = 1.35, floor: float = 0.010) -> None:
        self.ref, self.max_lag_s, self.window_s = ref, max_lag_s, window_s
        self.need, self.of, self.margin, self.floor = need, of, margin, floor
        self.lag: float | None = None                              # where the echo begins (seconds after playback)
        self.w: np.ndarray | None = None                           # envelope filter taps
        self.noise = 0.004
        self.sigma = 0.05                                          # how rough the echo model's fit is (std of its error)
        self._frames: deque[tuple[float, float]] = deque(maxlen=int(window_s / FRAME_S))     # (wall time of frame centre, rms)
        self._carry = np.zeros(0, dtype="float32")
        self._flags: deque[bool] = deque(maxlen=of)
        self._since_cal = 0
        self._latched = False
        self._freeze_until = 0.0

    @property
    def gain(self) -> float | None:
        return None if self.w is None else float(self.w.sum())

    @property
    def calibrated(self) -> bool:
        return self.lag is not None and self.w is not None

    # ---- input ----
    def feed(self, block: np.ndarray, t_end: float) -> None:
        """`block` = mic samples whose last sample was captured at wall time t_end."""
        x = np.concatenate([self._carry, block.astype("float32")])
        t_start = t_end - len(x) / SR
        n = len(x) // FRAME
        for i in range(n):
            seg = x[i * FRAME:(i + 1) * FRAME]
            tc = t_start + (i + 0.5) * FRAME_S
            env = float(np.sqrt(np.mean(seg.astype("float64") ** 2)))
            self._frames.append((tc, env))
            self._step(tc, env)
        self._carry = x[n * FRAME:]

    def reset(self, keep_calibration: bool = True) -> None:
        self._frames.clear(); self._flags.clear(); self._carry = np.zeros(0, dtype="float32"); self._latched = False; self._since_cal = 0; self._freeze_until = 0.0
        if not keep_calibration:
            self.lag = self.w = None

    def talking_over(self) -> bool:
        return self._latched

    # ---- the algorithm ----
    @staticmethod
    def _expand(lin: np.ndarray) -> np.ndarray:
        """Linear taps plus their square roots: a clipping or compressing speaker makes loud passages grow slower than linear."""
        return np.concatenate([lin, np.sqrt(lin)], axis=-1)

    def _features(self, tc: float) -> np.ndarray:
        start = tc - self.lag
        return self._expand(np.array([self.ref.env_at(start - k * FRAME_S) for k in range(self.TAPS)]))

    def _predicted(self, tc: float) -> float:
        return float(self._features(tc) @ self.w)

    def _step(self, tc: float, env: float) -> None:
        if not self.ref.playing(tc):
            self._flags.append(False)
            return
        self._since_cal += 1
        if sum(self._flags) >= 2:                                # louder than the echo model says: don't let it learn that as echo
            self._freeze_until = tc + 1.5
        suspicious = tc < self._freeze_until
        if not self._latched and not suspicious and (not self.calibrated or self._since_cal >= 25):
            self._calibrate()
            self._since_cal = 0
        if not self.calibrated:
            self._flags.append(False)
            return
        self._flags.append(env > self.margin * self._predicted(tc) + self.floor + self.noise + 2.5 * self.sigma)
        if sum(self._flags) >= self.need:
            self._latched = True                              # latch until reset(): the caller will cut the assistant off

    def _calibrate(self) -> None:
        if len(self._frames) < 30:
            return
        t = np.array([f[0] for f in self._frames]); e = np.array([f[1] for f in self._frames])
        best, best_corr = None, 0.0
        for lag in np.arange(0.0, self.max_lag_s, FRAME_S):
            r = np.array([self.ref.env_at(tt - lag) for tt in t])
            if r.std() < 1e-4 or e.std() < 1e-5:
                continue
            c = float(np.corrcoef(e, r)[0, 1])
            if c > best_corr:
                best, best_corr = float(lag), c
        if best is None or best_corr < 0.45:
            return                                             # can't see our own voice in the mic yet
        # the echo starts a little before the best single lag (room response is not a spike): centre the taps around it
        start_lag = max(0.0, best - 0.08)
        Xlin = np.array([[self.ref.env_at(tt - start_lag - k * FRAME_S) for k in range(self.TAPS)] for tt in t])
        if (Xlin.max(axis=1) > 0.15 * Xlin.max()).sum() < 12:
            return
        X = self._expand(Xlin)
        w = nnls(X, e, sweeps=60)
        resid = e - X @ w
        r2 = 1.0 - float(np.var(resid) / max(np.var(e), 1e-12))
        if r2 < 0.55 or len(t) < 40:                           # the model doesn't explain the mic well (or too little data): don't trust it yet
            return
        self.sigma = float(np.std(resid))
        self.noise = float(min(0.01, np.percentile(e, 10)))   # the room's own hiss, never inflated by echo/reverb tails
        self.lag = start_lag if self.lag is None else 0.7 * self.lag + 0.3 * start_lag
        self.w = w if self.w is None or len(self.w) != len(w) else 0.7 * self.w + 0.3 * w
