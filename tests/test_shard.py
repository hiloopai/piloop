import pytest

from tests.shard import Shard

IDS = [f'tests/test_x.py::test_{n}' for n in range(200)]


def test_the_parts_of_a_split_are_disjoint_and_hold_every_test() -> None:
    parts = [Shard.parse(f'{i}/4') for i in range(1, 5)]
    assert all(sum(part.holds(nodeid) for part in parts) == 1 for nodeid in IDS)
    assert all(any(part.holds(nodeid) for nodeid in IDS) for part in parts), 'no part is empty'


@pytest.mark.parametrize('text', ['0/4', '5/4'])
def test_a_part_outside_the_split_is_refused(text: str) -> None:
    with pytest.raises(pytest.UsageError, match=r'I/N with 1 <= I <= N'):
        Shard.parse(text)
