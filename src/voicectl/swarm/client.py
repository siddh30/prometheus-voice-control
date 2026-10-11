from __future__ import annotations

"""A small client for the swarm hub's HTTP API, shared by the voice assistant's swarm tools and by Swarm mode."""

import re

import aiohttp


class HubClient:
    def __init__(self, base: str, token: str, source: str = "voice") -> None:
        self.base = base.rstrip("/")
        self.headers = {"X-Swarm-Source": source, **({"Authorization": f"Bearer {token}"} if token else {})}

    async def call(self, method: str, path: str, timeout: float = 130, **kw) -> dict | list:
        async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, self.base + path, **kw) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    raise RuntimeError(data.get("error", f"HTTP {r.status}") if isinstance(data, dict) else f"HTTP {r.status}")
                return data


def short_for_speech(text: str, limit: int = 300) -> str:
    """The first few sentences of an answer, stripped of markdown and links, short enough to listen to. The full text stays on screen."""
    from voicectl.tts.speaker import clean_for_speech
    text = clean_for_speech(text, None) if text else ""
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", text.strip())[:4]:
        if len(out) + len(s) > limit and out:
            break
        out = f"{out} {s}".strip()
    return out[:limit].rstrip()
