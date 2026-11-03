"""One static HTML page per task, drawn with plotly in the browser: the any-time curves of each arm with their spread, and a table.

A curve is the sealed score of the head a run would hand in at each $ (or hour): a step function from the base at 0.
A head whose sealed score failed counts as 0 there. Each arm draws its median over repeats, and the band from the
lowest to the highest repeat.
"""

import html
import json
import statistics
from collections.abc import Callable, Iterable
from pathlib import Path

from evals.model import TASKS, Point, Result, Settings, Task

COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948']
"""Categorical slots 1-8, in the palette's fixed order: an arm keeps its slot by its order in the sorted arm names."""
STEPS = 200
Axis = Callable[[Point], float]
Summary = Callable[[list[float]], float]


def step(curve: list[Point], x: float, axis: Axis) -> float:
    """The sealed score of the head at ``x``: the last point at or before it."""
    value = 0.0
    for p in curve:
        if axis(p) > x:
            break
        value = p.sealed if p.sealed is not None else 0.0
    return value


def band(runs: list[Result], axis: Axis, end: float) -> tuple[list[float], list[float], list[float], list[float]]:
    """``x``, and the lowest, median and highest repeat at each ``x``, at ``STEPS`` points from 0 to ``end``."""
    xs = [end * i / STEPS for i in range(STEPS + 1)]
    ys = [[step(r.curve, x, axis) for r in runs] for x in xs]
    return xs, [min(y) for y in ys], [statistics.median(y) for y in ys], [max(y) for y in ys]


def fmt(values: Iterable[float | None], digits: int = 2, summary: Summary = statistics.median) -> str:
    """The summary (the median, by default) of the known values, with their range: ``m (lo–hi)``, or ``—``."""
    known = [v for v in values if v is not None]
    if not known:
        return '—'
    m, lo, hi = summary(known), min(known), max(known)
    return f'{m:.{digits}f}' if lo == hi else f'{m:.{digits}f} ({lo:.{digits}f}–{hi:.{digits}f})'


def auc(r: Result) -> float:
    """The mean sealed score over $0 to the budget: the area under the $ curve, per $."""
    return statistics.fmean(step(r.curve, r.budget_usd * i / STEPS, lambda p: p.usd) for i in range(STEPS + 1))


Row = tuple[str, Callable[[Result], float | None], int, Summary]
NOTE = 'Keep counts and keep rates are diagnostics: they depend on how hard the task is.'


def plain_bar(by_arm: dict[str, list[Result]], references: frozenset[str] = frozenset()) -> float | None:
    """The bar of the plain sessions: the best plain arm's median sealed score of its final head. None: no plain arm.

    A reference arm, such as one from an earlier round on another model, is drawn but never sets the bar.
    """
    medians = [
        statistics.median(r.final.sealed or 0.0 for r in rs)
        for arm, rs in by_arm.items()
        if rs[0].piloop is None and arm not in references
    ]
    return max(medians, default=None)


def reached(r: Result, bar: float) -> Point | None:
    """The first head of ``r`` whose sealed score is at ``bar`` or above."""
    return next((p for p in r.curve if p.sealed is not None and p.sealed >= bar), None)


def at_usd(usd: float) -> Callable[[Result], float | None]:
    """The sealed score of a run's head at ``usd`` spent: its final head when it stopped before."""
    return lambda r: step(r.curve, usd, lambda p: p.usd)


HOURS = (0.5, 1, 2)
"""Where a solvable task's report reads each run's sealed score, in hours."""


def solve_rows(solve: float) -> list[Row]:
    """The rows of a solvable task: whether a run solved it, the cost to its first solving head, the score each hour.

    The cost is in hours, $ (all, and the models' share) and tokens; the scores are at each of ``HOURS``.
    """

    def to(field: Callable[[Point], float | None]) -> Callable[[Result], float | None]:
        return lambda r: field(p) if (p := reached(r, solve)) else None

    return [
        ('solved', lambda r: float(reached(r, solve) is not None), 2, statistics.median),
        ('hours to solve', to(lambda p: p.hours), 2, statistics.median),
        ('$ to solve', to(lambda p: p.usd), 2, statistics.median),
        ('model $ to solve', to(lambda p: p.models_usd), 2, statistics.median),
        ('tokens to solve', to(lambda p: p.tokens.total if p.tokens else None), 0, statistics.median),
        *(
            (f'sealed score at {h:g} h', lambda r, h=h: step(r.curve, h, lambda p: p.hours), 3, statistics.median)
            for h in HOURS
        ),
    ]


