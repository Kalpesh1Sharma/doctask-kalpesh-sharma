import pytest

from db.health import check_database
from db.pool import pool


@pytest.fixture
def open_pool():
    pool.open()
    yield


def test_database_connection(open_pool):
    assert check_database() is True