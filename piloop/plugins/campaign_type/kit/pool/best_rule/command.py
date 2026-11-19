"""``command``: a best rule that is any program the campaign names. ``baseline`` answers first.

piloop writes the case to a JSON file, ``PILOOP_CASE_FILE``. The program writes one line of JSON to
``PILOOP_RESULT_FILE``: ``{"answer": "keep"}`` or ``{"answer": "refuse", "reason": "…"}``. The interface is modelled on
the validating admission webhooks of Kubernetes: one review object in, an allow and a message out, a timeout, and a
failure that fails closed.
"""

import subprocess
import tempfile
from pathlib import Path
from subprocess import PIPE
from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import Case, Keep, Refuse, Unanswered
from piloop.proc import RESULT_ENV, STDERR_IN_ERROR, ResultPipe, child_env, kill_group

CASE_ENV = 'PILOOP_CASE_FILE'
"""The JSON file of the case, in a private temporary directory that piloop removes after the call."""
CASE = TypeAdapter(Case)
"""The case as JSON. Its JSON Schema is on the docs page of best rules."""

type Reason = Annotated[str, StringConstraints(min_length=1, max_length=STDERR_IN_ERROR)]


class KeepResult(BaseModel):
    """``{"answer": "keep"}``, with an optional reason."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    answer: Literal['keep']
    reason: Reason | None = None


class RefuseResult(BaseModel):
    """``{"answer": "refuse", "reason": "…"}``: the reason is what the candidate's worker hears."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    answer: Literal['refuse']
    reason: Reason


RESULT = TypeAdapter(Annotated[KeepResult | RefuseResult, Field(discriminator='answer')])


class CommandRule(Baseline):
    """``command``: when ``baseline`` allows the candidate, run ``command`` and read its answer.

    The program runs in the private temporary directory of the case, with the daemon's environment, in a process group
    of its own. A failed start, a timeout (piloop kills the group), an exit code other than 0, or a result that is not
    exactly one valid line is no answer: the best stays, and piloop escalates.
    """

    command: list[str] = Field(min_length=1)
    """The program (argv). Keep it, and its data, outside the repo. Example: ``["python3", "/srv/rules/paired.py"]``."""
    timeout: float = Field(default=60, gt=0)
    """The seconds that the program can take."""
    version: str | None = None
    """Any value. piloop asks the rule again for each case when the table changes: change it after you edit the
    program."""

    def judge(self, case: Case) -> Keep | Refuse | Unanswered:
        """``baseline``'s refusal, else the program's answer."""
        if isinstance(first := super().judge(case), Refuse):
            return first
        with tempfile.TemporaryDirectory(prefix='piloop-case-') as tmp:
            (path := Path(tmp) / 'case.json').write_bytes(CASE.dump_json(case))
            return self._run(Path(tmp), path)

    def _run(self, tmp: Path, path: Path) -> Keep | Refuse | Unanswered:
        with ResultPipe(tmp / 'result') as results:
            try:
                proc = subprocess.Popen(
                    self.command,
                    cwd=tmp,
                    env=child_env() | {CASE_ENV: str(path), RESULT_ENV: str(results.path)},
                    stdin=subprocess.DEVNULL,
                    stdout=PIPE,
                    stderr=PIPE,
                    start_new_session=True,
                )
            except OSError as e:
                return Unanswered(f'could not start: {e}')
            with proc:
                try:
                    _, err = proc.communicate(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    # Kill the children too. A child that holds the pipe open blocks the read below.
                    kill_group(proc)
                    proc.communicate()
                    return Unanswered(f'timed out after {self.timeout:g}s')
            lines = results.close()
        if proc.returncode != 0:
            return Unanswered(f'exit {proc.returncode}: {err.decode(errors="replace").strip()[-STDERR_IN_ERROR:]}')
        match lines:
            case [line]:
                try:
                    answer = RESULT.validate_json(line)
                except ValidationError as e:
                    return Unanswered(f'wrote a result that is not {{"answer": "keep" | "refuse", "reason": …}}: {e}')
            case _:
                return Unanswered(f'wrote {len(lines)} results to {RESULT_ENV}; one call writes one line')
        match answer:
            case KeepResult(reason=reason):
                return Keep(reason)
            case RefuseResult(reason=reason):
                return Refuse(reason)
            case never:
                assert_never(never)
