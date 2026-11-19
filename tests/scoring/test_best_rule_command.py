"""The ``command`` best rule: a program that answers keep or refuse, run as a real subprocess.

``baseline`` answers first. Any failure of the program is no answer (``Unanswered``): the best stays.
"""

import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import psutil
import pytest
from pytest_mock import MockerFixture

from piloop.plugins.campaign_type.kit.pool.best_rule.command import CASE, CommandRule
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import Case, CaseSample, Keep, Refuse, Unanswered
from tests.conftest import wait_until
from tests.scoring.test_best_rule import SIZE, case

EXAMPLE = Path(__file__).parents[2] / 'examples' / 'best-rules' / 'paired.py'


def writes(tmp_path: Path, *lines: str, code: int = 0, then: str = '') -> list[str]:
    """A program that writes ``lines`` to its result pipe, copies its case to ``case.json``, and exits with ``code``."""
    script = tmp_path / 'rule.py'
    script.write_text(
        'import os, shutil, sys\n'
        f'shutil.copy(os.environ["PILOOP_CASE_FILE"], {str(tmp_path / "case.json")!r})\n'
        f'open({str(tmp_path / "where")!r}, "w").write(os.environ["PILOOP_CASE_FILE"])\n'
        f'with open(os.environ["PILOOP_RESULT_FILE"], "a") as out:\n'
        f'    out.write({"".join(f"{line}\n" for line in lines)!r})\n'
        f'{then}\n'
        f'sys.exit({code})\n'
    )
    return [sys.executable, '-I', str(script)]


GOOD = case({'score': 12}, {'score': 10})
"""A case that ``baseline`` allows: the program decides."""


@pytest.mark.parametrize(
    ('line', 'answer'),
    [
        ('{"answer": "keep"}', Keep()),
        ('{"answer": "keep", "reason": "significant"}', Keep('significant')),
        ('{"answer": "refuse", "reason": "within noise"}', Refuse('within noise')),
    ],
)
def test_the_program_answers_keep_or_refuse(tmp_path: Path, line: str, answer: Keep | Refuse) -> None:
    assert CommandRule(command=writes(tmp_path, line)).judge(GOOD) == answer


@pytest.mark.parametrize(
    ('lines', 'code', 'error'),
    [
        (['{"answer": "refuse"}'], 0, 'wrote a result that is not'),
        (['{"answer": "refuse", "reason": ""}'], 0, 'wrote a result that is not'),
        (['{"answer": "keep", "why": "x"}'], 0, 'Extra inputs are not permitted'),
        (['{"answer": "defer"}'], 0, 'wrote a result that is not'),
        (['not json'], 0, 'wrote a result that is not'),
        ([], 0, 'wrote 0 results to PILOOP_RESULT_FILE'),
        (['{"answer": "keep"}', '{"answer": "keep"}'], 0, 'wrote 2 results'),
        (['{"answer": "keep"}'], 3, 'exit 3: '),
    ],
)
def test_a_program_that_fails_does_not_answer(tmp_path: Path, lines: list[str], code: int, error: str) -> None:
    found = CommandRule(command=writes(tmp_path, *lines, code=code)).judge(GOOD)
    assert isinstance(found, Unanswered) and error in found.error, found


def test_the_error_of_an_exit_has_the_tail_of_stderr(tmp_path: Path) -> None:
    then = 'sys.stderr.write("x" * 600 + "the cause")'
    found = CommandRule(command=writes(tmp_path, code=2, then=then)).judge(GOOD)
    assert isinstance(found, Unanswered) and found.error.endswith('the cause') and len(found.error) < 520


def test_a_program_that_cannot_start_does_not_answer(tmp_path: Path) -> None:
    found = CommandRule(command=[str(tmp_path / 'missing')]).judge(GOOD)
    assert isinstance(found, Unanswered) and found.error.startswith('could not start: ')


