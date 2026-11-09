"""An upgrade from the last release to this checkout loses nothing, with a run in flight, on a home with real work.

    uv run pytest -m e2e tests/e2e/test_upgrade.py -rA [--e2e-model <model>]

piloop ``RELEASE`` from PyPI runs a small enwik8 campaign: a better and a worse submission in the pool, a cancelled
run, notes, an acknowledged decision, an archive of a done idea's workspace files and a ``research/`` loss table. Its
``config.toml`` and ``piloop.toml`` have keys that the release bridged with a warning or that this checkout removed:
``[agents] relay_wait`` and ``[evaluation] wake_after_tries`` of ``config.toml``, and the implementer's
``finding_grace``. While two agents of the release work, the user upgrades as the upgrading guide says: ``uv tool
install --reinstall <this checkout>`` and ``piloop upgrade``, which refuses the removed keys of ``config.toml`` and
changes nothing until the user deletes them. Then every row, ref, commit and file of the release is still there, the
migrations rewrote only what the guide lists (the acks, research memory, the stored configs' removed keys and their
scoring), the backup is the database before the migration, the new code cancels one agent and scores the other's run,
the removed keys of ``piloop.toml`` fail as unknown keys, and the store restores what both piloops archived.

A home of 0.5 is refused, untouched, with the release to install first.

Claude only: the release does not know the other harnesses' flags of today.
"""

import fnmatch
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import suppress
from pathlib import Path

import psutil
import pytest
import tomli_w

from piloop.api.views import CampaignState
from piloop.db.models import EVENT, LIVE_LEASES, UnknownEvent
from piloop.plugins.campaign_type.kit.candidates.ids import SCORED
from piloop.plugins.runtime.kit.tags import tagged
from tests.conftest import wait_until
from tests.e2e.campaign import (
    CHEAPEST_MODEL,
    CLAIM_THE_BOARD,
    OPEN,
    ROOT,
    STATUS,
    TEMPLATES,
    Smoke,
    held,
    merged,
    needs,
)
from tests.e2e.conftest import Install, caller_env
from tests.e2e.inventory import Changed, Inventory, Renames, differences, take

pytestmark = pytest.mark.e2e

RELEASE = '0.6.0'
RELEASE_HEAD = '0080'
"""The schema revision of core that ``RELEASE`` shipped."""
RENAMES = Renames()
"""The renames since ``RELEASE`` that the upgrading guide documents: none. A rename of a later release goes here."""
DROPPED = {'comments.acked_at', 'comments.acked_by'}
"""The columns that the upgrading guide documents as removed: an item's ack, which moves to ``acks`` (core 0084,
``test_nothing_of_the_release_is_lost``)."""
DROPPED_TABLES = {'lessons', 'documents', 'journal'}
"""The tables of research memory, which core 0085 drops with their rows."""
CONFIG_REWRITES = (
    'plugins.memory.*',
    'plugins.section.*',
    'plugins.role.*.sections',
    'plugins.role.orchestrator.*',
    'plugins.role.research.*',
    'plugins.role.watchdog.*',
    'plugins.role.*.interval',
    'plugins.role.*.incident_prompt',
    'plugins.role.*.routine_prompt',
    'plugins.role.*.follow_head',
    'plugins.trigger.*',
    'plugins.verdict_rule.*',
    'plugins.stop_rule.while_held',
    'plugins.escalation.grace',
    'metrics',
    'evaluator.scoring',
)
"""The keys of the stored ``piloop.toml`` that a migration may change, as patterns: revision 0085 drops
``[plugins.memory]`` and the ``lessons`` section, 0086 the keys of 0.6's removed features (the orchestrator, research
and watchdog roles, their sections, triggers and role keys, the verdict rule, ``while_held``, an escalation's
``grace`` and each metric's ``repeats``: ``metrics`` as a whole, a list), and 0087 and 0088 the scoring key. The keys that a later ``piloop init`` stores again, such as each role's
defaults, are not a migration's."""

