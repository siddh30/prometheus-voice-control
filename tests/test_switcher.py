"""Prometheus Switch: Ctrl+F12 swaps Voice Control and Prometheus Swarm. Its decision logic is compiled and run for real."""
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

SRC = pathlib.Path("overlay/Sources/PrometheusSwitch")

CASES = '''
let cases: [(Set<Mode>, Mode?, Plan)] = [
    ([.single], nil, Plan(quit: [.single], launch: .swarm)),
    ([.swarm], nil, Plan(quit: [.swarm], launch: .single)),
    ([.single], .swarm, Plan(quit: [.single], launch: .swarm)),       // what is running wins over what was used last
    ([], nil, Plan(quit: [], launch: .single)),                         // nothing running, never used: single mode
    ([], .swarm, Plan(quit: [], launch: .swarm)),                       // nothing running: bring back the last one
    ([.single, .swarm], nil, Plan(quit: [.swarm], launch: .single)),    // both (the apps refuse this): keep single mode
]
for (running, last, expected) in cases { precondition(plan(running: running, last: last) == expected, "wrong plan for \\(running)") }
print("plan ok")
'''


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="no Swift compiler")
def test_one_press_of_f12_does_the_right_thing_in_every_situation():
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "Plan.swift").write_text((SRC / "Plan.swift").read_text())
        (pathlib.Path(d) / "main.swift").write_text(CASES)
        build = subprocess.run(["swiftc", "-O", f"{d}/Plan.swift", f"{d}/main.swift", "-o", f"{d}/check"], capture_output=True, text=True, timeout=240)
        assert pathlib.Path(d, "check").exists(), build.stderr[-500:]
        out = subprocess.run([f"{d}/check"], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0 and "plan ok" in out.stdout, out.stderr[-300:]


def test_the_switcher_finds_each_app_by_the_same_identity_the_build_script_gives_it():
    """build_app.sh derives the bundle id from the app's name; the switcher must look for exactly those ids."""
    plan = (SRC / "Plan.swift").read_text()
    for name in ("Voice Control", "Prometheus Swarm"):
        derived = "com.siddharth." + name.replace(" ", "").lower()
        assert derived in plan, f"the switcher does not know {name} as {derived}"
        assert f'"{name}"' in plan
    assert 'ID_SUFFIX=$(printf \'%s\' "$NAME" | tr -d \' \' | tr \'A-Z\' \'a-z\')' in pathlib.Path("scripts/build_app.sh").read_text()


def test_the_switcher_is_a_separate_menu_bar_app_that_never_force_quits():
    package = pathlib.Path("overlay/Package.swift").read_text()
    assert 'name: "PrometheusSwitch"' in package and 'name: "VoiceOverlay"' in package
    code = (SRC / "main.swift").read_text()
    assert "forceTerminate" not in code and ".terminate()" in code             # a graceful quit only: a kill could orphan the voice daemon
    assert "keyboardEventKeycode)" in code and "== 111" in code and "maskControl" in code and "tapCreate" in code     # Ctrl+F12 works anywhere while the app runs
    assert "event.keyCode == 111" in code and "self.model.expanded" in code   # ...and while the card is open, even before the permissions are granted
    assert "beginActivity" in code                                            # a window-less app must not be put to sleep by macOS (App Nap)
    assert "NSPopover" not in code and "WidgetPanel" in code                  # a floating card extending from the icon, not a popover
    installer = pathlib.Path("scripts/build_switcher.sh").read_text()
    assert "LSUIElement" in installer and "com.siddharth.prometheusswitch" in installer
    assert "Accessibility" in installer and "Monitoring" in installer             # the one-time permissions are explained
    assert re.search(r"already open", pathlib.Path("overlay/Sources/VoiceOverlay/main.swift").read_text())