def test_a_program_past_its_timeout_does_not_answer_and_its_group_is_dead(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """The program starts a child that holds the pipe: piloop kills the whole group, not only the program.

    The timeout comes once the child runs, whatever the load: no real deadline races the program's start.
    """
    pid = tmp_path / 'child'
    then = (
        'import subprocess, time\n'
        f'open({str(pid)!r}, "w").write(str(subprocess.Popen(["sleep", "600"]).pid))\n'
        'time.sleep(600)'
    )
    real = subprocess.Popen.communicate

    def late(proc: subprocess.Popen[bytes], input: bytes | None = None, timeout: float | None = None) -> object:
        if timeout is None:
            return real(proc, input)
        wait_until(lambda: pid.exists() and pid.read_text() != '', 'the child to start')
        raise subprocess.TimeoutExpired(proc.args, timeout)

    mocker.patch.object(subprocess.Popen, 'communicate', late)
    found = CommandRule(command=writes(tmp_path, then=then)).judge(GOOD)
    assert found == Unanswered('timed out after 60s')
    child = int(pid.read_text())
    wait_until(lambda: not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE)


def test_baseline_answers_first_so_a_guardrail_breach_never_reaches_the_program(tmp_path: Path) -> None:
    breach = case({'score': 12, 'size': 20}, {'score': 10, 'size': 10}, SIZE)
    found = CommandRule(command=writes(tmp_path, '{"answer": "keep"}')).judge(breach)
    assert found == Refuse('guardrails: size 20 vs the baseline 10 beyond its margin 3')
    assert not (tmp_path / 'case.json').exists(), 'the program never ran'


def test_the_case_file_is_the_case_as_json_in_a_private_directory_that_is_gone_after_the_call(tmp_path: Path) -> None:
    units = {'score': {'well-a': 7.0, 'well-b': 5.0}}
    sample = CaseSample(id=4, rung=None, metrics={'score': 12.0}, units=units)
    rung = CaseSample(id=3, rung='screen', metrics={'score': 11.0}, units={})
    rich = replace(GOOD, candidate=replace(GOOD.candidate, samples=(rung, sample), sealed_samples=(sample,)))
    assert CommandRule(command=writes(tmp_path, '{"answer": "keep"}')).judge(rich) == Keep()
    text = (tmp_path / 'case.json').read_text()
    assert CASE.validate_json(text) == rich
    assert json.loads(text)['candidate']['samples'][1]['units'] == units
    where = Path((tmp_path / 'where').read_text())
    assert not where.exists() and not where.parent.exists(), 'removed after the call'
    assert where.is_relative_to(tempfile.gettempdir()), 'in the temporary directory, never under a worktree'


def units(gains: dict[str, float]) -> Case:
    """A case whose candidate beats the best by ``gains`` per unit, with the folds of ``folds.json``."""
    best = {u: 10.0 for u in gains}
    mine = {u: 10.0 + g for u, g in gains.items()}

    def samples(values: dict[str, float]) -> tuple[CaseSample, ...]:
        return (CaseSample(id=1, rung=None, metrics={'score': sum(values.values())}, units={'score': values}),)

    return replace(
        GOOD,
        candidate=replace(GOOD.candidate, samples=samples(mine)),
        incumbent=replace(GOOD.incumbent, samples=samples(best)),
    )


def test_the_example_paired_rule_keeps_a_significant_gain_that_holds_in_the_folds(tmp_path: Path) -> None:
    """``examples/best-rules/paired.py``, the docs' example, keeps a constant gain and refuses a noisy one."""
    names = [f'u{i}' for i in range(20)]
    folds = tmp_path / 'folds.json'
    folds.write_text(json.dumps({u: f'f{i % 5}' for i, u in enumerate(names)}))
    rule = CommandRule(command=[sys.executable, '-I', str(EXAMPLE), str(folds)])
    steady = rule.judge(units(dict.fromkeys(names, 0.4)))
    assert steady == Keep('paired 90% lower bound +8.000, up in 5 of 5 folds')
    noisy = rule.judge(units({u: (3.0 if i % 5 == 0 else -0.5) for i, u in enumerate(names)}))
    assert isinstance(noisy, Refuse) and noisy.reason.endswith('up in 1 of 5 folds'), noisy
