from piloop.api.views import Figure, Figures, Rendered
from piloop.db.models import count
from piloop.ids import CampaignStatus
from piloop.plugins.campaign_type.kit.candidates.references import references
from piloop.plugins.campaign_type.kit.candidates.tables import produced
from piloop.plugins.campaign_type.kit.evaluation.tables import evaluator_of
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.reports.overview import holds
from piloop.text import age
from piloop.usage.totals import campaign_tokens


class Headline(Section):
    """The best of each track and its score, the candidates, the tokens, the evaluator, and why the campaign is held.

    A score of the best comes with its delta against the baseline. The section says why the campaign stopped or is
    paused. Imported references are not candidates.
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.ORCHESTRATOR})
    ORDER = 100

    def render(self, r: Rendering) -> Rendered:
        s, campaign = r.s, r.campaign
        figures: list[Figure] = []
        for track in campaign.config.all_tracks:
            pool = Pool.of(s, campaign, track)
            best = pool.best()
            name = track.primary.name
            label = 'best' if not campaign.config.multitrack else f'best of {track.name}'
            figures.append(Figure(label=label, value=f'candidate#{best}' if best is not None else 'no baseline yet'))
            if best is None:
                continue
            figures.append(Figure(label=name, value=pool.mean(best)))
            if (gain := pool.gain(best, pool.baseline)) is not None:
                figures.append(Figure(label=f'{name} vs baseline', value=gain))
            figures.append(Figure(label=f'may be best on {track.name}', value=len(pool.ranked)))
        figures.append(Figure(label='candidates', value=count(s, produced(campaign))))
        if imported := len(references(s, campaign)):
            figures.append(Figure(label='starting points', value=imported))
        if spent := campaign_tokens(s, campaign.id):
            figures.append(Figure(label='tokens', value=spent))
        if (evaluator := evaluator_of(r.s, campaign)) is not None:
            figures.append(Figure(label='evaluator', value=f'#{evaluator.id}, {age(evaluator.created_at, r.now)} old'))
        if campaign.status is CampaignStatus.STOPPED:
            figures.append(Figure(label='stopped because', value=campaign.stopped_reason))
        if campaign.paused is not None:
            figures += [
                Figure(label='paused', value=holds(campaign)),
                Figure(label='paused because', value=campaign.paused_reason),
            ]
        return Rendered(title='Campaign', blocks=(Figures(figures=tuple(figures)),))
