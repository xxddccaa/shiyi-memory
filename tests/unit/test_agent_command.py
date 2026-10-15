"""The native command supervisor must not outlive a killed application parent."""
import asyncio
import os
import signal
import sys

import pytest
from workmemory.config import ROOT


@pytest.mark.asyncio
async def test_command_group_stops_when_application_parent_dies(tmp_path):
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import subprocess,sys,json,os; "
        "p=subprocess.Popen([sys.executable,sys.argv[1]],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,start_new_session=True); "
        "p.stdin.write((json.dumps({'parent_pid':os.getpid(),'command':'printf started > marker; sleep 30; printf late > late'})+'\\n').encode()); "
        "p.stdin.close(); print(p.pid,flush=True); sys.stdin.read()",
        str(ROOT / "scripts/agent_command.py"), cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    group = int(await owner.stdout.readline())
    try:
        for _ in range(100):
            if (tmp_path / "marker").exists():
                break
            await asyncio.sleep(.02)
        assert (tmp_path / "marker").exists()
        owner.kill()
        await owner.wait()
        for _ in range(100):
            try:
                os.kill(group, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(.02)
        else:
            pytest.fail("native supervisor survived application death")
        assert not (tmp_path / "late").exists()
    finally:
        if owner.returncode is None:
            owner.kill()
            await owner.wait()
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
