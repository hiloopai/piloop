"""The schema of ``secrets/local``: the ``secrets`` table, closed to ``piloop sql``."""

from pathlib import Path

from sqlalchemy import ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from piloop.db.models import Base
from piloop.db.schema import Schema
from piloop.db.triggers import on
from piloop.ids import CampaignId


class Secret(Base):
    """A secret of the ``local`` secrets provider (``piloop secret set``): a value that agents get in their environment.

    ``piloop sql`` cannot write the table, and reads ``value`` as NULL: an agent writes its campaign's secrets through
    ``piloop secret``, which checks that the campaign is its own, and gets their values in its environment only. Who
    set a secret, and when, is in its ``secret.set`` events.
    """

    __tablename__ = 'secrets'
    __table_args__ = (
        UniqueConstraint('campaign_id', 'name'),
        Index('ux_secrets_home_name', 'name', unique=True, sqlite_where=text('campaign_id IS NULL')),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId | None] = mapped_column(ForeignKey('campaigns.id'))
    """The campaign whose agents get it, over the home's of the same name. NULL: the home's, for every campaign. A
    campaign with secrets is deleted after them: ``piloop sql`` cannot delete them, and a cascade would."""
    name: Mapped[str]
    """An environment variable's name."""
    value: Mapped[str]


TABLES = (Secret,)
SCHEMA = Schema(
    tables=TABLES,
    versions=Path(__file__).parent / 'migrations',
    triggers=on(TABLES),
    internal=TABLES,
    protected=TABLES,
    hidden=(Secret.value,),
)
