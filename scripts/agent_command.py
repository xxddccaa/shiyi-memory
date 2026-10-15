"""Own one native command group; terminate it if the application parent disappears."""
import json
import os
import signal
import subprocess
import sys
import threading


def main():
    request = json.loads(sys.stdin.readline())
    parent, command = request["parent_pid"], request["command"]
    if os.getppid() != parent:
        return 125
    stopped = threading.Event()

    def watch_parent():
        while not stopped.wait(0.5):
            if os.getppid() != parent:
                os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        child = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", command], stdin=subprocess.DEVNULL)
        code = child.wait()
    finally:
        stopped.set()
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
