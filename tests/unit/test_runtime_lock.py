import pytest
from workmemory.jobs.runtime_lock import acquire_runtime_lock


def test_single_workspace_owner_and_release(tmp_path):
    path = tmp_path / "runtime.lock"
    owner = acquire_runtime_lock(path)
    try:
        with pytest.raises(RuntimeError, match="owns this project workspace"):
            acquire_runtime_lock(path)
    finally:
        owner.close()
    # Stale lock files do not block startup after a process exits.
    with acquire_runtime_lock(path):
        pass
