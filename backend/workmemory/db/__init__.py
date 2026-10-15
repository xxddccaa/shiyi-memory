from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from workmemory.config import get_settings


def make_engine(url=None):
    return create_async_engine(
        url or get_settings().database_url,
        pool_size=4,
        max_overflow=2,
        pool_pre_ping=True,
        connect_args={"timeout": 5},
    )


def make_sessions(engine):
    return async_sessionmaker(engine, expire_on_commit=False)
