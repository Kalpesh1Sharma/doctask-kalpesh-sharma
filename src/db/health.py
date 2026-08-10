from db.pool import pool


def check_database() -> bool:
    with pool.connection() as connection:
        result = connection.execute("SELECT 1").fetchone()

    return result == (1,)