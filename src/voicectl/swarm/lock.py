from __future__ import annotations

"""Only one thing per computer may run the swarm node (and hub): either Voice Control's background process or the Prometheus Swarm
app's own backend, whichever starts first. A tiny pid file settles it; a stale one (the process is gone) is simply taken over."""

import atexit
import os
from pathlib import Path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def holder(data_dir: Path) -> int | None:
    """The pid of the process currently running the swarm here, or None."""
    try:
        pid = int((data_dir / "swarm_backend.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid != os.getpid() and _alive(pid) else None


def try_acquire(data_dir: Path) -> bool:
    if holder(data_dir) is not None:
        return False
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "swarm_backend.pid"
    path.write_text(str(os.getpid()))

    def release() -> None:
        try:
            if path.read_text().strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass
    atexit.register(release)
    return True
