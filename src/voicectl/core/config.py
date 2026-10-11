from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "VoiceControl"


def _profile() -> str:
    """Which copy is this? A release snapshot says "stable" (profile.txt); the working folder is "dev"; the env var wins.
    The two never share data, ports, hotkeys or the microphone, so testing one cannot disturb the other."""
    if os.environ.get("VOICECTL_PROFILE"):
        return os.environ["VOICECTL_PROFILE"]
    marker = ROOT / "profile.txt"
    return marker.read_text().strip() if marker.exists() else "dev"


PROFILE = _profile()
DATA_DIR = (Path(os.environ["VOICECTL_DATA"]) if os.environ.get("VOICECTL_DATA")
            else APP_SUPPORT / "data" if PROFILE == "stable" else ROOT / f"data-{PROFILE}")


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path | None = None) -> Config:
    """config.yaml, then config.local.yaml (this computer's own settings: git-ignored, e.g. the swarm), then config.<profile>.yaml on top
    (the dev profile silences hotkeys, mic, voice and uses a hidden browser)."""
    load_dotenv(ROOT / ".env")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    for overlay in (path.with_name("config.local.yaml"), path.with_name(f"config.{PROFILE}.yaml")):
        if overlay.exists():
            raw = _merge(raw, yaml.safe_load(overlay.read_text()) or {})
    return Config(raw)
