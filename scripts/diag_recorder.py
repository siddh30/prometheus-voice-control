import os, re, subprocess, sys, threading, time

"""Diagnostic recorder: run it while you reproduce a stall (on any Mac) and it writes a timeline you can read afterwards.
    python3 scripts/diag_recorder.py [seconds]      (default 1800; logs to /tmp/swarm_diag/timeline.log)
Every 2 s it logs: how late a 50 ms timer fired (a stalled Mac makes that large), load, memory pressure, free/compressed/swap memory, the
busiest processes by CPU and by memory, and WindowServer's CPU (the macOS screen compositor). When something looks wrong it samples the
Voice Control / Prometheus Swarm app and its daemon automatically. It only reads system statistics; nothing leaves the machine."""
OUT = "/tmp/swarm_diag"
os.makedirs(OUT, exist_ok=True)
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 1800
log = open(f"{OUT}/timeline.log", "a", buffering=1)
late = {"max": 0.0}
stop = False

def probe():                       # how late is a 50 ms timer? a stalled Mac makes it hundreds of ms or seconds late
    while not stop:
        t0 = time.monotonic(); time.sleep(0.05)
        late["max"] = max(late["max"], time.monotonic() - t0 - 0.05)
threading.Thread(target=probe, daemon=True).start()

def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout

def procs():
    rows = []
    for l in sh("ps", "-Ao", "pid=,ppid=,pcpu=,rss=,comm=").splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(.*)", l)
        if m: rows.append((int(m[1]), int(m[2]), float(m[3]), int(m[4]) / 1024, m[5].split("/")[-1]))
    return rows

def vm():
    v = sh("vm_stat")
    comp = int(re.search(r"occupied by compressor:\s+(\d+)", v)[1]) * 16 / 1024
    free = int(re.search(r"Pages free:\s+(\d+)", v)[1]) * 16 / 1024
    swap = re.search(r"used = ([\d.]+)M", sh("sysctl", "-n", "vm.swapusage"))[1]
    level = sh("sysctl", "-n", "kern.memorystatus_vm_pressure_level").strip()
    return comp, free, swap, level

last_sample = 0
ws_hot = 0                         # consecutive ticks with WindowServer near a full core
start = time.monotonic()
log.write(f"# recorder started {time.strftime('%H:%M:%S')}  (time | lag_max_s | load1 | pressure p1 ok p2 warn p4 critical | free/compressor/swap MB | top CPU | top memory)\n")
while time.monotonic() - start < DURATION:
    time.sleep(2)
    lag, late["max"] = late["max"], 0.0
    try:
        P = procs(); comp, free, swap, level = vm()
        load = os.getloadavg()[0]
        cpu = ", ".join(f"{c}:{p:.0f}%" for _, _, p, _, c in sorted(P, key=lambda r: -r[2])[:5])
        mem = ", ".join(f"{c}:{m:.0f}M" for _, _, _, m, c in sorted(P, key=lambda r: -r[3])[:4])
        log.write(f"{time.strftime('%H:%M:%S')} | lag {lag:.2f} | load {load:.1f} | p{level} | free {free:.0f} comp {comp:.0f} swap {swap} | {cpu} | {mem}\n")
        ws = next((p for _, _, p, _, c in P if c == "WindowServer"), 0.0)
        ws_hot = ws_hot + 1 if ws >= 90 else 0
        # something is wrong: capture what the swarm app and its daemon are doing right now, at most once a minute
        if (lag > 1.0 or load > 8 or level in ("2", "4") or ws_hot >= 3) and time.monotonic() - last_sample > 60:
            last_sample = time.monotonic()
            app = [pid for pid, _, _, _, c in P if c == "VoiceControl"]
            targets = list(app)
            for a in app: targets += [pid for pid, pp, _, _, c in P if pp == a and c.startswith("python")]
            stamp = time.strftime("%H%M%S")
            for pid in targets[:3]:
                subprocess.Popen(["sample", str(pid), "3", "-file", f"{OUT}/sample_{stamp}_{pid}.txt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.write(f"{time.strftime('%H:%M:%S')} ** trouble detected (lag {lag:.2f}s, load {load:.1f}, pressure {level}, WindowServer {ws:.0f}%): sampling pids {targets[:3]}\n")
    except Exception as e:
        log.write(f"{time.strftime('%H:%M:%S')} recorder error: {type(e).__name__}: {e}\n")
stop = True
log.write("# recorder finished\n")
