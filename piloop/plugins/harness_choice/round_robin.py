"""``round_robin``: the built-in harness choice."""

from piloop.plugins.harness_choice.contract import HarnessCase, HarnessChoice


class RoundRobin(HarnessChoice):
    """The n-th agent of a role takes the n-th harness of the rotation, or the harness that resumes its last session.

    A harness that is full passes its turn to the first harness of the rotation that is not.
    """

    def choose(self, case: HarnessCase) -> str | None:
        turn = case.resumes if case.resumes is not None else case.rotation[case.turn % len(case.rotation)]
        return turn if turn not in case.full else next((h for h in case.rotation if h not in case.full), None)
