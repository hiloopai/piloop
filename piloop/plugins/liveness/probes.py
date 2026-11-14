"""``probes``: the built-in liveness policy. It ends an agent that does not start, or that goes quiet.

The policy imports only the Python standard library, pydantic and its contract, as a policy from a plugin does. It is
an entry point of piloop's own ``pyproject.toml``, in ``piloop.liveness_policies``.
"""

from datetime import timedelta

from pydantic import Field

from piloop.db.models import AgentSessionState
from piloop.plugins.liveness.contract import AgentCase, Alive, EscalateStall, LivenessPolicy, Stalled


class Probes(LivenessPolicy):
    """``probes``: end an agent that does not start within ``start_within``, or that writes nothing for ``quiet_for``.

    The start counts from when a worker took the agent's job: an agent whose job waits for a free worker is not stuck.
    The quiet counts from the agent's last snapshot of its session file, which its worker takes at most 30 s after the
    file grows. A tool call writes nothing until it returns, so set ``quiet_for`` above the longest one, such as the
    longest dev evaluation. The ``escalate_at``-th stall of an idea also tells a human, once: the idea stays open.
    """

    start_within: timedelta = Field(default=timedelta(minutes=15), gt=timedelta(0))
    """How long after a worker took an agent's job the agent must run: a bad launch, a hung clone or first transfer."""
    quiet_for: timedelta = Field(default=timedelta(minutes=20), gt=timedelta(0))
    """How long a running agent may write nothing to its session file: a hung harness, model call or tool."""
    escalate_at: int = Field(default=2, ge=1)
    """The stall of an idea, by its count, that escalates, once: by default its second."""

    def check(self, case: AgentCase) -> Alive | Stalled | EscalateStall:
        match case.state, case.running_for, case.quiet_for:
            case AgentSessionState.STARTING, None, _:  # its job waits for a worker
                return Alive(self.start_within)
            case AgentSessionState.STARTING, timedelta() as running, _:
                answer = _probe(running, self.start_within, f'it did not start within {_span(self.start_within)}')
            case _, _, timedelta() as quiet:
                answer = _probe(quiet, self.quiet_for, f'no output for {_span(quiet)}')
            case _:
                return Alive(self.quiet_for)
        if isinstance(answer, Stalled) and case.stalls + 1 == self.escalate_at:
            reason = f'stall {self.escalate_at} of its idea: {answer.reason}; the idea stays open'
            return EscalateStall(reason, then=answer)
        return answer


def _probe(waited: timedelta, limit: timedelta, reason: str) -> Alive | Stalled:
    """Stall once ``waited`` reached ``limit``, else ask again when it will."""
    return Stalled(reason) if waited >= limit else Alive(limit - waited)


def _span(span: timedelta) -> str:
    """``21 min``, in whole minutes down, or ``90 s`` under two minutes."""
    seconds = span.total_seconds()
    return f'{seconds:.0f} s' if seconds < 120 else f'{seconds // 60:.0f} min'
