"""One static HTML page per task, drawn by plotly: the any-time curves of each arm with their spread, and a table.

A curve is the sealed score of the head a run would hand in at each $ (or hour): a step function from the base at 0.
A head whose sealed score failed counts as 0 there. Each arm draws its median over repeats, and the band from the
lowest to the highest repeat.
"""

import statistics
from collections.abc import Callable, Iterable
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from evals.model import Point, Result

COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100']
"""Categorical slots 1-4: an arm keeps its slot by its order in the sorted arm names."""
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
NOTE = (
    'Both tasks start from easy baselines (zlib; a square grid of equal circles), so most ideas keep:'
    ' keep counts and rates are diagnostics, not results.'
)


def plain_bar(by_arm: dict[str, list[Result]]) -> float | None:
    """The bar of the plain sessions: the best plain arm's median sealed score of its final head. None: no plain arm."""
    medians = [statistics.median(r.final.sealed or 0.0 for r in rs) for rs in by_arm.values() if rs[0].piloop is None]
    return max(medians, default=None)


def reached(r: Result, bar: float) -> Point | None:
    """The first head of ``r`` whose sealed score is at ``bar`` or above."""
    return next((p for p in r.curve if p.sealed is not None and p.sealed >= bar), None)


def headline(bar: float | None) -> list[Row]:
    """The rows that compare the arms: the holdout score, and the $ and hours to the plain sessions' bar."""
    rows: list[Row] = [
        ('sealed score, final head', lambda r: r.final.sealed, 3, statistics.median),
        ('mean sealed score over $0–budget', auc, 3, statistics.median),
        ('dev − sealed gap, final head', lambda r: r.gap, 3, statistics.median),
        ('$ spent', lambda r: r.spend[-1].usd if r.spend else None, 2, statistics.median),
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


def rate(r: Result) -> float | None:
    """Kept over submissions."""
    return r.funnel.kept / r.funnel.submissions if r.funnel.submissions else None


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
]
"""Each a metric of one run, shown as the median (range) over an arm's repeats."""


def table(rows: list[Row], by_arm: dict[str, list[Result]]) -> go.Table:
    """The rows over the arms."""
    cells = [[name for name, *_ in rows]]
    cells += [[fmt(map(get, rs), digits, summary) for _, get, digits, summary in rows] for rs in by_arm.values()]
    header = ['', *(f'{arm} (n={len(rs)})' for arm, rs in by_arm.items())]
    return go.Table(header={'values': header}, cells={'values': cells})


def figure(task: str, by_arm: dict[str, list[Result]], notes: list[str]) -> go.Figure:
    """The page of one task: the $ and hours curves against the plain bar, then the headline and the diagnostics."""
    runs = [r for rs in by_arm.values() for r in rs]
    ends: list[tuple[int, Axis, float]] = [
        (1, lambda p: p.usd, max(r.budget_usd for r in runs)),
        (2, lambda p: p.hours, max(r.max_hours for r in runs)),
    ]
    bar = plain_bar(by_arm)
    full = {'type': 'table', 'colspan': 2}
    fig = make_subplots(
        rows=3,
        cols=2,
        specs=[[{'type': 'xy'}, {'type': 'xy'}], [full, None], [full, None]],
        row_heights=[0.44, 0.2, 0.36],
        vertical_spacing=0.07,
        subplot_titles=['sealed score against USD spent', 'sealed score against hours', 'headline', 'diagnostics'],
    )
    for color, (arm, rs) in zip(COLORS, by_arm.items(), strict=False):
        for col, axis, end in ends:
            xs, low, median, high = band(rs, axis, end)
            hull = dict(x=xs + xs[::-1], y=high + low[::-1], fill='toself', line_width=0, opacity=0.18)
            fig.add_scatter(**hull, fillcolor=color, hoverinfo='skip', showlegend=False, row=1, col=col)
            fig.add_scatter(x=xs, y=median, name=arm, legendgroup=arm, showlegend=col == 1, line_color=color,
                            line_shape='hv', line_width=2, row=1, col=col)  # fmt: skip
    if bar is not None:
        for col in (1, 2):
            fig.add_hline(y=bar, line_dash='dash', line_width=1, annotation_text='plain bar', row=1, col=col)
    fig.add_trace(table(headline(bar), by_arm), row=2, col=1)
    fig.add_trace(table(DIAGNOSTICS, by_arm), row=3, col=1)
    fig.update_yaxes(title_text='sealed score (0 = start, 1 = reference)', row=1, col=1)
    fig.update_xaxes(title_text='USD, list price', row=1, col=1)
    fig.update_xaxes(title_text='hours', row=1, col=2)
    lines = ''.join(f'<br><sup>{line}</sup>' for line in [NOTE, *notes])
    title = f'{task}: median over repeats, band from lowest to highest{lines}'
    fig.update_layout(title=title, height=1500, hovermode='x')
    return fig


def write(results: list[Result], out: Path, notes: dict[str, list[str]] | None = None) -> list[Path]:
    """Write ``<out>/<task>.html`` for each task, with the ``notes`` of each task under its title. The pages."""
    out.mkdir(parents=True, exist_ok=True)
    pages = []
    for task in sorted({r.task for r in results}):
        arms = sorted({r.arm for r in results if r.task == task})
        if len(arms) > len(COLORS):
            raise ValueError(f'{task} has {len(arms)} arms: a page draws at most {len(COLORS)}; split them')
        by_arm = {arm: [r for r in results if r.task == task and r.arm == arm] for arm in arms}
        path = out / f'{task}.html'
        figure(task, by_arm, (notes or {}).get(task, [])).write_html(path, include_plotlyjs='cdn')
        pages.append(path)
    return pages
