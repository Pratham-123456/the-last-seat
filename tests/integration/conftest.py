import pytest


@pytest.fixture(autouse=True)
def _fresh_database(clean_db):
    """Every integration test starts with 500 free seats in last_seat_test_db."""
    yield
