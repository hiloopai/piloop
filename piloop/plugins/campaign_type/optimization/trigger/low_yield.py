from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import Field
from sqlalchemy.orm import Session

from piloop.db.models import Campaign
from piloop.ids import TargetKind, TargetRef
from piloop.plugins.campaign_type.optimization.yields import Found, Low, Yield, ended_at, recent
from piloop.plugins.trigger.contract import TRIGGERS, Fire, Pass, Trigger
from piloop.plugins.trigger.kit.incidents import Incident, given, report

if TYPE_CHECKING:  # piloop.config types the roles with their triggers: only the annotation names it
    from piloop.config import CampaignConfig


class LowYield(Trigger):
    """Of the last ``window`` finished runs, fewer than ``min_scored`` scored, or ``keepless`` scored and none is kept.

    The agent gets the breakdown to investigate, once until the yield recovers. The daemon also posts one escalation
    of priority 1 while the yield stays low, whether a role names the trigger or not.
    """

    INVESTIGATES: ClassVar[bool] = True

    window: int = Field(default=20, ge=1)
    """How many of the latest finished attempts it looks at."""
    min_scored: float = Field(default=0.25, ge=0, le=1)
    """The share of them that must reach the evaluator. 0 turns this check off."""
    keepless: int | None = Field(default=15, ge=1)
    """It fires when at least this many of them scored and none is kept. Unset turns this check off."""

    @classmethod
    def of(cls, cfg: CampaignConfig) -> Self:
        """The campaign's yield alarm: its ``[plugins.trigger.low_yield]``, else the defaults."""
        return next(t for t in TRIGGERS.instances(cfg.plugins).values() if isinstance(t, cls))

    def check(self, s: Session, campaign: Campaign) -> Found | None:
        """The campaign's yield when it is low, or None while the window is not full or the yield is not low."""
        return self.low(recent(s, campaign, self.window))

    def low(self, got: Yield) -> Found | None:
        """Why the yield of a window is low: the first check that it fails. None while the window is not full."""
        if got.runs < self.window:
            return None
        checks = (
            (Low.UNSCORED, got.scored < self.min_scored * self.window, f'fewer than {self.min_scored:.0%} scored'),
            (
                Low.KEEPLESS,
                self.keepless is not None and got.scored >= self.keepless and not got.kept,
                f'none kept of {got.scored} scored',
            ),
        )
        return next((Found(got, low, detail) for low, hit, detail in checks if hit), None)

    def fire(self, p: Pass) -> Fire | None:
        found = self.check(p.s, p.campaign)
        if found is None or found.got.last is None:
            return None
        since = ended_at(p.s, p.campaign)
        if any(since is None or e.at > since for e, _ in given(p, TRIGGERS.name_of(self))):
            return None
        subject = TargetRef(TargetKind.CAMPAIGN, p.campaign.id)
        return report(p, self, [Incident(subject, found.got.last, 'yields little', str(found))])

    def due_in(self, p: Pass) -> timedelta | None:
        return None