QUEUE = re.compile(r'evaluations#\d+: |home/runs/\d+/eval-\d+/')
"""The evaluation queue, rows and worktrees: a daemon's start empties it, and what still needs a measurement asks
again (``Scheduler.reap``)."""
CONFIG = {
    'evaluate_timeout': 300,
    'plugins': {
        'stop_rule': {'max_runs': 10, 'max_hours': 1, 'max_tokens': 20_000_000},
        'role': {'implementer': {'max_concurrent': 2, 'timeout': 600, 'finding_grace': 30}},
    },
}
"""Over the release's enwik8 template: two implementers, and stop conditions only as a backstop. The implementer's
``finding_grace`` is a key that the release bridged with a warning and this checkout removed: the upgrade leaves it for
the user to delete."""
REMOVED_KEYS = {
    ('plugins', 'role', 'implementer', 'finding_grace'): (
        'plugins.role.implementer.finding_grace = 30: Extra inputs are not permitted'
    )
}
"""Each removed key that the campaign's ``piloop.toml`` still has after the upgrade, and how ``piloop doctor`` names
it: an unknown key."""
HOME_CONFIG = {
    # a shared disk with less than [storage]'s default 20 GB free would evict and archive by pressure all along
    'storage': {'min_free_gb': 0},
    'agents': {'relay_wait': 30},
    'evaluation': {'wake_after_tries': 2},
}
"""The home's ``config.toml``. ``[agents] relay_wait``, which the release bridged with a warning, and ``[evaluation]
wake_after_tries``, which it read, are keys that this checkout removed (``REMOVED_HOME_KEYS``)."""
REMOVED_HOME_KEYS = {
    key: f'{".".join(key)}\n  Extra inputs are not permitted'
    for key in (('agents', 'relay_wait'), ('evaluation', 'wake_after_tries'))
}
"""Each removed key of ``config.toml``, and how this checkout names it: every command refuses the file, ``piloop
upgrade`` too, before it changes anything."""
HERE = (
    'Work only in your current directory, your git worktree: edit ./compressor.py there and commit there, never in the'
    ' campaign repo that the brief names.'
)
NOTE = 'Then append one line on what you did and the bytes you measured to ./workspace/NOTES.md, and commit.'
WAIT = (
    "First wait for the data: run `python3 -c \"import os, time; [time.sleep(1) for _ in range(60) if os.path.exists('{hold}')];"
    " print('wait' if os.path.exists('{hold}') else 'go')\"` as a call of its own, never in the background, and run it"
    ' again each time it prints wait. Once it prints go,'
)
"""How a plan holds its agent until the test removes ``{hold}``: a sleep in Python, a minute at a time, because Claude
Code moves a long ``sleep`` to the background and goes on."""
SUBMIT = ' Then run `piloop submit`, and end your run with `piloop run end submitted`.'
"""How each plan that the release finishes ends: one submission, then the run's end."""
IDEAS = {
    'cancelled': ('Wait for the data, then use lzma at preset 0', f'{WAIT} use lzma preset 0.', 0),
    'better': (
        'Replace zlib with lzma at preset 1',
        f'{HERE} In compressor.py, compress with lzma.compress(data, preset=1) and decompress with lzma.decompress.'
        f' Run `uv run python evaluate.py`. Change nothing else. {NOTE}{SUBMIT}',
        1,
    ),
    'worse': (
        'Lower the zlib level to 1',
        f'{HERE} In compressor.py, change the zlib level from 9 to 1. Run `uv run python evaluate.py`. Change nothing'
        f' else. {NOTE}{SUBMIT}',
        2,
    ),
}
"""Two implementers claim cancelled and better; the cancel frees a slot for worse. Better beats the baseline, and worse
loses to it."""
IN_FLIGHT = (
    'Wait for the data, then use lzma at preset 2',
    f'{HERE} {WAIT} in compressor.py, use lzma.compress(data, preset=2) and lzma.decompress. Run'
    f' `uv run python evaluate.py`, then commit. Change nothing else, and write nothing in ./workspace.{SUBMIT}',
    3,
)
"""The idea whose agent works through the upgrade: it waits until the test lets it go, after the upgrade. It and
``TO_CANCEL`` go on the board after the cancel, below the others, so the slots that better and worse free take them."""
TO_CANCEL = (
    'Wait long for the data, then use lzma at preset 3',
    f'{HERE} {WAIT} use lzma preset 3, measure and commit.',
    4,
)
"""The idea of a second agent of the release, which the new code cancels after the upgrade."""
LIVE = ('NOTES.md', 'research')
"""Workspace entries that the store never archives (``ArchivePolicy.live``)."""
LIVE_STATES = {'requested', 'starting', 'running'}
"""The states of an agent that is not done."""
OLD_SHAPE = 'old-shape'
OLD_SHAPE_CONFIG = Path(__file__).parents[1] / 'fixtures' / 'configs' / '0080-30.json'
"""A second campaign, paused, with a stored config of a real home as the release left it
(``tests/db/test_stored_configs.py``), with codex among its harnesses."""
PLANTED_BACKUPS = [f'piloop.db.bak-{RELEASE_HEAD}-2025010{day}T000000Z' for day in (1, 2, 3)]
"""Backups that piloop wrote before the upgrade, oldest first: the upgrade's own backup pushes out the oldest."""
NOT_BACKUPS = [
    'piloop.db.bak-before-upgrade',
    f'{PLANTED_BACKUPS[-1]}-shm',
    f'{PLANTED_BACKUPS[-1]}-wal',
]
"""A copy that the user saved, and what SQLite leaves next to a backup that something opened: never pruned."""


