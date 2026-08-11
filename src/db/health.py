from psycopg import OperationalError

from db.pool import pool


def check_database() -> bool:
    try:
        with pool.connection() as connection:
            result = connection.execute("SELECT 1").fetchone()

        return result == (1,)
    except OperationalError:
        return False