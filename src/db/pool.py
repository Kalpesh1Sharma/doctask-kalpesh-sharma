from collections.abc import Iterator

from psycopg import Connection
from psycopg_pool import ConnectionPool

from config.settings import get_settings


def create_pool() -> ConnectionPool:
    settings = get_settings()

    return ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        open=False,
    )


pool = create_pool()
pool.open()


def get_connection() -> Iterator[Connection]:
    with pool.connection() as connection:
        yield connection