class Upgrade:
    """The release's campaign, and what the steps learn of it."""

    def __init__(self, smoke: Smoke, root: Path) -> None:
        self.smoke = smoke
        self.root = root
        self.before: Inventory | None = None
        self.after: Inventory | None = None
        self.output = ''
        self.archive: int | None = None
        """The id of the archive that the release wrote, of the better idea's workspace files."""
        self.in_flight: int | None = None
        """The id of the run that the upgrade happens in."""
        self.to_cancel: int | None = None
        """The id of the other run in flight, which the new code cancels."""
        self.leftovers: list[str] = []
        """The processes of the cancelled agent that were still alive 30 seconds after the cancel."""

    def transcripts(self) -> list[Path]:
        """The Claude Code session directories of the agents of this campaign: Claude Code names each after its cwd."""
        projects = (
            Path(self.smoke.env.get('CLAUDE_CONFIG_DIR') or Path(self.smoke.env['HOME']) / '.claude') / 'projects'
        )
        return sorted(projects.glob(re.sub(r'[^A-Za-z0-9]', '-', str(self.root)) + '*'))

    def take(self) -> Inventory:
        return take(self.smoke.home, self.smoke.repo, self.transcripts())

    def install(self, requirement: str) -> None:
        """``uv tool install --reinstall <requirement>``, as the user does, into this test's tool directory."""
        subprocess.run(
            ['uv', 'tool', 'install', '--reinstall', '--quiet', '--python', '3.14', requirement],
            env=self.smoke.env,
            check=True,
        )

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """``piloop <args>`` of the installed piloop, without a check: the step reads the exit status."""
        return subprocess.run(['piloop', *args], env=self.smoke.env, capture_output=True, text=True, timeout=300)

    def held(self, plan: str, key: str) -> str:
        """``plan`` with its wait on a hold file of its own, which ``release`` removes. A plan without a wait is as it is."""
        hold = self.root / 'holds' / key.replace(' ', '-')
        hold.parent.mkdir(exist_ok=True)
        hold.touch()
        return plan.format(hold=hold)

    def release(self, key: str) -> None:
        (self.root / 'holds' / key.replace(' ', '-')).unlink()

    @property
    def workspace(self) -> Path:
        return self.smoke.home / 'projects' / '1' / 'workspace'