def solve_of(task: str) -> float | None:
    """The ``solve`` score of ``task``, or None for a task that is not solvable or no longer in ``tasks/``."""
    return Task.load(task).solve if (TASKS / task / 'task.toml').exists() else None


def headline(bar: float | None, marks: Iterable[float] = ()) -> list[Row]:
    """The rows that compare the arms: the sealed score at each of the ``marks`` in $, at the end, and to the bar.

    The bar rows give the $ and hours to the plain sessions' bar.
    """
    rows: list[Row] = [
        *((f'sealed score at ${usd:g}', at_usd(usd), 3, statistics.median) for usd in marks),
        ('sealed score, final head', lambda r: r.final.sealed, 3, statistics.median),
        ('mean sealed score over $0–budget', auc, 3, statistics.median),
        ('dev − sealed gap, final head', lambda r: r.gap, 3, statistics.median),
        ('$ spent', lambda r: r.spend[-1].usd if r.spend else None, 2, statistics.median),
        ('model $', lambda r: r.spend[-1].models_usd if r.spend else None, 2, statistics.median),
        ('compute $', lambda r: r.spend[-1].compute_usd if r.spend else None, 2, statistics.median),
        ('hours run', lambda r: r.spend[-1].hours if r.spend else None, 2, statistics.median),
    ]
    if bar is None:
        return rows
    return [
        *rows,
        (
            f'share of repeats that reached the plain bar ({bar:.3f})',
            lambda r: float(reached(r, bar) is not None),
            2,
            statistics.median,
        ),
        ('$ to the plain bar', lambda r: p.usd if (p := reached(r, bar)) else None, 2, statistics.median),
        ('hours to the plain bar', lambda r: p.hours if (p := reached(r, bar)) else None, 2, statistics.median),
    ]


def spread(values: list[float]) -> float | None:
    """The standard deviation of ``values`` over their mean. None: fewer than two, or a mean of 0."""
    mean = statistics.fmean(values) if values else 0.0
    return statistics.stdev(values) / abs(mean) if len(values) > 1 and mean else None


def rate(r: Result) -> float | None:
    """Kept over submissions."""
    return r.funnel.kept / r.funnel.submissions if r.funnel.submissions else None


def rate_of(r: Result) -> float | None:
    """Dev evaluations plus submissions per hour."""
    return r.loop.dev_per_hour + r.loop.submissions_per_hour if r.loop else None


def gain_per_experiment(r: Result) -> float | None:
    """The final head's sealed score over a hundred experiments: how much each experiment moved the run."""
    if r.loop is None or r.final.sealed is None:
        return None
    hours = r.spend[-1].hours if r.spend else 0.0
    experiments = (rate_of(r) or 0.0) * hours
    return 100 * r.final.sealed / experiments if experiments else None


