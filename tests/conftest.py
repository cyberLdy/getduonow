import os

# Most tests exercise the SMS path; production keeps it off until a business number exists (SMS_ENABLED).
os.environ.setdefault("SMS_ENABLED", "1")

import pytest


@pytest.fixture(autouse=True)
def _fresh_neighbours():
    """poller.nearby is learned across passes; one test's metro must not plan another test's requests."""
    from duo_monitor import poller
    poller.nearby.clear()
    yield
    poller.nearby.clear()
