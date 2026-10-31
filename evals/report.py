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


def fmt(values: Iterable[float | None], digits: int = 2) -> str:
    """The median of the known values, with their range: ``m (lo–hi)``, or ``—``."""
    known = [v for v in values if v is not None]
    if not known:
        return '—'
    m, lo, hi = statistics.median(known), min(known), max(known)
    return f'{m:.{digits}f}' if lo == hi else f'{m:.{digits}f} ({lo:.{digits}f}–{hi:.{digits}f})'


def auc(r: Result) -> float:
    """The mean sealed score over $0 to the budget: the area under the $ curve, per $."""
    return statistics.fmean(step(r.curve, r.budget_usd * i / STEPS, lambda p: p.usd) for i in range(STEPS + 1))


ROWS: list[tuple[str, Callable[[Result], float | None], int]] = [
    ('sealed score, final head', lambda r: r.final.sealed, 3),
    ('dev score, final head', lambda r: r.final.dev, 3),
    ('dev − sealed gap', lambda r: r.gap, 3),
    ('mean sealed score over $0–budget', auc, 3),
    ('$ to first keep', lambda r: r.first_keep.usd if r.first_keep else None, 2),
    ('hours to first keep', lambda r: r.first_keep.hours if r.first_keep else None, 2),
    ('$ spent', lambda r: r.spend[-1].usd if r.spend else None, 2),
    ('$ per keep', lambda r: r.spend[-1].usd / r.funnel.kept if r.spend and r.funnel.kept else None, 2),
    ('keeps per run', lambda r: r.funnel.kept / r.funnel.runs if r.funnel.runs else None, 2),
    ('ideas', lambda r: r.funnel.ideas, 0),
    ('runs', lambda r: r.funnel.runs, 0),
    ('submissions', lambda r: r.funnel.submissions, 0),
    ('kept', lambda r: r.funnel.kept, 0),
    ('escalations', lambda r: r.escalations, 0),
    ('human interventions', lambda r: r.interventions, 0),
    ('lead token share', lambda r: r.lead_token_share, 3),
]
"""The rows of the table: each a metric of one run, shown as the median (range) over an arm's repeats."""


def figure(task: str, by_arm: dict[str, list[Result]]) -> go.Figure:
    """The page of one task: the $ and hours curves above, the table below."""
    runs = [r for rs in by_arm.values() for r in rs]
    ends: list[tuple[int, Axis, float]] = [
        (1, lambda p: p.usd, max(r.budget_usd for r in runs)),
        (2, lambda p: p.hours, max(r.max_hours for r in runs)),
    ]
    fig = make_subplots(
        rows=2,
        cols=2,
        specs=[[{'type': 'xy'}, {'type': 'xy'}], [{'type': 'table', 'colspan': 2}, None]],
        row_heights=[0.5, 0.5],
        vertical_spacing=0.12,
        subplot_titles=['sealed score against USD spent', 'sealed score against hours'],
    )
    for color, (arm, rs) in zip(COLORS, by_arm.items(), strict=False):
        for col, axis, end in ends:
            xs, low, median, high = band(rs, axis, end)
            hull = dict(x=xs + xs[::-1], y=high + low[::-1], fill='toself', line_width=0, opacity=0.18)
            fig.add_scatter(**hull, fillcolor=color, hoverinfo='skip', showlegend=False, row=1, col=col)
            fig.add_scatter(x=xs, y=median, name=arm, legendgroup=arm, showlegend=col == 1, line_color=color,
                            line_shape='hv', line_width=2, row=1, col=col)  # fmt: skip
    cells = [[name for name, _, _ in ROWS]]
    cells += [[fmt(map(get, rs), digits) for _, get, digits in ROWS] for rs in by_arm.values()]
    header = ['', *(f'{arm} (n={len(rs)})' for arm, rs in by_arm.items())]
    fig.add_trace(go.Table(header={'values': header}, cells={'values': cells}), row=2, col=1)
    fig.update_yaxes(title_text='sealed score (0 = start, 1 = reference)', row=1, col=1)
    fig.update_xaxes(title_text='USD, list price', row=1, col=1)
    fig.update_xaxes(title_text='hours', row=1, col=2)
    fig.update_layout(title=f'{task}: median over repeats, band from lowest to highest', height=1100, hovermode='x')
    return fig


def write(results: list[Result], out: Path) -> list[Path]:
    """Write ``<out>/<task>.html`` for each task. The pages."""
    out.mkdir(parents=True, exist_ok=True)
    pages = []
    for task in sorted({r.task for r in results}):
        arms = sorted({r.arm for r in results if r.task == task})
        if len(arms) > len(COLORS):
            raise ValueError(f'{task} has {len(arms)} arms: a page draws at most {len(COLORS)}; split them')
        by_arm = {arm: [r for r in results if r.task == task and r.arm == arm] for arm in arms}
        path = out / f'{task}.html'
        figure(task, by_arm).write_html(path, include_plotlyjs='cdn')
        pages.append(path)
    return pages