DIAGNOSTICS: list[Row] = [
    ('$ to first keep', lambda r: r.first_keep.usd if r.first_keep else None, 2, statistics.median),
    ('hours to first keep', lambda r: r.first_keep.hours if r.first_keep else None, 2, statistics.median),
    ('ideas', lambda r: r.funnel.ideas, 0, statistics.median),
    ('runs', lambda r: r.funnel.runs, 0, statistics.median),
    ('submissions', lambda r: r.funnel.submissions, 0, statistics.median),
    ('kept', lambda r: r.funnel.kept, 0, statistics.median),
    ('keep rate (kept / submissions)', rate, 2, statistics.median),
    (
        '$ per keep',
        lambda r: r.spend[-1].usd / r.funnel.kept if r.spend and r.funnel.kept else None,
        2,
        statistics.median,
    ),
    ('keeps per run', lambda r: r.funnel.kept / r.funnel.runs if r.funnel.runs else None, 2, statistics.median),
    ('escalations', lambda r: r.escalations, 0, statistics.median),
    ('human interventions', lambda r: r.interventions, 0, statistics.median),
    ('lead token share', lambda r: r.lead_token_share, 3, statistics.median),
    ('daemon restarts', lambda r: r.restarts, 0, statistics.median),
    (
        "self-test: the base's dev metric, mean",
        lambda r: statistics.fmean(r.selftest.values) if r.selftest else None,
        4,
        statistics.median,
    ),
    (
        'self-test: its spread (sd / mean)',
        lambda r: spread(r.selftest.values) if r.selftest else None,
        4,
        statistics.median,
    ),
    (
        'protocol calls (claim, submit, run end, notes)',
        lambda r: r.loop.protocol_calls if r.loop else None,
        0,
        statistics.median,
    ),
    (
        'protocol wall time, minutes',
        lambda r: r.loop.protocol_seconds / 60 if r.loop and r.loop.protocol_seconds is not None else None,
        1,
        statistics.median,
    ),
    ('protocol share of tokens', lambda r: r.loop.protocol_token_share if r.loop else None, 3, statistics.median),
    ('messages to workers: sent', lambda r: r.messages_sent, 0, statistics.median),
    ('messages to workers: delivered', lambda r: r.messages_delivered, 0, statistics.median),
    ('messages to workers: not delivered', lambda r: r.messages_refused, 0, statistics.median),
    ('experiments per hour (dev evals + submissions)', lambda r: rate_of(r), 1, statistics.median),
    ('sealed gain per 100 experiments', lambda r: gain_per_experiment(r), 3, statistics.median),
    ('dev evals per hour', lambda r: r.loop.dev_per_hour if r.loop else None, 1, statistics.median),
    ('dev eval p50, s', lambda r: r.loop.dev_p50_s if r.loop else None, 1, statistics.median),
    ('dev eval p90, s', lambda r: r.loop.dev_p90_s if r.loop else None, 1, statistics.median),
    ('dev eval drift (last third / first third)', lambda r: r.loop.drift if r.loop else None, 2, statistics.median),
    ('submissions per hour', lambda r: r.loop.submissions_per_hour if r.loop else None, 1, statistics.median),
    ('$ per experiment', lambda r: r.loop.usd_per_experiment if r.loop else None, 3, statistics.median),
    ('submitted to verdict p50, s', lambda r: r.loop.verdict_p50_s if r.loop else None, 0, statistics.median),
    (
        'submitted to visible measurement p50, s',
        lambda r: r.loop.visible_p50_s if r.loop else None,
        0,
        statistics.median,
    ),
    ('visible to sealed measurement p50, s', lambda r: r.loop.sealed_p50_s if r.loop else None, 0, statistics.median),
]
"""Each a metric of one run, shown as the median (range) over an arm's repeats."""


def total(counts: Callable[[Result], dict[str, int]]) -> Callable[[Result], float | None]:
    """The sum of a piloop run's ``counts``. None: a plain run."""
    return lambda r: None if r.piloop is None else sum(counts(r).values())


def count_of(key: str, counts: Callable[[Result], dict[str, int]]) -> Callable[[Result], float | None]:
    """A piloop run's ``counts`` at ``key``, 0 when missing. None: a plain run."""
    return lambda r: None if r.piloop is None else counts(r).get(key, 0)


def share_in_final(r: Result) -> float | None:
    """The share of a piloop run's keeps that its final head contains. None: a plain run, or no keeps."""
    kept = sum(r.keeps.values())
    return None if r.piloop is None or not kept else sum(r.keeps_in_final.values()) / kept


def race_rate(r: Result) -> float | None:
    """The share of a piloop run's submissions lost to a race."""
    return None if r.races is None or not r.funnel.submissions else r.races / r.funnel.submissions


def team(by_arm: dict[str, list[Result]]) -> list[Row]:
    """How the workers built on each other: in total, then for each role, and each pair of roles, that shows up."""
    rows: list[Row] = [
        ('keeps in the final head', total(lambda r: r.keeps_in_final), 0, statistics.median),
        ('share of keeps in the final head', share_in_final, 2, statistics.median),
        ('race rate (beat its start, not the moved head)', race_rate, 2, statistics.median),
        ('notes written', total(lambda r: r.notes_written), 0, statistics.median),
        ('notes read by a worker of another run', total(lambda r: r.notes_read), 0, statistics.median),
    ]
    runs = [r for rs in by_arm.values() for r in rs]
    roles = sorted({k for r in runs for k in r.keeps})
    if len(roles) > 1:
        for role in roles:
            rows.append((f'keeps by {role}', count_of(role, lambda r: r.keeps), 0, statistics.median))
            rows.append(
                (f'keeps by {role} in the final head', count_of(role, lambda r: r.keeps_in_final), 0, statistics.median)
            )
    for key in sorted({k for r in runs for k in r.notes_read}):
        a, _, b = key.partition('>')
        if a != b:
            rows.append((f'notes by {a} read by {b}', count_of(key, lambda r: r.notes_read), 0, statistics.median))
    return rows


