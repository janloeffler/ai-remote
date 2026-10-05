import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app import rate_limit

    rate_limit.reset()
    yield


@pytest.fixture
def conn(tmp_path):
    from app import db

    connection = db.get_connection(str(tmp_path / "test.db"))
    db.init_db(connection)
    yield connection
    connection.close()
