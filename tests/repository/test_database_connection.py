from db.health import check_database


def test_database_connection():
    assert check_database() is True