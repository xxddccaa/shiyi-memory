"""Reopen only this project's app, without rebuilding or resetting permissions."""

from manager import operation_lock, script, stop_collector

if __name__ == "__main__":
    with operation_lock():
        stop_collector()
        script("collector-start.sh")
