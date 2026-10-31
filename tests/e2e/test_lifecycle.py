"""The life of one small campaign, with real agents, on the enwik8 example: claim, submit, judge, cancel, stop.

    make e2e
    uv run pytest -m e2e tests/e2e/test_lifecycle.py -rA [--e2e-harness claude] [--e2e-model <model>]

The campaign starts paused, and two ideas go on its board. Once it resumes, its two workers claim them. The slow one is
cancelled. The lzma worker checks its change with the dev command, submits it, reads its verdict, which the noise of the
baseline decides, and ends its run. The lead drops the slow idea and adds bz2, which the next worker claims, and the
campaign stops while it runs. About 4 minutes and 0.45 USD in each mode (``tests/e2e/conftest.py``).

The tests are the steps of this one campaign. They run in the order of this file, and read the state that earlier
tests leave on the ``Campaign``. ``@needs(...)`` names those earlier tests: when one of them did not pass, the test is
skipped with that reason. So ``-k`` on a test that needs others skips it: select its needs too.
"""

import json
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from piloop.plugins.campaign_type.kit.candidates.ids import JUDGED
from piloop.plugins.runtime.contract import TAG_ENV
from piloop.plugins.runtime.kit.tags import tagged
from tests.conftest import wait_until
from tests.e2e.campaign import CLAIM_THE_BOARD, OPEN, Smoke, needs
from tests.e2e.conftest import MakeCampaign

pytestmark = pytest.mark.e2e
PENDING = f'verdict IS NULL AND origin IN ({", ".join(repr(str(o)) for o in sorted(JUDGED))})'
"""The candidates that wait for their verdict, as SQL."""

MAX_EVALUATIONS = 1
"""One evaluation slot, so that the evaluation queue has something to order."""
DEV = ['uv', 'run', 'python', 'evaluate.py']
"""The dev command that the workers run in their checkouts: the evaluate command itself, on this small example."""
CONFIG = {
    'evaluate_timeout': 300,
    'evaluator': {'dev': DEV},
    'plugins': {
        'role': {
            'implementer': {'max_concurrent': 2, 'timeout': 600},
            'research': {'triggers': []},
            'watchdog': {'triggers': []},
        },
        'stop_rule': {'max_runs': 5, 'max_hours': 1, 'max_tokens': 20_000_000},
    },
}
"""Over the template's ``piloop.toml``: a dev command, two implementers, no sweeps (the tests add the ideas), and stop
conditions only as a backstop (the tests stop the campaign). It scores submissions, by the noise of its baseline: the
defaults."""
NOISE_RUNS = 3
"""``[evaluator] noise_runs``, by default: the baseline's evaluations that give the noise."""
CANCEL_AFTER = 40
"""Seconds that the slow run works before it is cancelled."""
EXIT_GRACE = 10
"""Seconds that a process of the stopped campaign may take to end."""
REQUIRED_EVENTS = (
    'campaign.paused',
    'agent_session.started',
    'run.started',
    'run.cancel_requested',
    'run.submitted',
    'run.end_requested',
    'measurement.recorded',
    'candidate.judged',
    'campaign.stop_requested',
    'campaign.status',
)
"""Event kinds that the feed must show after the snapshot that the event reader starts from."""


@dataclass(frozen=True)
class IdeaSpec:
    """An idea that the tests add, and what it is for."""

    key: str
    title: str
    plan: str
    priority: int


SUBMIT = ' Commit, run `piloop submit`, and end your run with `piloop run end submitted`.'
"""How each plan ends: one submission, then the run's end."""
IDEAS = (
    IdeaSpec(
        'slow',
        'Write an order-2 context-mixing compressor with arithmetic coding',
        'Implement an order-2 context model with an adaptive binary arithmetic coder in compressor.py with numpy.'
        ' Check it with `uv run python evaluate.py`.' + SUBMIT,
        0,
    ),
    IdeaSpec(
        'lzma',
        'Replace zlib with lzma at preset 1',
        'In compressor.py, compress with lzma.compress(data, preset=1) and decompress with lzma.decompress.'
        ' Check it once with `uv run python evaluate.py`.' + SUBMIT + ' Change nothing else.',
        1,
    ),
    IdeaSpec(
        'bz2',
        'Replace zlib with bz2 at its best level',
        'In compressor.py, compress with bz2.compress(data, level) and decompress with bz2.decompress. Try the levels'
        ' 1, 5 and 9, each checked with `uv run python evaluate.py`, and keep the best one.'
        + SUBMIT
        + ' Change nothing else.',
        2,
    ),
)
"""Two workers claim slow and lzma; slow is cancelled; lzma should be a keep; bz2 goes on the board after the cancel,
and runs while the campaign stops.

lzma uses preset 1 (about 1 s) to keep the scenario short."""
FIRST = IDEAS[:2]
"""The ideas on the board when the campaign resumes: one for each worker."""