@pytest.fixture(scope='module')
def upgrade(request: pytest.FixtureRequest, e2e_summary: list[str]) -> Iterator[Upgrade]:
    """Install the release as a uv tool of its own, and start its campaign as its user would."""
    if (harness := request.config.getoption('e2e_harness')) != 'claude':
        pytest.skip(f'the release runs claude only, not {harness}')
    model = request.config.getoption('e2e_model') or CHEAPEST_MODEL['claude']
    root = Path(tempfile.mkdtemp(prefix='piloop-e2e-upgrade-'))
    base = caller_env(root / 'home')
    env = base | {
        'PATH': f'{root / "bin"}{os.pathsep}{base["PATH"]}',
        'UV_TOOL_DIR': str(root / 'uv-tools'),
        'UV_TOOL_BIN_DIR': str(root / 'bin'),
        'PILOOP_AGENT_SKILLS_DIR': str(root / 'skills' / 'agents'),
        'PILOOP_CLAUDE_SKILLS_DIR': str(root / 'skills' / 'claude'),
    }
    smoke = Smoke(root, 'enwik8', env)
    smoke.home.mkdir(parents=True)
    (smoke.home / 'config.toml').write_text(tomli_w.dumps(HOME_CONFIG))
    up = Upgrade(smoke, root)
    up.install(f'piloop=={RELEASE}')
    started = time.monotonic()
    try:
        assert smoke.piloop('--version').strip() == f'piloop {RELEASE}'
        tool_python = root / 'uv-tools' / 'piloop' / 'bin' / 'python'
        templates = subprocess.run(
            [
                str(tool_python),
                '-I',  # the release's piloop: in the cwd, the checkout's would come first on sys.path
                '-c',
                'import piloop.plugins.campaign_type.optimization as o, pathlib; print(pathlib.Path(o.__file__).parent)',
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        shutil.copytree(Path(templates) / 'templates' / 'enwik8', smoke.repo)
        template = tomllib.loads((smoke.repo / 'piloop.toml').read_text())
        toml = merged(merged(template, CONFIG), {'plugins': {'harness': {'claude': {'model': model}}}})
        (smoke.repo / 'piloop.toml').write_text(tomli_w.dumps(held(toml, smoke.hold)))
        with (smoke.repo / 'brief.md').open('a') as f:
            f.write(f'\n{CLAIM_THE_BOARD}\n')
        smoke.piloop('install-skills')
        smoke.init_paused(smoke.repo, 'enwik8', 'e2e: the ideas go on the board first')
        yield up
    finally:
        try:
            smoke.shut_down()
        finally:
            e2e_summary.extend(
                [
                    f'test_upgrade: {RELEASE} → this checkout, model {model},'
                    f' {(time.monotonic() - started) / 60:.1f} minutes',
                    *smoke.summary(),
                ]
            )


def test_the_release_does_real_work(upgrade: Upgrade) -> None:
    """A better and a worse candidate, a cancelled run, notes, an acknowledged decision, an archive and a loss table,
    all written by the release."""
    smoke = upgrade.smoke
    for key, (title, plan, priority) in IDEAS.items():
        smoke.add_idea(key, title, upgrade.held(plan, key), priority)
    # the better idea's own entry, which the release archives once the idea is done; the release makes the workspace
    # with its first agent
    upgrade.workspace.mkdir(parents=True, exist_ok=True)
    result = {'status': 'ok', 'score': 3342035, 'direction': 'lower', 'metrics': {'seconds': 0.82}}
    (upgrade.workspace / f'idea{smoke.ideas["better"]}-eval.txt').write_text(json.dumps(result) + '\n')
    smoke.piloop('resume', smoke.name)
    wait_until(lambda: smoke.run_in('cancelled', {'running'}), timeout=600)
    time.sleep(15)  # into its wait, so the cancel ends an agent in the middle of its work
    cancelled = smoke.run_of('cancelled')
    assert cancelled is not None
    # dropped first: a cancelled run gives its idea back to the board, and a free slot would claim it again
    smoke.piloop('idea', 'status', str(smoke.ideas['cancelled']), 'dropped', '--reason', 'e2e: cancelled')
    smoke.piloop('run', 'cancel', str(cancelled['id']))
    for key, (title, plan, priority) in (('in flight', IN_FLIGHT), ('to cancel', TO_CANCEL)):
        smoke.add_idea(key, title, upgrade.held(plan, key), priority)
    for key in ('better', 'worse'):
        wait_until(lambda key=key: idea_status(smoke, key) == 'done', timeout=900)
    [baseline] = mean_bytes(smoke, "c.origin = 'baseline'")
    scores = {key: min(mean_bytes(smoke, 'r.idea_id = ?', smoke.ideas[key])) for key in ('better', 'worse')}
    assert scores['better'] < baseline < scores['worse'], (baseline, scores)
    wait_until(lambda: smoke.run_in('cancelled', OPEN, within=False), timeout=120)
    cancelled = smoke.run_of('cancelled')
    assert cancelled is not None and (cancelled['status'], cancelled['end']) == ('failed', 'cancelled'), dict(
        cancelled or {}
    )
    smoke.piloop(
        'sql',
        'INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES'
        f" ('idea', {smoke.ideas['worse']}, 'human', 'note', 'a lower level loses bytes, as expected')",
    )
    smoke.piloop(
        'sql',
        'INSERT INTO comments (target_kind, target_id, author, kind, body, priority, acked_at, acked_by) VALUES'
        " ('campaign', 1, 'orchestrator', 'decision', 'decision: lzma before any context model', 3,"
        " '2026-01-01 00:00:00', 'human')",
    )
    workspace = upgrade.workspace
    (workspace / 'research').mkdir(parents=True, exist_ok=True)
    (workspace / 'research' / 'loss-table.md').write_text('| idea | bytes |\n|---|---|\n| lzma 1 | see candidate |\n')
    (workspace / 'research' / 'residuals.bin').write_bytes(os.urandom(200_000))
    with (workspace / 'NOTES.md').open('a') as notes:  # the implementers may have written some already
        notes.write('- human: lzma beats zlib here; try the presets next\n')
    # what the store archives after the upgrade, when the campaign stops
    (workspace / 'scratch').mkdir(exist_ok=True)
    (workspace / 'scratch' / 'bulk.bin').write_bytes(os.urandom(300_000))
    archived = (
        "SELECT id FROM archives WHERE subject_kind = 'idea' AND subject_id = ? AND complete AND manifest IS NOT NULL"
    )
    wait_until(lambda: smoke.sql(archived, smoke.ideas['better']), timeout=300)
    upgrade.archive = smoke.sql(archived, smoke.ideas['better'])[0]['id']
    log = subprocess.run(['git', 'log', '--format=%s', 'main'], cwd=smoke.repo, capture_output=True, text=True).stdout
    assert log.splitlines() == ['enwik8: baseline'], f'an agent committed in the campaign repo, not its worktree: {log}'
    print(f'bytes by idea {scores}; cancelled run {cancelled["id"]}; archive#{upgrade.archive} of the better idea')


@needs(test_the_release_does_real_work)
def test_two_agents_of_the_release_are_in_flight(upgrade: Upgrade) -> None:
    """The release starts the two agents that the upgrade happens under, on the slots that better and worse freed, and
    they get into their wait."""
    smoke = upgrade.smoke
    wait_until(lambda: smoke.run_in('in flight', {'running'}) and smoke.run_in('to cancel', {'running'}), timeout=300)
    time.sleep(20)  # the agents are in their wait
    run, other = smoke.run_in('in flight', {'running'}), smoke.run_in('to cancel', {'running'})
    assert run is not None and other is not None, 'a run in flight ended before the upgrade'
    upgrade.in_flight, upgrade.to_cancel = run['id'], other['id']


@needs(test_two_agents_of_the_release_are_in_flight)
def test_upgrade_with_an_agent_in_flight(upgrade: Upgrade) -> None:
    """The user's upgrade, as the guide says, while two agents of the release work. It refuses the keys of
    ``config.toml`` that this checkout removed, and changes nothing; once the user deletes them, it upgrades. Its last
    step, ``piloop doctor``, fails on the keys of ``piloop.toml`` that this checkout removed, and only on those."""
    smoke = upgrade.smoke
    assert smoke.run_in('in flight', {'running'}) is not None, 'the in-flight run ended before the upgrade'
    add_old_shape(upgrade)
    pid = int((smoke.home / 'serve.pid').read_text())
    upgrade.install(str(ROOT))
    refused = upgrade.run('upgrade')
    assert refused.returncode != 0, refused.stdout
    assert all(named in refused.stderr for named in REMOVED_HOME_KEYS.values()), refused.stderr
    assert smoke.sql('SELECT version_num FROM alembic_version')[0]['version_num'] == RELEASE_HEAD
    assert list(smoke.home.glob('piloop.db.bak-*')) == []
    assert int((smoke.home / 'serve.pid').read_text()) == pid
    delete_keys(smoke.home / 'config.toml', REMOVED_HOME_KEYS)
    upgrade.before = upgrade.take()
    for name in (*PLANTED_BACKUPS, *NOT_BACKUPS):
        (smoke.home / name).write_bytes(b'a copy')
    done = upgrade.run('upgrade')
    upgrade.output = done.stdout
    upgrade.after = upgrade.take()
    lines = done.stdout.splitlines()
    assert f'daemon: pid {pid} runs the new code' in lines, done.stdout + done.stderr
    # OLD_SHAPE runs codex, which this machine may lack
    failed = [
        line for line in lines if line.startswith('FAIL') and not line.startswith('FAIL  harnesses: not on PATH:')
    ]
    assert len(failed) == 1 and 'piloop.toml' in failed[0], done.stdout
    assert next(iter(REMOVED_KEYS.values())) in failed[0] + done.stdout, done.stdout
    [backup] = [p.name for p in smoke.home.glob(f'piloop.db.bak-{RELEASE_HEAD}-*Z') if p.name not in PLANTED_BACKUPS]
    left = sorted(p.name for p in smoke.home.glob('piloop.db.bak-*'))
    assert left == sorted([*PLANTED_BACKUPS[1:], backup, *NOT_BACKUPS]), left
    assert done.returncode != 0 and 'piloop doctor found problems' in done.stderr, done.stderr
    assert upgrade.before.revision == RELEASE_HEAD and upgrade.after.revision != RELEASE_HEAD
    assert smoke.run_in('in flight', {'running'}) is not None, 'the upgrade ended the in-flight run'
    upgrade.release('in flight')
    print(done.stdout)


@needs(test_upgrade_with_an_agent_in_flight)
def test_status_without_a_campaign_named_answers_after_the_upgrade(upgrade: Upgrade) -> None:
    """The check after every upgrade: ``piloop status``, in JSON and as text, with no campaign named. The campaign of
    the old stored config is valid; then it goes, so that the steps after see one campaign."""
    smoke = upgrade.smoke
    try:
        as_json, as_text = upgrade.run('status', '--json'), upgrade.run('status')
        assert as_json.returncode == 0, as_json.stderr
        assert as_text.returncode == 0, as_text.stderr
        listed = {c.name: c for c in STATUS.validate_json(as_json.stdout).campaigns}
        assert listed and set(listed) <= {smoke.name, OLD_SHAPE}, listed
        [old] = STATUS.validate_json(smoke.piloop('status', OLD_SHAPE, '--json')).campaigns
        assert isinstance(old, CampaignState), old
        print(as_text.stdout)
    finally:
        smoke.piloop('sql', f"DELETE FROM campaigns WHERE name = '{OLD_SHAPE}'")


@needs(test_upgrade_with_an_agent_in_flight)
def test_nothing_of_the_release_is_lost(upgrade: Upgrade) -> None:
    """Every row, ref, commit and file is still there; a cell differs only by a documented rewrite, or because the
    campaign wrote it: an open row, the stored config's documented rewrite, the files of the agents in flight."""
    assert upgrade.before is not None and upgrade.after is not None
    before, after = upgrade.before, upgrade.after
    found = differences(before, after, RENAMES)
    assert (after.integrity, after.foreign_key_violations) == ('ok', 0)
    open_rows = opened(before)
    unexpected = [c for c in found.changed if not allowed(c, open_rows)]
    assert unexpected == [], '\n'.join(map(str, unexpected))
    live = live_dirs(before)  # an agent at work changes its own directories, and they go when it ends
    files = [f for f in found.files if not any(f'/{n}/' in f or f'-{n.replace("/", "-")}-' in f for n in live)]
    lost = [d for d in found.lost() if d not in found.files and d not in DROPPED] + files
    lost = [d for d in lost if not QUEUE.match(d) and not dropped(d, before)]
    assert lost == [], '\n'.join(lost)
    kinds = [{r['id'] for r in i.tables['comment_kinds']} for i in (before, after)]
    assert kinds[0] <= kinds[1], 'every kind of comment is still there'
    candidates = {r['revision'] for r in before.tables['candidates']}
    assert candidates <= after.commits, 'every candidate commit is still in the repo'
    acked = {(r['__rowid'], r['acked_by']) for r in before.tables['comments'] if r['acked_at'] is not None}
    acks = {(r['comment_id'], r['acked_by']) for r in after.tables['acks']}
    assert acked and acked <= acks, 'each ack of the release stays, in acks'
    worktree = next(w for p, w in before.worktrees.items() if p.endswith(f'runs/{upgrade.in_flight}/worktree'))
    kept = after.worktrees.get(worktree.path)
    assert kept is not None and kept.head is not None and worktree.head is not None, after.worktrees
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', worktree.head, kept.head], cwd=upgrade.smoke.repo)
    assert ancestor.returncode == 0, "the in-flight run's worktree stays, at its commit or one its agent made since"
    assert after.head == before.head
    rows = {t: len(r) for t, r in before.tables.items()}
    print(
        f'{sum(rows.values())} rows {rows}, {len(before.refs)} refs, {len(before.commits)} commits,'
        f' {len(before.files)} files: none lost; {len(found.changed)} cells changed, each allowed;'
        f' {len(found.files) - len(files)} files of the agents at work ({", ".join(live)}) changed or went'
    )


@needs(test_upgrade_with_an_agent_in_flight)
def test_each_run_of_the_release_has_its_end(upgrade: Upgrade) -> None:
    """``runs.end`` of each finished run of the release stays, and every run that is not open has one."""
    smoke = upgrade.smoke
    ends = {key: (run['status'], run['end']) for key in IDEAS if (run := smoke.run_of(key)) is not None}
    assert ends == {
        'cancelled': ('failed', 'cancelled'),
        'better': ('succeeded', 'finished'),
        'worse': ('succeeded', 'finished'),
    }, ends
    unended = smoke.sql('SELECT id FROM runs WHERE "end" IS NULL AND status NOT IN (?, ?)', *OPEN)
    assert unended == [], [dict(r) for r in unended]


@needs(test_upgrade_with_an_agent_in_flight)
def test_the_new_code_cancels_an_agent_of_the_release(upgrade: Upgrade) -> None:
    """``piloop run cancel`` on the new code ends an agent that the release started, and its run fails as cancelled.
    Its idea is dropped first, so no free slot claims it again."""
    smoke = upgrade.smoke
    assert upgrade.before is not None
    [session] = [
        r['id']
        for r in upgrade.before.tables['agent_sessions']
        if (r['subject_kind'], r['subject_id']) == ('run', upgrade.to_cancel)
    ]
    marker = f'PILOOP_ACTOR=implementer#{session}'  # what the release put in the environment of its agent
    assert tagged(marker), 'the agent of the release still runs'
    smoke.piloop('idea', 'status', str(smoke.ideas['to cancel']), 'dropped', '--reason', 'e2e: cancelled')
    smoke.piloop('run', 'cancel', str(upgrade.to_cancel))
    wait_until(lambda: smoke.run_in('to cancel', OPEN, within=False), timeout=120)
    run = smoke.run_of('to cancel')
    assert run is not None and (run['status'], run['end']) == ('failed', 'cancelled'), run and dict(run)
    home = f'PILOOP_HOME={smoke.home}'

    def left() -> list[str]:
        """The processes of the cancelled agent: its signature and this home, so another campaign's never match."""
        ours = {pid for pid, _, _ in tagged(home)}
        return [f'{pid} {cmd[:100]}' for pid, _, cmd in tagged(marker) if pid in ours]

    with suppress(AssertionError):
        wait_until(lambda: not left(), timeout=30)
    upgrade.leftovers = left()
    for line in upgrade.leftovers:  # so that the steps after this one see only their own processes
        with suppress(psutil.Error):
            psutil.Process(int(line.split()[0])).kill()
    print(f'run {run["id"]} {run["status"]}, cancelled; processes of implementer#{session} left: {upgrade.leftovers}')


@needs(test_the_new_code_cancels_an_agent_of_the_release)
def test_the_cancel_ends_every_process_of_the_release_agent(upgrade: Upgrade) -> None:
    """Its tool calls run in sessions of their own (Claude Code's Bash), outside the process group of the agent."""
    assert upgrade.leftovers == []


@needs(test_upgrade_with_an_agent_in_flight)
def test_the_run_in_flight_is_scored_by_the_new_code(upgrade: Upgrade) -> None:
    """The run that the release started ends on the new code, which scores each of its commits into the pool."""
    smoke = upgrade.smoke

    def done() -> sqlite3.Row | None:
        rows = smoke.sql(
            'SELECT * FROM runs WHERE id = ? AND status NOT IN (?, ?) AND NOT scoring', upgrade.in_flight, *OPEN
        )
        return rows[0] if rows else None

    wait_until(done, timeout=600)
    run = done()
    assert run is not None and run['status'] == 'succeeded', run and dict(run)
    scored = smoke.sql(
        "SELECT c.id, c.origin, (SELECT json_extract(e.payload, '$.told') FROM events e"
        " WHERE e.kind = 'candidate.scored' AND e.target_kind = 'candidate' AND e.target_id = c.id"
        ' ORDER BY e.id DESC LIMIT 1) AS told FROM candidates c WHERE c.run_id = ?',
        upgrade.in_flight,
    )
    assert scored and not any(r['told'] is None and r['origin'] in SCORED for r in scored), [dict(r) for r in scored]
    print(f'run {upgrade.in_flight} {run["status"]}: ' + ', '.join(f'candidate#{r["id"]} {r["told"]}' for r in scored))


@needs(test_the_run_in_flight_is_scored_by_the_new_code)
def test_the_store_restores_what_both_piloops_archived(upgrade: Upgrade) -> None:
    """Stopping the campaign archives what is left; ``piloop restore`` brings back every file that the release and the
    new code archived, byte for byte, the release's through its manifest stored again."""
    smoke = upgrade.smoke
    smoke.stop_campaign('e2e: archive the workspace')
    wait_until(lambda: smoke.sql('SELECT id FROM archives WHERE subject_kind IS NULL AND complete'), timeout=600)
    verified = smoke.piloop('store', 'verify').strip()
    assert re.search(r'(\d+) blob\(s\): \1 ok, 0 missing, 0 corrupt$', verified), verified
    workspace = upgrade.workspace
    aside = smoke.dir / 'workspace-aside'
    workspace.rename(aside)
    workspace.mkdir()
    for name in LIVE:  # never archived: they stay where they are
        if (aside / name).exists():
            shutil.move(aside / name, workspace / name)
    smoke.piloop('restore', smoke.name)
    archived = {str(p.relative_to(aside)): p.read_bytes() for p in aside.rglob('*') if p.is_file()}
    restored = {str(p.relative_to(workspace)): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    assert {f'idea{smoke.ideas["better"]}-eval.txt', 'scratch/bulk.bin'} <= set(archived), sorted(archived)
    assert {k: restored.get(k) for k in archived} == archived
    print(f'{verified}; restored {len(archived)} file(s) byte for byte')


@needs(test_upgrade_with_an_agent_in_flight)
def test_the_removed_keys_load_once_the_user_deletes_them(upgrade: Upgrade) -> None:
    """The upgrade leaves ``piloop.toml`` as it was. ``piloop doctor`` names each removed key as an unknown key; without
    them the file loads, and ``piloop init`` re-reads it."""
    smoke = upgrade.smoke
    assert upgrade.before is not None and upgrade.after is not None
    assert upgrade.after.files['repo/piloop.toml'] == upgrade.before.files['repo/piloop.toml']
    for key, named in REMOVED_KEYS.items():
        proc = upgrade.run('doctor')
        # a warning, not a failure, once the campaign is stopped: the daemon reads the file only at `piloop init`
        found = [line for line in proc.stdout.splitlines() if line.startswith(('FAIL', 'WARN'))]
        assert len(found) == 1 and named in proc.stdout, proc.stdout
        delete_keys(smoke.repo / 'piloop.toml', [key])
        print(f'{key}: {next(line.strip() for line in proc.stdout.splitlines() if named in line)[:200]}')
    smoke.piloop('init', str(smoke.repo))
    proc = upgrade.run('doctor')
    assert [line for line in proc.stdout.splitlines() if line.startswith('FAIL')] == [], proc.stdout
    assert 'campaign enwik8: ' in proc.stdout and 'piloop.toml loads' in proc.stdout
    warnings = [line for line in proc.stderr.splitlines() if 'Warning' in line]
    assert all('PiloopDeprecationWarning' in w for w in warnings), proc.stderr


@needs(test_upgrade_with_an_agent_in_flight)
def test_no_process_of_the_release_is_left(upgrade: Upgrade) -> None:
    """``piloop stop`` on the new code ends every process of the campaign, those that the release started too."""
    smoke = upgrade.smoke
    smoke.piloop('stop', '--timeout', '60')
    try:
        wait_until(lambda: not smoke.processes(), timeout=10)
    except AssertionError as e:
        raise AssertionError(f'processes left: {smoke.processes()}') from e


def test_a_home_of_0_5_is_refused_and_left_as_it_was(piloop_install: Install, tmp_path: Path) -> None:
    """This piloop migrates from ``RELEASE``'s schema only: it names the release to install first, and changes
    nothing, on a database that piloop 0.5.0 created."""
    home = tmp_path / 'home'
    env = piloop_install.env(home) | {
        'UV_TOOL_DIR': str(tmp_path / 'uv-tools'),
        'UV_TOOL_BIN_DIR': str(tmp_path / 'bin'),
    }
    subprocess.run(['uv', 'tool', 'install', '--quiet', '--python', '3.14', 'piloop==0.5.0'], env=env, check=True)
    subprocess.run([str(tmp_path / 'bin' / 'piloop'), 'migrate'], env=env, check=True, capture_output=True)
    before = (home / 'piloop.db').read_bytes()
    proc = subprocess.run(['piloop', 'migrate'], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert 'is from piloop 0.5 (schema revision core 0073' in proc.stderr, proc.stderr
    assert "install piloop 0.6 (`uv tool install 'piloop==0.6.*'`)" in proc.stderr, proc.stderr
    assert (home / 'piloop.db').read_bytes() == before
    assert list(home.glob('piloop.db.bak-*')) == []


def add_old_shape(upgrade: Upgrade) -> None:
    """Register ``OLD_SHAPE`` with the release's ``piloop sql``, paused: its repo has a ``piloop.toml`` that loads."""
    repo = upgrade.root / OLD_SHAPE
    repo.mkdir()
    shutil.copy(TEMPLATES / 'enwik8' / 'piloop.toml', repo)
    subprocess.run(['git', 'init', '--quiet'], cwd=repo, check=True)
    config = json.dumps(json.loads(OLD_SHAPE_CONFIG.read_text())).replace("'", "''")
    upgrade.smoke.piloop(
        'sql',
        'INSERT INTO campaigns (name, path, config, paused, paused_reason) VALUES'
        f" ('{OLD_SHAPE}', '{repo}', '{config}', 'all', 'e2e: an old stored config')",
    )


def live_dirs(inventory: Inventory) -> list[str]:
    """``runs/<id>`` and ``agent_sessions/<id>`` of every agent at work in ``inventory``: its files under the home and
    its transcripts, by these names."""
    live = [r for r in inventory.tables['agent_sessions'] if r['state'] in LIVE_STATES]
    return [f'runs/{r["subject_id"]}' if r['subject_kind'] == 'run' else f'agent_sessions/{r["id"]}' for r in live]


def delete_keys(path: Path, keys: Iterable[tuple[str, ...]]) -> None:
    """Delete each key from the TOML file at ``path``, as the user does."""
    toml = tomllib.loads(path.read_text())
    for *tables, last in keys:
        table = toml
        for name in tables:
            table = table[name]
        del table[last]
    path.write_text(tomli_w.dumps(toml))


def mean_bytes(smoke: Smoke, where: str, *params: object) -> list[float]:
    """The mean ``bytes`` of each measured candidate that ``where`` selects, of ``c`` the candidate and ``r`` its run."""
    rows = smoke.sql(
        "SELECT AVG(json_extract(m.metrics, '$.bytes')) AS bytes FROM candidates c LEFT JOIN runs r ON r.id = c.run_id"
        ' JOIN measurements m ON m.candidate_id = c.id AND m.error IS NULL AND m.rung IS NULL'
        f' WHERE {where} GROUP BY c.id',
        *params,
    )
    return [r['bytes'] for r in rows]


def idea_status(smoke: Smoke, key: str) -> str:
    return smoke.sql('SELECT status FROM ideas WHERE id = ?', smoke.ideas[key])[0]['status']


def opened(inventory: Inventory) -> set[tuple[str, object]]:
    """``(table, rowid)`` of the rows that the campaign still changes: open runs and their candidates, live agents,
    claimed ideas, the campaign row, the live leases, and the workers that renew them: a worker restarts on the new
    piloop, says its version, and renews the leases of the agents in flight."""
    rows = {('campaigns', r['__rowid']) for r in inventory.tables['campaigns']}
    rows |= {('lease_workers', r['__rowid']) for r in inventory.tables['lease_workers']}
    for table, column, values in (
        ('runs', 'status', OPEN),
        ('agent_sessions', 'state', LIVE_STATES),
        ('ideas', 'status', {'claimed'}),
        ('leases', 'state', {str(s) for s in LIVE_LEASES}),
    ):
        rows |= {(table, r['__rowid']) for r in inventory.tables[table] if r[column] in values}
    runs = {rowid for table, rowid in rows if table == 'runs'}
    rows |= {('candidates', r['__rowid']) for r in inventory.tables['candidates'] if r['run_id'] in runs}
    return rows


def dropped(lost: str, before: Inventory) -> bool:
    """A loss that the upgrading guide documents: a table of research memory, the ``memory/local`` row of
    ``migrations`` (core 0085), or the token of an agent that had ended (core 0083)."""
    table, _, rest = lost.partition('#')
    if lost.partition(' ')[0] in DROPPED_TABLES:
        return True
    if table == 'migrations':
        return '"memory/local"' in rest
    if table == 'tokens':
        rowid = int(rest.partition(':')[0])
        [token] = [r for r in before.tables['tokens'] if r['__rowid'] == rowid]
        ended = {r['id'] for r in before.tables['agent_sessions'] if r['state'] not in LIVE_STATES}
        return token['agent_session_id'] in ended
    return False


def allowed(change: Changed, open_rows: set[tuple[str, object]]) -> bool:
    """A change of a row the campaign still changes; for the campaign row, only its time, signer and the documented
    rewrite of its stored config; for an event, the documented rewrite to its model."""
    match change.table, change.column:
        case 'campaigns', 'config':
            return all(
                any(fnmatch.fnmatch(k, p) for p in CONFIG_REWRITES) for k in config_keys(change.before, change.after)
            )
        case 'campaigns', column:
            return column in {'updated_at', 'updated_by'}
        case 'migrations', 'fingerprint':
            return True  # each chain records the fingerprint of its new head
        case 'events', 'payload':
            return typed(change)
        case table, _:
            return (table, change.rowid) in open_rows


def typed(change: Changed) -> bool:
    """An event payload that the migrations rewrote: the payload loads as its model, or a kind that nothing registers
    only gained its ``kind``. This checks that the rows of a real campaign load.
    """
    importlib.import_module('piloop.daemon.scheduler')  # registers the events of every layer
    importlib.import_module('piloop.plugins.campaign_type.optimization.planner')  # and of the optimization type
    before, after = str(change.before), str(change.after)
    event = EVENT.validate_json(after)
    return not isinstance(event, UnknownEvent) or json.loads(after) == {**json.loads(before), 'kind': event.kind}


def config_keys(before: object, after: object) -> set[str]:
    """The dotted keys whose values differ between two stored configs."""

    def flat(value: object, prefix: str = '') -> dict[str, object]:
        if isinstance(value, Mapping):
            return {k: v for key, item in value.items() for k, v in flat(item, f'{prefix}{key}.').items()}
        return {prefix.removesuffix('.'): value}

    old, new = (flat(json.loads(str(c))) for c in (before, after))
    return {k for k in old.keys() | new.keys() if old.get(k, ...) != new.get(k, ...)}
