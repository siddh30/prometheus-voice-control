from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("voicectl.risk")

QUESTION = ("Would this action send, submit, pay for, delete, post, or otherwise commit something on the user's "
            "behalf that is hard to undo?")


class JevRisk:
    """Second opinion on whether a click/keypress is risky, from TypeSafe Jev (a fast classifier with calibrated
    probabilities, ~140 ms). It complements the word list: it catches "Place bet", "Donate", "Withdraw" and the like.
    Returns None whenever it can't answer (no key, slow, error), and the caller then relies on the word list alone."""

    def __init__(self, threshold: float = 0.4, timeout_s: float = 0.7, client=None) -> None:
        self.threshold, self.timeout_s = threshold, timeout_s
        self._client = client

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("TYPESAFE_API_KEY"))

    def _get(self):
        if self._client is None:
            from typesafe_sdk import AsyncTypeSafeClient
            self._client = AsyncTypeSafeClient()
        return self._client

    async def score(self, action: str, page: str = "") -> float | None:
        from typesafe_sdk import Noul
        try:
            resp = await asyncio.wait_for(self._get().system_one(
                state={"action": action, "page": page or "unknown"}, questions={"risky": Noul(instructions=QUESTION)}),
                self.timeout_s)
            return float(resp.nouls["risky"].noul)
        except Exception as e:
            log.debug("risk check unavailable (%s: %s)", type(e).__name__, e)
            return None

    async def is_risky(self, action: str, page: str = "") -> bool:
        p = await self.score(action, page)
        return p is not None and p >= self.threshold