Chart = tuple[str, list[dict[str, object]], dict[str, object]]
"""A chart: its title, its traces and its layout, as ``Plotly.newPlot`` takes them. A trace's ``meta`` is its arm's
series number, which the page turns into the arm's colour."""

PAINT = """
const css = getComputedStyle(document.documentElement);
const FALLBACK = %s;
function paint(data) {
  for (const t of data) {
    const c = css.getPropertyValue('--series-' + (t.meta + 1)).trim() || FALLBACK[t.meta %% FALLBACK.length];
    t.line = Object.assign({}, t.line, {color: c});
    if (t.fill) t.fillcolor = c;
    if (t.mode === 'markers') t.marker = Object.assign({}, t.marker, {color: c});
  }
  return data;
}
for (const [id, data, layout] of CHARTS) %s(id, paint(data), layout);
"""
"""Draws the charts with the page's plot function, each arm in its series colour: the stylesheet's ``--series-<n>``
when it defines them, else ``COLORS``."""


def charts(by_arm: dict[str, list[Result]], bar: float | None, references: frozenset[str] = frozenset()) -> list[Chart]:
    """The $ and hours curves with their bands, the heads of each repeat, and the outcome against the loop's rate.

    A reference arm's curves are dotted.
    """
    runs = [r for rs in by_arm.values() for r in rs]
    out: list[Chart] = []
    for title, axis, end, label in (
        ('Sealed score against USD spent', lambda p: p.usd, max(r.budget_usd for r in runs), 'USD, list price'),
        ('Sealed score against hours', lambda p: p.hours, max(r.max_hours for r in runs), 'hours'),
    ):
        data: list[dict[str, object]] = []
        for k, (arm, rs) in enumerate(by_arm.items()):
            xs, low, median, high = band(rs, axis, end)
            data.append({'x': xs + xs[::-1], 'y': high + low[::-1], 'fill': 'toself', 'opacity': 0.18, 'meta': k,
                         'line': {'width': 0}, 'hoverinfo': 'skip', 'showlegend': False, 'legendgroup': arm})  # fmt: skip
            data.append({'x': xs, 'y': median, 'name': arm, 'meta': k, 'legendgroup': arm,
                         'line': {'shape': 'hv', 'width': 2, 'dash': 'dot' if arm in references else 'solid'}})  # fmt: skip
        shapes = [] if bar is None else [{'type': 'line', 'xref': 'paper', 'x0': 0, 'x1': 1, 'y0': bar, 'y1': bar,
                                          'line': {'dash': 'dash', 'width': 1}}]  # fmt: skip
        layout: dict[str, object] = {
            'xaxis': {'title': {'text': label}},
            'yaxis': {'title': {'text': 'sealed score'}},
            'shapes': shapes,
        }
        out.append((title, data, layout))
    heads: list[dict[str, object]] = [
        {'x': list(range(len(r.curve))), 'y': [p.sealed if p.sealed is not None else 0.0 for p in r.curve],
         'name': arm, 'meta': k, 'legendgroup': arm, 'showlegend': i == 0, 'line': {'shape': 'hv', 'width': 1}}
        for k, (arm, rs) in enumerate(by_arm.items()) for i, r in enumerate(rs)
    ]  # fmt: skip
    head_axis: dict[str, object] = {'xaxis': {'title': {'text': 'head (0 = start)'}}}
    out.append(("Sealed score over each repeat's heads", heads, head_axis))
    points: list[dict[str, object]] = []
    for k, (arm, rs) in enumerate(by_arm.items()):
        pairs = [(x, r.final.sealed or 0.0) for r in rs if (x := rate_of(r)) is not None]
        if pairs:
            points.append({'x': [x for x, _ in pairs], 'y': [y for _, y in pairs], 'mode': 'markers', 'name': arm,
                           'meta': k, 'marker': {'size': 10}})  # fmt: skip
    rate_axis: dict[str, object] = {'xaxis': {'title': {'text': 'dev evaluations plus submissions per hour'}}}
    out.append(('Final sealed score against experiments per hour', points, rate_axis))
    return out


