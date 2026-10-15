"""Read the selected backend without creating an app or starting any worker."""

import asyncio

from sqlalchemy import select
from workmemory.config import get_settings
from workmemory.db import make_engine, make_sessions
from workmemory.db.models import AppState


async def main():
    engine = make_engine()
    try:
        async with make_sessions(engine)() as session:
            state = await session.scalar(select(AppState).where(AppState.key == "memory"))
            print(state.value["backend"] if state else get_settings().memory.backend)
    except Exception:
        print(get_settings().memory.backend)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
