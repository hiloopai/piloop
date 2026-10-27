from collections.abc import Iterator

import pytest

from tests.fixtures.slack import FakeSlack, fake_slack


@pytest.fixture
def slack() -> Iterator[FakeSlack]:
    with fake_slack() as fake:
        yield fake