def table(rows: list[Row], by_arm: dict[str, list[Result]]) -> str:
    """The rows over the arms, as an HTML table."""
    head = ''.join(f'<th>{html.escape(arm)} (n={len(rs)})</th>' for arm, rs in by_arm.items())
    body = ''.join(
        f'<tr><td>{html.escape(name)}</td>'
        + ''.join(f'<td class="num">{fmt(map(get, rs), digits, summary)}</td>' for rs in by_arm.values())
        + '</tr>'
        for name, get, digits, summary in rows
    )
    return f'<table><thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table>'


def stats(by_arm: dict[str, list[Result]], bar: float | None) -> str:
    """One stat per arm: the median sealed score of its final heads, and its $ to the plain bar."""
    cards = []
    for arm, rs in by_arm.items():
        value = fmt((r.final.sealed for r in rs), 3).split(' ')[0]
        hits = [h for r in rs if bar is not None and (h := reached(r, bar)) is not None]
        delta = f'${statistics.median(h.usd for h in hits):.2f} to the plain bar' if hits else 'never at the plain bar'
        cards.append(
            f'<div class="stat"><div class="label">{html.escape(arm)}</div><div class="value">{value}</div>'
            f'<div class="delta">{len(hits)}/{len(rs)} reached; {delta}</div></div>'
        )
    return f'<div class="grid">{"".join(cards)}</div>'


def page(
    task: str,
    by_arm: dict[str, list[Result]],
    notes: list[str],
    settings: Settings,
    references: frozenset[str] = frozenset(),
    marks: Iterable[float] = (),
) -> str:
    """The page of one task, with its headline at each of the ``marks`` in $.

    The ``references`` arms are drawn dotted and never set the plain bar.
    """
    bar = plain_bar(by_arm, references)
    solve = solve_of(task)
    rows = [*solve_rows(solve), *headline(bar, marks)] if solve is not None else headline(bar, marks)
    drawn = charts(by_arm, bar, references)
    divs = ''.join(
        f'<div class="card"><h3>{html.escape(t)}</h3><div id="c{i}"></div></div>' for i, (t, _, _) in enumerate(drawn)
    )
    data = json.dumps([[f'c{i}', d, layout] for i, (_, d, layout) in enumerate(drawn)])
    scripts = ''.join(f'<script src="{html.escape(src)}"></script>' for src in settings.report_plot_js)
    css = f'<link rel="stylesheet" href="{html.escape(settings.report_css)}">' if settings.report_css else ''
    callouts = ''.join(f'<div class="callout">{html.escape(n)}</div>' for n in [NOTE, *notes])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(task)}</title>{css}{scripts}</head>
<body class="wide"><h1>{html.escape(task)}</h1>
<p class="muted">The sealed score of the version each run would hand in: 0 is the starting repo, 1 the reference.
Lines are the median over repeats, bands the lowest to highest repeat; spend is list price, models plus compute.</p>
{callouts}<h2>Headline</h2>{stats(by_arm, bar)}{table(rows, by_arm)}{divs}
<h2>Diagnostics</h2>{table([*DIAGNOSTICS, *team(by_arm)], by_arm)}
<script>const CHARTS = {data};{PAINT % (json.dumps(COLORS), settings.report_plot_fn)}</script></body></html>
"""


def write(
    results: list[Result],
    out: Path,
    settings: Settings,
    notes: dict[str, list[str]] | None = None,
    references: frozenset[str] = frozenset(),
    marks: Iterable[float] = (),
) -> list[Path]:
    """Write ``<out>/<task>.html`` for each task, with the ``notes`` of each task, and an ``index.html``. The pages.

    The ``references`` arms are drawn dotted and never set the plain bar.
    """
    out.mkdir(parents=True, exist_ok=True)
    pages = []
    for task in sorted({r.task for r in results}):
        arms = sorted({r.arm for r in results if r.task == task})
        if len(arms) > len(COLORS):
            raise ValueError(f'{task} has {len(arms)} arms: a page draws at most {len(COLORS)}; split them')
        by_arm = {arm: [r for r in results if r.task == task and r.arm == arm] for arm in arms}
        path = out / f'{task}.html'
        path.write_text(page(task, by_arm, (notes or {}).get(task, []), settings, references, tuple(marks)))
        pages.append(path)
    css = f'<link rel="stylesheet" href="{html.escape(settings.report_css)}">' if settings.report_css else ''
    links = ''.join(f'<li><a href="{p.name}">{html.escape(p.stem)}</a></li>' for p in pages)
    (out / 'index.html').write_text(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>evals</title>{css}</head>'
        f'<body><h1>Eval results</h1><ul>{links}</ul></body></html>\n'
    )
    return pages