@pytest.fixture(scope='module')
def live_campaign(make_campaign: MakeCampaign) -> Iterator[Smoke]:
    live_campaign = make_campaign(
        config=CONFIG,
        home_config={
            'plugins': {
                'executor': {'provider': 'workers', 'pools': {'serve': {'slots': {'evaluation': MAX_EVALUATIONS}}}}
            }
        },
        paused='e2e: the ideas go on the board first',
        brief=CLAIM_THE_BOARD,
    )
    live_campaign.start_readers()
    yield live_campaign


def test_init_measured_the_baseline(live_campaign: Smoke) -> None:
    rows = live_campaign.sql(
        'SELECT c.verdict, m.metrics FROM candidates c JOIN measurements m ON m.candidate_id = c.id'
        ' WHERE c.run_id IS NULL'
    )
    assert rows, 'no measured baseline candidate'
    print(f'baseline {rows[0]["verdict"]}: {rows[0]["metrics"]}')


def test_doctor_with_the_daemon_running(live_campaign: Smoke) -> None:
    assert_doctor_passes(live_campaign)


def test_status_json(live_campaign: Smoke) -> None:
    """``piloop status --json`` shows the daemon and the campaign at a cursor, which the event reader starts from."""
    doc = live_campaign.status()
    pid = int((live_campaign.home / 'serve.pid').read_text().split()[0])
    assert doc.daemon.pid == pid, f'daemon {doc.daemon}, serve.pid has {pid}'
    assert doc.cursor > 0
    campaign = live_campaign.campaign_state()
    assert campaign.status == 'active', campaign
    assert campaign.head is not None, campaign
    live_campaign.snapshot = live_campaign.cursor = doc.cursor
    print(f'cursor {doc.cursor}, head {campaign.head}')


def test_pause_holds_launches(live_campaign: Smoke) -> None:
    """The campaign starts paused, and the ideas go on its board: no worker starts, and none of them is claimed."""
    assert live_campaign.campaign_state().paused == 'launches'
    for idea in FIRST:
        live_campaign.add_idea(idea.key, idea.title, idea.plan, idea.priority)
    time.sleep(10)  # a fixed window: nothing must launch during five ticks of the daemon
    assert [dict(r) for r in live_campaign.sql('SELECT id, status FROM runs')] == []
    assert live_campaign.sql("SELECT id FROM agent_sessions WHERE kind = 'implementer'") == []
    assert {idea_status(live_campaign, i.key) for i in FIRST} == {'open'}


@needs(test_pause_holds_launches)
def test_resume_and_the_two_workers_claim_the_two_ideas(live_campaign: Smoke) -> None:
    """Once it resumes, a fresh run starts on each free slot, and each worker claims one idea of the board."""
    live_campaign.piloop('resume', live_campaign.name)

    def claimed() -> list[sqlite3.Row] | None:
        rows = live_campaign.sql("SELECT id, idea_id FROM runs WHERE status = 'running' AND idea_id IS NOT NULL")
        return rows if len(rows) >= 2 else None

    wait_until(claimed, 'two workers that claimed an idea', timeout=300)
    both = claimed()
    assert both is not None
    assert {r['idea_id'] for r in both} == {live_campaign.ideas[i.key] for i in FIRST}, live_campaign.ideas
    assert {idea_status(live_campaign, i.key) for i in FIRST} == {'claimed'}
    print(f'runs {[r["id"] for r in both]} claimed ideas {[r["idea_id"] for r in both]}')


