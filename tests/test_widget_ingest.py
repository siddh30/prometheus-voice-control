"""The widget's message buffer, compiled for real and flooded from several threads. Skipped where there is no Swift compiler."""
import pathlib
import shutil
import subprocess
import tempfile

import pytest

SOCKET = pathlib.Path("overlay/Sources/VoiceOverlay/Socket.swift")

CHECK = '''
let ing = Ingest()
let g = DispatchGroup()
for _ in 0..<4 { DispatchQueue.global().async(group: g) { for i in 0..<200_000 { ing.push(type: "level", data: ["value": Double(i % 10) / 10]) } } }
for i in 0..<1000 { ing.push(type: "swarm", data: ["i": i]) }
g.wait()
let b = ing.take()
precondition(b.level != nil && b.events.count == 1000 && b.link, "800k mic levels collapse to one value; the 1000 swarm updates keep their order")
precondition(ing.take().level == nil && ing.take().events.isEmpty, "a quiet tick changes nothing")
Thread.sleep(forTimeInterval: 0.3)
precondition(ing.take().silent >= 0.3, "silence is measured, so the pill can say NOT RESPONDING")
ing.push(type: "heartbeat", data: [:])
precondition(ing.take().silent < 0.1 && ing.take().events.isEmpty, "a heartbeat resets the silence clock and shows nothing")
ing.setLink(false)
precondition(ing.take().link == false)
print("ingest ok")
'''


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="no Swift compiler")
def test_a_flood_of_messages_becomes_one_update_and_silence_is_measured():
    text = SOCKET.read_text()
    start = text.index("final class Ingest")
    body = text[start:text.index("\n}\n", start) + 3]
    with tempfile.TemporaryDirectory() as d:
        src, exe = pathlib.Path(d, "main.swift"), pathlib.Path(d, "check")
        src.write_text("import Foundation\n" + body + CHECK)
        build = subprocess.run(["swiftc", "-O", str(src), "-o", str(exe)], capture_output=True, text=True, timeout=240)
        assert exe.exists(), build.stderr[-600:]
        out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and "ingest ok" in out.stdout, out.stderr[-400:]
