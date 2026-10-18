import socket
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.models import Base

TEST_DB_URL = "postgresql+asyncpg://postgres@127.0.0.1:55432/shopbot_test"


def is_postgres_running() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 55432), timeout=0.2)
        s.close()
        return True
    except OSError:
        return False


@pytest_asyncio.fixture
async def session():
    if not is_postgres_running():
        pytest.skip("PostgreSQL test database is not running at 127.0.0.1:55432")
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as s:
        yield s
    
    await engine.dispose()

