from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import config

engine = create_async_engine(
    config.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # Проверять соединение перед использованием
    pool_recycle=3600,   # Переподключаться каждый час
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Закрыть все соединения в пуле. Вызывать при остановке бота."""
    await engine.dispose()
