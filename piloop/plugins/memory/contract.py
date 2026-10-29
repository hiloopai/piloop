"""The memory capability: what past campaigns leave for the next ones, such as lessons, documents and the journal.

``[plugins.memory]`` in ``piloop.toml`` chooses a campaign's provider: ``local`` by default, the tables of the home's
database.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from piloop.actor import Actor, System
from piloop.plugins.host import Capability, One, Parameters, Scope

if TYPE_CHECKING:  # the API's views, which a light client loads, answer Synced: only the annotations name the database
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign


class Synced(BaseModel):
    """What one sync of the campaign's documents did: the files that it added, those already in, those it skipped."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    added: list[str]
    unchanged: list[str]
    skipped: dict[str, str]
    """Repo-relative path → why the sync did not read it (not UTF-8 text, unreadable, outside the repo)."""

    def line(self) -> str:
        """One line for the console or a span event."""
        skipped = ''.join(f'; skipped {path}: {why}' for path, why in self.skipped.items())
        return f'documents: {len(self.added)} added, {len(self.unchanged)} unchanged{skipped}'


class MemoryProvider(Parameters, ABC):
    """A memory provider: its parameters, the keys of ``[plugins.memory]`` besides ``provider``."""

    @abstractmethod
    def sync(self, s: Session, campaign: Campaign, actor: Actor) -> Synced:
        """Take in the campaign's documents. ``piloop init`` (campaign registration) and a campaign's close call it.

        It only flushes.
        """

    def closing(self, s: Session, campaign: Campaign) -> str:
        """Sync the documents before the closing review searches them (``Closes``).

        A repo that cannot be read leaves the memory as it was, and the review still happens.
        """
        try:
            with s.begin_nested():
                return self.sync(s, campaign, System.DAEMON).line()
        except OSError as e:
            return f'documents: not synced: {type(e).__name__}: {e}'


MEMORY = Capability(
    'memory',
    MemoryProvider,
    table=One(Scope.CAMPAIGN, default='local'),
    group='piloop.memory',
    noun='memory provider',
)
"""Every memory provider by name. ``local``, built in and the default, keeps the memory in the home's database."""
