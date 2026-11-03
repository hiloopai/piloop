"""A campaign of optimization in one line: its best, new bests and candidates, for ``piloop wake`` and ``piloop watch``."""

from sqlalchemy.orm import Session

from piloop.config import CampaignConfig
from piloop.db.models import Campaign, count
from piloop.plugins.campaign_type.kit.candidates.references import references
from piloop.plugins.campaign_type.kit.candidates.tables import produced
from piloop.plugins.campaign_type.kit.pool.pool import best_scores
from piloop.plugins.campaign_type.kit.pool.scored import new_bests
from piloop.reports.overview import _four
from piloop.text import metric
from piloop.usage import human
from piloop.usage.totals import campaign_tokens


def summary(s: Session, campaign: Campaign, cfg: CampaignConfig) -> str:
    """Return the one-line summary of the campaign.

    The format is ``<name> · best <metric> <score> (candidate#<id>, <delta> vs baseline) · <n> new bests / <total>
    candidates``. With several tracks, each track's best: ``best accuracy 0.9100 (candidate#9, +0.0200), seconds
    3.1000 (candidate#7, -0.4000)``. Imported references are not counted as candidates. ``· 3 starting points`` follows
    when there are any. ``· 1.2M tok`` follows when the usage of an agent is known: it is the total for all roles so far.
    """
    found = [b for b in best_scores(s, campaign) if b.candidate is not None]
    total = count(s, produced(campaign))
    bests = len(new_bests(s, campaign))
    imported = len(references(s, campaign))
    if not found:
        best_text = 'no baseline yet'
    else:
        best_text = 'best ' + ', '.join(
            f'{b.track.primary.name} {_four(b.value)} (candidate#{b.candidate}'
            + (f', {metric(b.delta, signed=True)} vs baseline' if b.delta is not None else '')
            + ')'
            for b in found
        )
    spent = campaign_tokens(s, campaign.id)
    tokens = f' · {human(spent)} tok' if spent else ''
    starts = f' · {imported} starting point{"s" if imported != 1 else ""}' if imported else ''
    return f'{campaign.name} · {best_text} · {bests} new bests / {total} candidates{starts}{tokens}'
