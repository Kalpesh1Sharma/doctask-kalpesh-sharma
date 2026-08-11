from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from db.pool import pool


def check_database() -> bool:
    try:
        with pool.connection() as connection:
            result = connection.execute("SELECT 1").fetchone()

        return result == (1,)
    except (OperationalError, PoolTimeout):
        return False