@needs(test_resume_and_the_two_workers_claim_the_two_ideas)
def test_run_cancel(live_campaign: Smoke) -> None:
    """``piloop run cancel`` on the slow run: it closes as failed, its idea is open again, its processes end. The lead
    drops that idea and adds bz2, while the campaign is paused, so the slot that the cancel freed takes bz2."""
    time.sleep(CANCEL_AFTER)  # let the slow run work, so the cancel ends an agent in the middle of its work
    run = live_campaign.run_of('slow')
    assert run is not None and run['status'] == 'running', f'the slow run after {CANCEL_AFTER}s: {run and dict(run)}'
    tokens = live_campaign.process_tags(run['id'])
    live_campaign.piloop('pause', live_campaign.name, '--reason', 'e2e: the board changes after the cancel')
    live_campaign.piloop('run', 'cancel', str(run['id']))
    wait_until(lambda: live_campaign.run_in('slow', OPEN, within=False), timeout=120)
    closed = live_campaign.run_of('slow')
    assert closed is not None
    assert closed['status'] == 'failed', dict(closed)
    # The daemon closes the run, then settles its idea in a later write.
    wait_until(lambda: idea_status(live_campaign, 'slow') != 'claimed', timeout=60)
    assert idea_status(live_campaign, 'slow') == 'open'
    assert [p for t in tokens for p in tagged(f'{TAG_ENV}={t}')] == []
    live_campaign.piloop(
        'idea',
        'status',
        str(live_campaign.ideas['slow']),
        'dropped',
        '--reason',
        'e2e: the lead takes it off the board',
    )
    bz2 = IDEAS[2]
    live_campaign.add_idea(bz2.key, bz2.title, bz2.plan, bz2.priority)
    live_campaign.piloop('resume', live_campaign.name)
    print(f'run {run["id"]} failed ({closed["error"]}), its idea open again, then dropped; no process left')


@needs(test_resume_and_the_two_workers_claim_the_two_ideas)
def test_a_worker_submits_its_verdict_is_the_noise_s_and_it_ends_its_run(live_campaign: Smoke) -> None:
    """The lzma worker submits once and ends its run. Its submission is judged against the head, by the noise of the
    baseline's evaluations, and it keeps. Then a worker claims bz2 in the slot that the cancel freed."""
    wait_until(lambda: live_campaign.run_in('lzma', OPEN, within=False), timeout=900)
    done = live_campaign.run_of('lzma')
    assert done is not None
    assert (done['status'], done['end']) == ('succeeded', 'finished'), f'run {done["id"]}: {done["error"]}'
    submitted = live_campaign.sql(
        "SELECT json_extract(payload, '$.candidate') AS candidate FROM events"
        " WHERE kind = 'run.submitted' AND target_kind = 'run' AND target_id = ?",
        done['id'],
    )
    assert submitted, f'run {done["id"]} submitted nothing'
    candidate = submitted[-1]['candidate']
    wait_until(lambda: verdict(live_campaign, candidate)['verdict'] is not None, timeout=300)
    judged = verdict(live_campaign, candidate)
    assert judged['verdict'] == 'keep', dict(judged)
    assert re.search(r'; noise \S+ over \d+ evaluations\)', judged['verdict_reason']), judged['verdict_reason']
    baseline = live_campaign.sql(
        'SELECT count(*) AS n FROM measurements m JOIN candidates c ON c.id = m.candidate_id WHERE c.run_id IS NULL'
    )
    assert baseline[0]['n'] >= NOISE_RUNS, 'the noise comes from the baseline evaluated noise_runs times'
    ended = live_campaign.sql(
        "SELECT json_extract(payload, '$.how') AS how FROM events WHERE kind = 'run.end_requested' AND target_id = ?",
        done['id'],
    )
    assert [e['how'] for e in ended] == ['submitted']
    wait_until(lambda: live_campaign.run_in('bz2', {'running'}), 'a worker that claimed bz2', timeout=300)
    print(f'run {done["id"]}: candidate#{candidate} {judged["verdict"]}: {judged["verdict_reason"]}')


@needs(test_a_worker_submits_its_verdict_is_the_noise_s_and_it_ends_its_run)
def test_commits_are_judged(live_campaign: Smoke) -> None:
    """Every commit of the lzma run is a candidate: a submission with its verdict, any other one skipped."""
    run = live_campaign.run_of('lzma')
    assert run is not None

    def verdicts() -> list[sqlite3.Row] | None:
        rows = live_campaign.sql(
            'SELECT id, origin, verdict, verdict_reason FROM candidates WHERE run_id = ?', run['id']
        )  # a candidate that waits for its verdict has none yet
        return rows if rows and not any(r['verdict'] is None and r['origin'] in JUDGED for r in rows) else None

    wait_until(verdicts, timeout=300)
    rows = verdicts()
    assert rows is not None
    print('; '.join(f'candidate#{r["id"]} {r["origin"]} {r["verdict"]}: {r["verdict_reason"]}' for r in rows))


