"""One-time, explicitly requested cleanup of pre-scheduled-todo data.

Only origin=legacy rows are removed. A pg_dump is required before --apply, and
normal source invalidation/tombstones handle existing answers and proposals.
"""
import argparse
import asyncio
import json

from manager import operation_lock, owns_process
from native_database import NativePostgres
from sqlalchemy import select
from workmemory.db import make_engine, make_sessions
from workmemory.db.models import AppState, Todo
from workmemory.jobs.queue import now
from workmemory.workspace.service import remove_todo

KEY = "legacy_todos_retired_v1"


async def retire(apply=False, backup=None):
    engine = make_engine()
    try:
        async with make_sessions(engine).begin() as session:
            if await session.get(AppState, KEY):
                return {"already_completed": True}
            rows = (await session.scalars(select(Todo).where(Todo.origin == "legacy").order_by(Todo.id).with_for_update())).all()
            result = {"count": len(rows), "ids": [str(row.id) for row in rows]}
            if apply:
                for row in rows:
                    await remove_todo(session, row)
                result.update(completed_at=now().isoformat(), backup=str(backup))
                session.add(AppState(key=KEY, value=result))
            return {"count": len(rows), "applied": apply}
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="清理升级前待办（只运行一次）")
    parser.add_argument("--apply", action="store_true", help="执行已获用户授权的旧待办清理；默认只显示数量")
    args = parser.parse_args()
    with operation_lock():
        if owns_process("backend"):
            raise RuntimeError("请先停止本项目后端，再清理旧待办")
        pg = NativePostgres()
        pg.check_marker()
        pg.assert_connection()
        backup = None
        if args.apply:
            backup = pg.backup()
            pg.restore_check(backup)
        print(json.dumps(asyncio.run(retire(args.apply, backup)), ensure_ascii=False))


if __name__ == "__main__":
    main()
