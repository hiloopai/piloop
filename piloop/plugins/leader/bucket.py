"""``bucket``: the term in one S3 object, ``s3://<bucket>/<prefix>/term.json``, for a home that several hosts serve.

A take reads the object. With none, it creates it (``If-None-Match: *``); with one whose ``until`` passed, by the
reader's clock, more than ``max_skew`` ago, it replaces it with the next epoch (``If-Match`` on the ETag it read).
The holder renews with ``If-Match`` on the ETag of its own last write, and its release writes ``until`` = now the same
way: it never deletes the object, whose epoch the next take counts from. Two writes never race through: S3 applies
one and refuses the other (412). Each write's body differs from every earlier one (a higher epoch, or a later
``until``), so an ETag never comes back.

The holder sets ``until`` from its clock when it sends the write, and stops writing there (``piloop.daemon.holding``).
A taker waits ``max_skew`` more: safe while no two hosts' clocks differ by more. The protocol is model-checked in
``tests/models/term.tla``.

boto3 is imported when the record opens, not with this module: it takes ~0.5 s, and every command reads the settings
that name this provider. Credentials are boto3's own chain: the environment, a profile, an instance role.
"""

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Self
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from piloop.db import terms
from piloop.db.models import utcnow
from piloop.db.session import Database
from piloop.plugins.leader.contract import LeaderConfig, LeaderHost, LostTerm, Renewal, Term, Unreachable

if TYPE_CHECKING:
    from botocore.exceptions import ClientError
    from mypy_boto3_s3 import S3Client

KEY = 'term.json'
REFUSED = ('PreconditionFailed', 'ConditionalRequestConflict')
"""What S3 answers a conditional write that another write beat: 412, or 409 while the other is under way."""
NOT_FOUND = ('404', 'NoSuchKey', 'NotFound')


