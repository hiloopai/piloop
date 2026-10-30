from pydantic import Field

from piloop.api.views import Rendered, Text
from piloop.plugins.campaign_type.optimization.pack import pack
from piloop.plugins.section.contract import Rendering, Section, Surface


class Pack(Section):
    """The research pack, which ``piloop history`` prints: what a sweep needs to propose work.

    It has the metrics, the analysis files and the workspace, the history, and the latest notes and digests. Its parameters bound each list.

    Nothing for an agent that got incidents to investigate: the pack is for proposing work.
    """

    SHOWN_IN = frozenset({Surface.SWEEP})
    ORDER = 910

    ideas: int = Field(default=60, ge=1)
    """How many of the newest ideas the history lists. ``piloop history`` lists as many."""
    campaign_findings: int = Field(default=10, ge=0)
    """How many of the latest agent notes on the campaign the history lists."""
    findings: int = Field(default=8, ge=0)
    """How many of the implementers' latest notes the pack lists."""
    digests: int = Field(default=5, ge=0)
    """How many of the latest digests and decisions of the orchestrator and the human the pack lists."""
    artifacts: int = Field(default=12, ge=0)
    """How many of the newest analysis artifacts (``context``) the pack outlines."""
    workspace: int = Field(default=10, ge=0)
    """How many of the newest files of the shared workspace the pack outlines."""

    def render(self, r: Rendering) -> Rendered | None:
        if r.given:
            return None
        text = pack(r.s, r.campaign, r.campaign.config, workspace=r.workspace, shown=self)
        return Rendered(title='Research pack', blocks=(Text(text=text.strip()),))
