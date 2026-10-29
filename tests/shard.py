"""``--shard I/N`` runs the I-th of N disjoint parts of the selected tests, so CI can spread the suite over runners.

A test's part is a hash of its node id: it stays put as tests come and go, with no file of timings to keep."""

import zlib
from typing import NamedTuple

import pytest


class Shard(NamedTuple):
    index: int
    count: int

    @classmethod
    def parse(cls, text: str) -> Shard:
        index, count = (int(part) for part in text.split('/'))
        if not 1 <= index <= count:
            raise pytest.UsageError(f'--shard {text}: I/N with 1 <= I <= N')
        return cls(index, count)

    def holds(self, nodeid: str) -> bool:
        return zlib.crc32(nodeid.encode()) % self.count == self.index - 1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption('--shard', type=Shard.parse, help='run only the I-th of N parts of the tests, as I/N')


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard: Shard | None = config.getoption('shard')
    if shard is None:
        return
    config.hook.pytest_deselected(items=[item for item in items if not shard.holds(item.nodeid)])
    items[:] = [item for item in items if shard.holds(item.nodeid)]
