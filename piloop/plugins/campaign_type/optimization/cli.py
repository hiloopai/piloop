"""Optimization's commands, which span its components: ``piloop candidate`` and ``piloop history``."""

from typing import Annotated

import cyclopts
from cyclopts import validators

from piloop.cli import common
from piloop.cli.client import campaign_name, daemon_http
from piloop.cli.common import CampaignRef
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Outcome
from piloop.plugins.campaign_type.kit.candidates.wire import CandidatesClient, ImportBody
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.evaluation.wire import REPEATS, EvaluationClient, RungResult
from piloop.plugins.campaign_type.kit.verdicts.wire import RemeasureBody, VerdictBody, VerdictsClient
from piloop.plugins.campaign_type.optimization.wire import NoteBody, OptimizationClient
from piloop.text import NONE, metric

candidate_app = cyclopts.App(
    name='candidate',
    help='Show a candidate, import a starting point, measure a candidate again, keep a regressed result in view, or'
    ' override a verdict.',
)


@candidate_app.command(name='show')
def candidate_show(candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate.')], /) -> None:
    """Print a candidate: its verdict, its metrics beside its parent's, and each rung of the ladder that it measured.

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
        f'candidate#{c.id} {c.label}: {c.verdict_reason or NONE}',
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
    """Register the files of a version as a reference candidate: a starting point for an idea.

    The daemon measures it with the current evaluator. A reference is never kept, and it is never the head or a bar.
    It does not count in the kept total, the sparkline or a stop condition. To import a candidate of another campaign,
    bring its `revision` into this campaign's repo first.
    """
    with CandidatesClient.connect(daemon_http()) as client:
        name = campaign_name(client, ref)
        candidate = client.import_candidate(name, ImportBody(version=version, summary=summary)).candidate
    common.console.print(
        f'candidate#{candidate.id} reference: {candidate.summary}; the daemon measures it; start an idea from it'
        f' with `piloop idea add {name} "<title>" --base {candidate.id}`',
        markup=False,
    )


@candidate_app.command(name='remeasure')
def candidate_remeasure(
    candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate to measure again.')],
    /,
    *,
    repeats: Annotated[
        int, cyclopts.Parameter(help='How many pairs of the candidate and its bar.', validator=validators.Number(gte=1))
    ] = REPEATS,
) -> None:
    """Ask the daemon to measure a candidate again, next to its bar, with the current evaluator.

    Use it when a fault of the host spoiled a measurement (a full disk, a lost GPU), or when load made it noisy. The
    daemon runs the evaluate command on the candidate and on its bar, one after the other, `--repeats` times, and judges
    a `crash`, `discard` or `interesting` that it gave again on these pairs. A keep, and a verdict that someone set,
    stay. It measures soon, after the head and the bars that runs need, also when the campaign is stopped: not while it
    is paused with `--evals`, or held by its stop rule. A commit that the daemon never measured, `skipped` or
    `recovered`, is measured and judged. A pending candidate is refused: its run measures and judges it.
    """
    with VerdictsClient.connect(daemon_http()) as client:
        done = client.remeasure(candidate_id, RemeasureBody(repeats=repeats))
    later = '' if done.held is None else f' {done.held}'
    bar = 'alone' if done.bar is None else f'next to its bar candidate#{done.bar}'
    effect = 'then judges it again' if done.rejudged else 'its verdict stays'
    common.console.print(
        f'candidate#{candidate_id} {done.candidate.label}: the daemon measures it {repeats} times {bar}{later}; {effect}',
        markup=False,
    )


@candidate_app.command(name='note')
def candidate_note(
    candidate_id: Annotated[
        CandidateId, cyclopts.Parameter(help='The candidate: a measured discard, or one marked interesting already.')
    ],
    reason: Annotated[str, cyclopts.Parameter(help='Why the tree is worth starting from although it regressed.')],
    /,
    *,
    follow_ups: Annotated[
        tuple[str, ...],
        cyclopts.Parameter(
            name='--follow-up', help='A seed pinned to the candidate, for research to develop. Repeatable.'
        ),
    ] = (),
    track: Annotated[
        TrackName | None,
        cyclopts.Parameter(
            help="The track of the follow-ups. Default: the track of the candidate's run, else the first track."
        ),
    ] = None,
) -> None:
    """Keep a regressed result in view as a lead, with follow-up seeds pinned to it.

    The verdict becomes `interesting`, with the cause `note` and your reason before the daemon's. It is yours only when
    the note changes it, from a discard: the `candidate.noted` event signs the note. The candidate is not
    kept and sets no bar, but it stays listed and ideas can start from it. Each `--follow-up` is a seed whose `base_candidate_id` is the
    candidate. An implementer files no follow-ups. When its candidate does not exist yet, an implementer notes the
    version that it saves instead, and the daemon applies the note when it judges the version.
    """
    with OptimizationClient.connect(daemon_http()) as client:
        noted = client.note(candidate_id, NoteBody(reason=reason, follow_ups=follow_ups, track=track))
    candidate = noted.candidate
    common.console.print(
        f'candidate#{candidate.id} interesting ({candidate.cause}): {candidate.verdict_reason}', markup=False
    )
    for idea in noted.seeds:
        common.console.print(f'idea#{idea.id} seed, pinned to candidate#{candidate.id}: {idea.title}', markup=False)


@candidate_app.command(name='verdict')
def candidate_verdict(
    candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate.')],
    verdict: Annotated[Outcome, cyclopts.Parameter(help='keep, interesting, discard or crash.')],
    /,
    *,
    reason: Annotated[str, cyclopts.Parameter(help='Why, with ids: what the number does not say.')],
) -> None:
    """Override a candidate's judgement, with the reason.

    The verdict is then yours, signed as PILOOP_ACTOR, so the daemon never changes it. The `candidate.verdict` event
    records the old and new verdict with the reason. Only a judgement is overridden: pending, reference, recovered and
    skipped candidates are refused. A keep of a trunk run that does not build on the head would drop what the head
    kept. So the candidate becomes `interesting`, and its merge onto the head is a new candidate that the daemon
    measures and judges against the head, as for its own keeps. A merge that conflicts files a seed to compose it.
    """
    with VerdictsClient.connect(daemon_http()) as client:
        done = client.set_verdict(candidate_id, VerdictBody(verdict=verdict, reason=reason))
    common.console.print(
        f'candidate#{candidate_id} {done.was} → {done.candidate.label}: {done.candidate.verdict_reason}', markup=False
    )
    if done.merge is not None:
        common.console.print(
            f'candidate#{done.merge.candidate}: the daemon measures it and judges it against the head'
            f' candidate#{done.merge.head}',
            markup=False,
        )


def history(ref: CampaignRef | None = None, /) -> None:
    """Print the research pack: what a research sweep reads before it proposes ideas.

    It shows the metrics, the analysis artifacts, what was tried per mechanism family, the latest ideas with their
    outcomes, the kept lineage, the findings and the digests, as many as `[plugins.section.pack]` says. Without a
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