@needs(test_commits_are_judged)
def test_candidate_remeasure_keeps_the_head(live_campaign: Smoke) -> None:
    run = live_campaign.run_of('lzma')
    assert run is not None
    head = live_campaign.sql("SELECT id FROM candidates WHERE run_id = ? AND verdict = 'keep'", run['id'])
    assert head, f'run {run["id"]} has no keep to measure again'
    candidate = head[0]['id']
    before = live_campaign.sql('SELECT count(*) AS n FROM measurements WHERE candidate_id = ?', candidate)[0]['n']
    live_campaign.piloop('candidate', 'remeasure', str(candidate))

    def measured() -> list[sqlite3.Row] | None:
        rows = live_campaign.sql('SELECT metrics FROM measurements WHERE candidate_id = ? ORDER BY id', candidate)
        return rows if len(rows) >= before + 3 else None

    wait_until(measured, timeout=120)
    rows = measured()
    assert rows is not None
    assert live_campaign.sql('SELECT verdict FROM candidates WHERE id = ?', candidate)[0]['verdict'] == 'keep'
    print(f'candidate#{candidate}: {rows[0]["metrics"]} then {rows[-1]["metrics"]}, still keep')


@needs(test_resume_and_the_two_workers_claim_the_two_ideas)
def test_graceful_stop(live_campaign: Smoke) -> None:
    """A graceful stop of the campaign while the bz2 implementer runs: it winds down, nothing stays open."""
    bz2 = live_campaign.run_of('bz2')
    at_stop = f'run {bz2["id"]} was {bz2["status"]}' if bz2 else 'no bz2 run'
    live_campaign.stop_campaign('e2e: graceful stop')

    def settled() -> bool:
        open_ = live_campaign.sql(
            "SELECT (SELECT count(*) FROM runs WHERE status IN ('queued', 'running') OR scoring)"
            " + (SELECT count(*) FROM agent_sessions WHERE state IN ('requested', 'starting', 'running'))"
            f' + (SELECT count(*) FROM candidates WHERE {PENDING})'
            ' + (SELECT count(*) FROM evaluations) AS n'
        )
        return open_[0]['n'] == 0

    wait_until(settled, timeout=900)
    campaign = live_campaign.campaign_row()
    assert campaign['status'] == 'stopped', dict(campaign)
    scored = live_campaign.sql(
        'SELECT c.id, c.origin, c.verdict FROM candidates c JOIN runs r ON r.id = c.run_id WHERE r.idea_id = ?',
        live_campaign.ideas['bz2'],
    )
    run = live_campaign.run_of('bz2')
    commits = ', '.join(f'candidate#{c["id"]} {c["origin"]} {c["verdict"]}' for c in scored) or 'no commit'
    print(f'{at_stop}; it ended {run and run["status"]}; {commits}; {campaign["stopped_reason"]!r}')


@needs(test_graceful_stop)
def test_verdict_override_and_measurement_add(live_campaign: Smoke) -> None:
    """``piloop candidate verdict`` overrides the newest judged verdict, and ``piloop measurement add`` records a
    measurement."""
    rows = live_campaign.sql(
        'SELECT c.id, m.metrics FROM candidates c JOIN measurements m ON m.candidate_id = c.id'
        ' WHERE c.run_id IS NOT NULL ORDER BY c.id DESC LIMIT 1'
    )
    assert rows, 'no run has a measured candidate'
    candidate, metrics = rows[0]['id'], json.loads(rows[0]['metrics'])
    live_campaign.piloop('candidate', 'verdict', str(candidate), 'discard', '--reason', 'e2e: override')
    report = live_campaign.dir / 'measurement.json'
    report.write_text(json.dumps({'metrics': metrics, 'seconds': 1}))
    said = live_campaign.piloop('measurement', 'add', str(candidate), '--report', str(report)).strip()
    row = live_campaign.sql('SELECT verdict, verdict_by, verdict_reason FROM candidates WHERE id = ?', candidate)[0]
    assert (row['verdict'], row['verdict_by'], row['verdict_reason']) == ('discard', 'human', 'e2e: override')
    count = live_campaign.sql('SELECT count(*) AS n FROM measurements WHERE candidate_id = ?', candidate)[0]['n']
    assert count >= 2
    print(f'candidate#{candidate} discard by human, {count} measurements; {said}')