class BucketLeader(LeaderConfig):
    """``[plugins.leader] provider = "bucket"``: the object that holds the term, and its timings.

    The defaults: a term of 30 s, renewed every 5 s, given up after 20 s without a renewal: four tries before the
    holder begins to stop, and 10 s for its stop. A standby takes over at most ~37 s after a holder dies, and a holder
    writes about 520,000 times a month.
    """

    url: str
    """``s3://<bucket>[/<prefix>]``: the term is ``term.json`` there. One per home."""
    ttl: timedelta = Field(default=timedelta(seconds=30), gt=timedelta(0))
    """How long a term lasts past its take or its latest renewal."""
    renew: timedelta = Field(default=timedelta(seconds=5), gt=timedelta(0))
    """How often the holder renews, and a standby tries to take the term."""
    deadline: timedelta = Field(default=timedelta(seconds=20), gt=timedelta(0))
    """How long after its latest renewal the holder begins to stop, while it cannot renew."""
    max_skew: timedelta = Field(default=timedelta(seconds=2), ge=timedelta(0))
    """The most that two hosts' clocks differ by. A standby takes a term this long after its ``until``."""

    @field_validator('url')
    @classmethod
    def _s3_url(cls, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != 's3' or not parsed.netloc:
            raise ValueError(f'{url!r} is no s3://<bucket>[/<prefix>] URL')
        return url

    @model_validator(mode='after')
    def _in_order(self) -> Self:
        if not self.renew < self.deadline < self.ttl:
            raise ValueError(f'renew ({self.renew}) < deadline ({self.deadline}) < ttl ({self.ttl}) does not hold')
        return self

    def open(self, host: LeaderHost) -> BucketTerms:
        return BucketTerms(self, host.db)


class BucketTerms:
    """The home's term in its object. The holder's own last write is the only one it renews."""

    def __init__(
        self,
        config: BucketLeader,
        db: Database,
        client: S3Client | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        import boto3  # here, not at the top: see the module docstring
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        parsed = urlparse(config.url)
        prefix = parsed.path.strip('/')
        self.bucket = parsed.netloc
        self.key = f'{prefix}/{KEY}' if prefix else KEY
        self.config = config
        self.renewal = Renewal(config.renew, config.deadline)
        seconds = config.renew.total_seconds()
        timeouts = Config(
            connect_timeout=seconds, read_timeout=seconds, retries={'mode': 'standard', 'max_attempts': 2}
        )
        self.client = client or boto3.client('s3', config=timeouts)
        """A call ends within about a renewal: a renewal that hangs must not outlast the term."""
        self._db = db
        self._clock = clock
        self._client_error: type[ClientError] = ClientError
        self._boto_error: type[BotoCoreError] = BotoCoreError
        self._held: tuple[Term, str] | None = None
        """The term of the holder's last write, and that write's ETag."""

    def take(self, holder: str, url: str | None, stop: threading.Event) -> Term | None:
        while not stop.is_set():
            if (term := self.attempt(holder, url)) is not None:
                return term
            stop.wait(self.config.renew.total_seconds())
        return None

    def attempt(self, holder: str, url: str | None) -> Term | None:
        """Take the next term, once: None while another holds one, or when another write beat this one."""
        record, etag = self._read()
        if record is not None and not self._expired(record):
            return None
        newest = self._newest_here()
        epoch = max(0 if record is None else record.epoch, newest) + 1
        term = Term(epoch=epoch, holder=holder, url=url, until=self._clock() + self.config.ttl)
        return term if self._write(term, etag) else None

    def renew(self, term: Term) -> Term:
        return self._again(term, self._clock() + self.config.ttl)

    def release(self, term: Term) -> None:
        self._again(term, self._clock())
        self._held = None

    def current(self) -> Term | None:
        record, _ = self._read()
        if record is None or record.until is None or record.until <= self._clock():
            return None
        return record

    def _again(self, term: Term, until: datetime) -> Term:
        if self._held is None or self._held[0].epoch != term.epoch:
            raise LostTerm(f'term {term.epoch} is not the one that this process wrote last')
        again = term.model_copy(update={'until': until})
        if not self._write(again, self._held[1]):
            self._held = None
            raise LostTerm(f'another daemon wrote s3://{self.bucket}/{self.key} after term {term.epoch}')
        return again

    def _expired(self, record: Term) -> bool:
        return record.until is None or self._clock() >= record.until + self.config.max_skew

    def _newest_here(self) -> int:
        """The newest epoch of this home's database: a term taken here before, under this provider or another."""
        with self._db.session() as s:
            return terms.newest(s)

    def _read(self) -> tuple[Term | None, str | None]:
        """The record and its ETag, or none.

        Raises:
            Unreachable: S3 did not answer, or answered with another error.
        """
        try:
            got = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except self._client_error as e:
            if e.response.get('Error', {}).get('Code') in NOT_FOUND:
                return None, None
            raise Unreachable(f's3://{self.bucket}/{self.key}: {e}') from e
        except self._boto_error as e:
            raise Unreachable(f's3://{self.bucket}/{self.key}: {e}') from e
        return Term.model_validate_json(got['Body'].read()), got['ETag']

    def _write(self, term: Term, etag: str | None) -> bool:
        """Write ``term`` if the object is still the one of ``etag`` (None: if there is none). Whether it was.

        Raises:
            Unreachable: S3 did not answer, or answered with an error other than a refusal.
        """
        body = term.model_dump_json().encode()
        try:
            if etag is None:
                put = self.client.put_object(Bucket=self.bucket, Key=self.key, Body=body, IfNoneMatch='*')
            else:
                put = self.client.put_object(Bucket=self.bucket, Key=self.key, Body=body, IfMatch=etag)
        except self._client_error as e:
            if e.response.get('Error', {}).get('Code') in REFUSED:
                return False
            raise Unreachable(f's3://{self.bucket}/{self.key}: {e}') from e
        except self._boto_error as e:
            raise Unreachable(f's3://{self.bucket}/{self.key}: {e}') from e
        self._held = (term, put['ETag'])
        return True
