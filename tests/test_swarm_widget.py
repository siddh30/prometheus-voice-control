import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "overlay" / ".build" / "debug" / "VoiceOverlay"


@pytest.mark.skipif(not BIN.exists(), reason="the overlay hasn't been built (cd overlay && swift build)")
def test_the_swarm_widgets_own_logic_shows_only_the_latest_job_shrinks_to_the_pill_and_never_leaves_a_loading_card():
    out = subprocess.run([str(BIN), "--swarm-selftest"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "all passed" in out.stdout