def idea_status(live_campaign: Smoke, key: str) -> str:
    return live_campaign.sql('SELECT status FROM ideas WHERE id = ?', live_campaign.ideas[key])[0]['status']


def verdict(live_campaign: Smoke, candidate: int) -> sqlite3.Row:
    return live_campaign.sql('SELECT verdict, verdict_reason FROM candidates WHERE id = ?', candidate)[0]


@needs(test_graceful_stop)
def test_eval_queue_stayed_within_its_slot(live_campaign: Smoke) -> None:
    """``piloop eval queue`` showed the evaluations, within the one slot, and is empty once the campaign settled."""
    live_campaign.sample_queue()
    samples = live_campaign.queue_samples
    assert samples, f'no sample; reader errors {live_campaign.errors[-3:]}'
    busiest = max(r for r, _, _ in samples)
    assert 1 <= busiest <= MAX_EVALUATIONS, samples
    assert samples[-1][:2] == (0, 0)
    waited = max(w for _, w, _ in samples)
    print(f'{len(samples)} samples: at most {busiest} running and {waited} waiting in {MAX_EVALUATIONS} slot')


@needs(test_status_json, test_graceful_stop)
def test_event_feed_across_reconnects(live_campaign: Smoke) -> None:
    """The feed, read page by page from the saved cursor, gives every event after the snapshot once, in order."""
    live_campaign.join_readers()  # the reader thread and this drain must not read from the same cursor at once
    while True:  # drain what the reader thread has not read yet
        before = live_campaign.cursor
        live_campaign.read_events()
        if live_campaign.cursor == before:
            break
    followed = live_campaign.feed(live_campaign.cursor, '--follow', '--timeout', '3')
    seen = [e.id for e in live_campaign.events]
    stored = live_campaign.sql(
        'SELECT id FROM events WHERE id > ? AND id <= ? ORDER BY id', live_campaign.snapshot, live_campaign.cursor
    )
    evidence = f'{live_campaign.event_reads} reads, reader errors {live_campaign.errors[-3:]}'
    assert seen == sorted(set(seen)), f'the feed gave an event twice or out of order; {evidence}'
    assert seen == [r['id'] for r in stored], f'the feed must give every stored event; {evidence}'
    kinds = {e.kind for e in live_campaign.events}
    assert [k for k in REQUIRED_EVENTS if k not in kinds] == [], evidence
    assert followed.events == []
    assert live_campaign.event_reads >= 2, evidence
    print(f'{len(seen)} events in {live_campaign.event_reads} reads, all stored ones, in order; {len(kinds)} kinds')


@needs(test_graceful_stop)
def test_no_anomaly_event(live_campaign: Smoke) -> None:
    """The daemon saw no fault of the environment (``anomaly.*``): every agent started and worked."""
    rows = live_campaign.sql("SELECT id, kind, target_kind, target_id, payload FROM events WHERE kind LIKE 'anomaly.%'")
    assert [dict(r) for r in rows] == []


def test_daemon_stops(live_campaign: Smoke) -> None:
    live_campaign.join_readers()
    live_campaign.stop()
    assert not (live_campaign.home / 'serve.pid').exists()


@needs(test_daemon_stops)
def test_no_orphan_process(live_campaign: Smoke) -> None:
    """No process of the campaign is left: none with its PILOOP_HOME, none with an agent's process tag."""
    # `piloop stop` returns when the daemon releases its lock, a moment before its process ends: an orphan is a
    # process that stays.
    started = time.monotonic()
    try:
        wait_until(lambda: not live_campaign.processes(), timeout=EXIT_GRACE)
    except AssertionError as e:
        raise AssertionError(f'processes left: {live_campaign.processes()}') from e
    tokens = len(live_campaign.process_tags())
    print(f'{tokens} agent tokens checked; the last process ended within {time.monotonic() - started:.1f}s')


@needs(test_daemon_stops)
def test_doctor_with_the_daemon_stopped(live_campaign: Smoke) -> None:
    assert_doctor_passes(live_campaign)


def assert_doctor_passes(live_campaign: Smoke) -> None:
    """``piloop doctor`` has no FAIL line, with the skills read from the temporary directories."""
    out = live_campaign.piloop('doctor', check=False)
    assert [line for line in out.splitlines() if line.startswith('FAIL')] == []
