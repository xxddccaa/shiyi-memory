"""Keep the local ReMe workspace single-owner across PostgreSQL restarts."""

import fcntl
from pathlib import Path


def acquire_runtime_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("Another WorkMemory runtime owns this project workspace") from exc
    except BaseException:
        handle.close()
        raise
    return handle
