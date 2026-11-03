"""Optimization's commands, which span its components: ``piloop candidate`` and ``piloop history``."""

from typing import Annotated

import cyclopts
from cyclopts import validators

from piloop.cli import common
from piloop.cli.client import campaign_name, daemon_http
from piloop.cli.common import CampaignRef
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.wire import CandidatesClient, ImportBody
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.evaluation.wire import REPEATS, EvaluationClient, RungResult
from piloop.plugins.campaign_type.kit.pool.wire import ExcludeBody, PoolClient, RemeasureBody
from piloop.plugins.campaign_type.optimization.wire import NoteBody, OptimizationClient
from piloop.text import NONE, metric

candidate_app = cyclopts.App(
    name='candidate',
    help='Show a candidate, import a starting point, measure a candidate again, note one, or exclude one from the best.',
)


@candidate_app.command(name='show')
def candidate_show(candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate.')], /) -> None:
    """Print a candidate: where it stands, its metrics beside its parent's, and each rung of the ladder that it measured.

    The metrics are the means under the campaign's current evaluator. A rung's time is its own, without the rungs
    before it.
    """
    with EvaluationClient.connect(daemon_http()) as client:
        shown = client.candidate(candidate_id)
    c, before = shown.candidate, shown.parent_metrics
    full = ', '.join(
        f'{name} {metric(value)}' + (f' ({metric(value - before[name], signed=True)})' if name in before else '')
        for name, value in sorted(shown.metrics.items())
    )
    lines = [
        f'candidate#{c.id} {c.origin}: {shown.standing or NONE}',
        *([f'excluded: {c.excluded_reason}'] if c.excluded_reason else []),
        c.summary or NONE,
        f'parent {f"candidate#{shown.parent}" if shown.parent else NONE} · run {f"#{shown.run}" if shown.run else NONE}'
        f' · revision {shown.revision[:12]} · evaluator{f"#{shown.evaluator}" if shown.evaluator else f" {NONE}"}',
        f'full panel (vs parent): {full or NONE}',
        *(_rung_line(r) for r in shown.rungs),
    ]
    for line in lines:
        common.console.print(line, markup=False, highlight=False)


def _rung_line(r: RungResult) -> str:
    """``rung screen: score 2, size 4 · 2.25s · 2 measurements · stopped: <why>``."""
    values = ', '.join(f'{name} {metric(value)}' for name, value in sorted(r.metrics.items()))
    parts = [
        f'rung {r.rung}: {values or NONE}',
        *([f'{metric(r.seconds)}s'] if r.seconds is not None else []),
        *([f'{r.measurements} measurements'] if r.measurements > 1 else []),
        *([f'stopped: {r.stopped}'] if r.stopped else []),
    ]
    return ' · '.join(parts)


@candidate_app.command(name='import')
def candidate_import(
    ref: CampaignRef,
    version: Annotated[
        str,
        cyclopts.Parameter(help="A version in the campaign's repo, as its vcs names one: a revision, a branch, a tag."),
    ],
    /,
    *,
    summary: Annotated[
        str | None, cyclopts.Parameter(help="A summary of the candidate. Default: the version's own summary.")
    ] = None,
) -> None:
    """Register the files of a version as a reference candidate: a starting point for a run.

    The daemon scores it into the pool with the current evaluator, as any candidate: it may become the best. To import
    a candidate of another campaign, bring its `revision` into this campaign's repo first.
    """
    with CandidatesClient.connect(daemon_http()) as client:
        name = campaign_name(client, ref)
        candidate = client.import_candidate(name, ImportBody(version=version, summary=summary)).candidate
    common.console.print(
        f'candidate#{candidate.id} reference: {candidate.summary}; the daemon measures it; start a run from it'
        f' with `piloop idea claim <id> --base {candidate.id}`',
        markup=False,
    )


@candidate_app.command(name='remeasure')
def candidate_remeasure(
    candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate to measure again.')],
    /,
    *,
    repeats: Annotated[
        int, cyclopts.Parameter(help='How many more evaluations.', validator=validators.Number(gte=1))
    ] = REPEATS,
) -> None:
    """Ask the daemon to evaluate a candidate `--repeats` more times, with the current evaluator.

    Use it when a fault of the host spoiled a measurement (a full disk, a lost GPU), or when load made it noisy. The
    candidate's mean includes the new evaluations, so the pool and its best follow. It measures soon, after the
    baseline, also when the campaign is stopped: not while it is paused with `--evals`, or held by its stop rule. A
    commit that the daemon never measured, `skipped` or `recovered`, gets its first evaluations.
    """
    with PoolClient.connect(daemon_http()) as client:
        done = client.remeasure(candidate_id, RemeasureBody(repeats=repeats))
    later = '' if done.held is None else f' {done.held}'
    common.console.print(f'candidate#{candidate_id}: the daemon evaluates it {repeats} more times{later}', markup=False)


@candidate_app.command(name='note')
def candidate_note(
    candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate.')],
    reason: Annotated[str, cyclopts.Parameter(help='Why the tree is worth starting from.')],
    /,
    *,
    follow_ups: Annotated[
        tuple[str, ...],
        cyclopts.Parameter(name='--follow-up', help='An open idea about the candidate. Repeatable.'),
    ] = (),
    track: Annotated[
        TrackName | None,
        cyclopts.Parameter(
            help="The track of the follow-ups. Default: the track of the candidate's run, else the first track."
        ),
    ] = None,
) -> None:
    """Note why a candidate is worth starting from, with follow-up ideas about it.

    The note is a comment on the candidate, signed as PILOOP_ACTOR. Each `--follow-up` is an open idea about the
    candidate, whose run can start from it (`piloop idea claim <id> --base <candidate>`). An implementer files no
    follow-ups.
    """
    with OptimizationClient.connect(daemon_http()) as client:
        noted = client.note(candidate_id, NoteBody(reason=reason, follow_ups=follow_ups, track=track))
    candidate = noted.candidate
    common.console.print(f'candidate#{candidate.id}: noted', markup=False)
    for idea in noted.ideas:
        common.console.print(f'idea#{idea.id} open, about candidate#{candidate.id}: {idea.title}', markup=False)


@candidate_app.command(name='exclude')
def candidate_exclude(
    candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate.')],
    /,
    *,
    reason: Annotated[str, cyclopts.Parameter(help='Why it may not be best, with ids: what the number does not say.')],
) -> None:
    """Take a candidate out of the race for the best, such as one that gamed the evaluation.

    Its scores stay in the pool, and a run may still start from it. The `candidate.excluded_reason` event records the
    reason, signed as PILOOP_ACTOR. To take it back, set `excluded_reason` and `excluded_by` to NULL with `piloop sql`.
    """
    with PoolClient.connect(daemon_http()) as client:
        done = client.exclude(candidate_id, ExcludeBody(reason=reason))
    common.console.print(f'candidate#{candidate_id} excluded: {done.candidate.excluded_reason}', markup=False)


def history(ref: CampaignRef | None = None, /) -> None:
    """Print the research pack: the campaign's metrics, analysis, ideas, new bests and notes.

    It shows the metrics, the analysis artifacts, the latest ideas with their outcomes, the new bests, the latest
    notes and the digests, as many as `[plugins.section.pack]` says. Without a
    campaign, it uses the campaign that the current directory is in, else the only active campaign, else every campaign.
    """
    # here: it opens the database, and the candidate commands of this module load none of it
    from rich import box
    from rich.console import Group

    from piloop.campaign.campaigns import workspace_dir
    from piloop.cli import local
    from piloop.plugins.campaign_type.optimization.pack import renderables as pack_renderables
    from piloop.plugins.campaign_type.optimization.section.pack import Pack
    from piloop.plugins.section.contract import SECTIONS

    with local.session() as s:
        for campaign in local.campaigns(s, ref):
            common.heading(f'[bold]{campaign.name}[/] #{campaign.id}  {campaign.path}')
            workspace = workspace_dir(common.settings.home, campaign.id)
            cfg = campaign.config
            shown = next((x for x in SECTIONS.instances(cfg.plugins).values() if isinstance(x, Pack)), Pack())
            parts = pack_renderables(s, campaign, cfg, table_box=box.SIMPLE_HEAD, workspace=workspace, shown=shown)
            common.console.print(Group(*parts))
