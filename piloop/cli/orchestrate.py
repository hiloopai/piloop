"""Start an external orchestrator in a numbered seat of the current campaign."""

import os
from typing import Annotated

import cyclopts
from cyclopts import validators


def orchestrate(
    seat: Annotated[int, cyclopts.Parameter(validator=validators.Number(gte=1))],
    command: str,
    /,
    *args: Annotated[str, cyclopts.Parameter(allow_leading_hyphen=True)],
) -> None:
    """Run COMMAND with the stable identity of numbered SEAT. Use one process per seat in the campaign repo.

    Pass the agent command after --. Each seat shares the campaign board and workers and reads its own inbox.
    """
    inbox_id = f'orchestrator via service:seat-{seat}'
    env = dict(os.environ, PILOOP_SEAT_NUM=str(seat), PILOOP_INBOX_ID=inbox_id, PILOOP_ACTOR=inbox_id)
    os.execvpe(command, [command, *args], env)
