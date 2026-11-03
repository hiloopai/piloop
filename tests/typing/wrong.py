"""Providers that ty refuses in their own package: ``tests/test_typing.py`` expects one error on each ``# wrong`` line.

ty models pydantic's lax mode, which turns a string into a float, so the wrong parameter here is None.
"""

from piloop_min_gain import MinGain

from piloop.capabilities import Baseline, BestRule, Case, Limits


class Late(Baseline):
    def refuse(self, case: Case) -> bool:  # wrong
        return True


MinGain(min_gain=None)  # wrong
rule: BestRule = Limits()  # wrong
