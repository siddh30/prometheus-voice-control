from __future__ import annotations

"""Is the user on headphones or speakers? On speakers the microphone hears the assistant's own voice, so the ears must be
deaf while it speaks. With headphones there is no echo and talking over it can stay on."""

import json
import re
from voicectl import platform
import time

HEADPHONES = re.compile(r"airpods|headphone|headset|ear\s?buds?|\bbuds\b|beats|bose|wh-1000|wf-1000|jabra|plantronics|sennheiser", re.I)
_cache: tuple[float, tuple[str, str]] | None = None


def default_output(ttl_s: float = 60.0) -> tuple[str, str]:
    """-> (device name, transport) of the default output, e.g. ("USBAudio2.0", "usb"). ("", "") if unknown."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < ttl_s:
        return _cache[1]
    result = ("", "")
    try:
        raw = platform.current().audio_devices_json()
        for group in json.loads(raw).get("SPAudioDataType", []):
            for item in group.get("_items", []):
                if any(k.startswith("coreaudio_default_audio_output") and str(v).endswith("yes") for k, v in item.items()):
                    transport = str(item.get("coreaudio_device_transport", "")).replace("coreaudio_device_type_", "")
                    result = (str(item.get("_name", "")), transport)
    except Exception:
        pass
    _cache = (time.monotonic(), result)
    return result


def wearing_headphones() -> bool:
    name, transport = default_output()
    return bool(HEADPHONES.search(name)) or (transport == "bluetooth" and not re.search(r"speaker|homepod|soundbar|boombox", name, re.I))
