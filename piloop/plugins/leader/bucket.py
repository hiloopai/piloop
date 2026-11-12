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

Each term replicates the home's database with Litestream to ``terms/<epoch>/`` beside the record, and only there: a
stale writer never writes where a newer term reads. The record names the source of the next restore (``Source``): the
prefix and the transaction that the holder restored, until its own replica holds the whole database, then its own
prefix. A term restores from that source, and names it as its own before it serves, so a term that dies before its first
replica leaves the source as it found it. A term whose replica does not hold the whole database within
``replica_wait`` stops rather than go on serving writes that nothing ships. The protocol is model-checked in ``tests/models/replica.tla``.

boto3 is imported when the record opens, not with this module: it takes ~0.5 s, and every command reads the settings
that name this provider. Credentials are boto3's own chain: the environment, a profile, an instance role.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from piloop.campaign.campaigns import REPOS
from piloop.db import ids, terms
from piloop.db.models import utcnow
from piloop.plugins.cas.contract import CasProvider
from piloop.plugins.host import Check, Severity
from piloop.plugins.leader.contract import LeaderConfig, LeaderHost, LostTerm, Renewal, Term, Unreachable

if TYPE_CHECKING:
    from botocore.exceptions import ClientError
    from mypy_boto3_s3 import S3Client

KEY = 'term.json'
TERMS = 'terms'
"""``terms/<epoch>``: the replica of each term, beside the record."""
FIRST = '0000/0000000000000001-'
"""The start of the key of a replica's first file: Litestream's first sync holds the whole database."""
REFUSED = ('PreconditionFailed', 'ConditionalRequestConflict')
"""What S3 answers a conditional write that another write beat: 412, or 409 while the other is under way."""
NOT_FOUND = ('404', 'NoSuchKey', 'NotFound')


class Source(BaseModel):
    """Where the next term restores the home's database from."""

    model_config = ConfigDict(frozen=True)

    epoch: int
    """The term whose replica it is: ``terms/<epoch>``."""
    txid: str | None = None
    """The last transaction to restore (Litestream's hex TXID). None: the replica's newest."""


class Record(Term):
    """The object: the term, and the source of the next restore. None: there is nothing to restore."""

    source: Source | None = None


class Replicating(Protocol):
    """A replication that runs: ``stop`` ends it once it shipped what it has."""

    def stop(self) -> None:
        """Ship what it has, then end."""
        ...


class Replica(Protocol):
    """What replicates the home's database to a prefix of the bucket, and restores it from one: Litestream."""

    def restore(self, url: str, txid: str | None, out: Path) -> str:
        """Write the database of the replica at ``url`` to ``out``, and return the last transaction that it holds.

        It holds those up to ``txid``; None: the replica's newest.

        Raises:
            Unreachable: The replica could not be read.
        """
        ...

    def replicate(self, database: Path, url: str) -> Replicating:
        """Ship ``database`` to the replica at ``url``, which is new, from now on: its first file holds all of it."""
        ...


class Litestream:
    """``litestream`` (0.5) in a child process: what the hosted image holds."""

    def __init__(self, executable: str, region: str | None) -> None:
        self._executable = executable
        self._env = {**os.environ, **({'AWS_REGION': region} if region else {})}

    def restore(self, url: str, txid: str | None, out: Path) -> str:
        argv = [self._executable, 'restore', '-json', '-o', str(out), *(['-txid', txid] if txid else []), url]
        done = subprocess.run(argv, capture_output=True, text=True, env=self._env, check=False)
        if done.returncode != 0:
            raise Unreachable(f'litestream restore {url}: {done.stderr.strip()[-500:]}')
        return str(json.loads(done.stdout)['txid'])

    def replicate(self, database: Path, url: str) -> Replicating:
        """Start Litestream with no local state: what an earlier replication left there belongs to another replica.

        That replication may be an earlier term's on this host, or a sidecar's. Once Litestream's L0 retention dropped
        TXID 1 from that state, a new replica gets one snapshot and then fails every upload: no later write ships.
        Nothing needs that state: each term's replica is new, and starts at TXID 1.
        """
        with suppress(FileNotFoundError):
            shutil.rmtree(database.with_name(f'.{database.name}-litestream'))
        argv = [self._executable, 'replicate', str(database), url]
        return _Child(subprocess.Popen(argv, stdout=subprocess.DEVNULL, env=self._env))  # its log goes to the daemon's


@dataclass(frozen=True)
class _Child:
    process: subprocess.Popen[bytes]

    def stop(self) -> None:
        """SIGTERM: Litestream ships its last frames, then exits."""
        self.process.terminate()
        self.process.wait()


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
    litestream: str = 'litestream'
    """The Litestream executable (0.5), which replicates and restores the home's database."""
    replica_poll: timedelta = Field(default=timedelta(seconds=0.5), gt=timedelta(0))
    """How often a new term looks whether its replica holds the whole database yet: the record names it then."""
    replica_wait: timedelta = Field(default=timedelta(seconds=60), gt=timedelta(0))
    """How long a new term waits for its replica to hold the whole database. Past it, the daemon stops rather than
    serve a term that it does not replicate. Litestream ships a database of 100 MB in about 5 s."""

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
        if not self.replica_poll < self.replica_wait:
            raise ValueError(f'replica_poll ({self.replica_poll}) < replica_wait ({self.replica_wait}) does not hold')
        return self

    def checks(self) -> list[Check]:
        found = shutil.which(self.litestream)
        if found is None:
            return [Check('leader', f'{self.litestream} is not on PATH', 'install Litestream 0.5', Severity.FAIL)]
        return [Check('leader', f'bucket {self.url}, Litestream at {found}')]

    def refuses_cas(self, cas: CasProvider) -> str | None:
        if cas.shared():
            return None
        return (
            'the CAS is a directory of this host: under the bucket leader, another host makes the home again from the'
            ' CAS, so choose one that every host reaches (s3, gcs, http, or cached over one of them)'
        )

    def refuses(self, home: Path, checkout: Path) -> str | None:
        if checkout.resolve().is_relative_to((home / REPOS).resolve()):
            return None
        return (
            f"{checkout} is a checkout of your own: under the bucket leader, another host makes a campaign's repo again"
            ' from its clone, so register it by its URL (`piloop init <url>`)'
        )

    def open(self, host: LeaderHost) -> BucketTerms:
        return BucketTerms(self, host)


class BucketTerms:
    """The home's term in its object. The holder's own last write is the only one it renews."""

    def __init__(
        self,
        config: BucketLeader,
        host: LeaderHost,
        client: S3Client | None = None,
        clock: Callable[[], datetime] = utcnow,
        replica: Replica | None = None,
    ) -> None:
        import boto3  # here, not at the top: see the module docstring
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        parsed = urlparse(config.url)
        prefix = parsed.path.strip('/')
        self.bucket = parsed.netloc
        self.prefix = f'{prefix}/' if prefix else ''
        self.key = f'{self.prefix}{KEY}'
        self.config = config
        self.renewal = Renewal(config.renew, config.deadline)
        seconds = config.renew.total_seconds()
        timeouts = Config(
            connect_timeout=seconds, read_timeout=seconds, retries={'mode': 'standard', 'max_attempts': 2}
        )
        self.client = client or boto3.client('s3', config=timeouts)
        """A call ends within about a renewal: a renewal that hangs must not outlast the term."""
        self._host = host
        self._clock = clock
        self.replica = replica or Litestream(config.litestream, self.client.meta.region_name)
        self._lock = threading.Lock()
        """One write of the record at a time: the renewals' and the replica's watch."""
        self._client_error: type[ClientError] = ClientError
        self._boto_error: type[BotoCoreError] = BotoCoreError
        self._held: tuple[Record, str] | None = None
        """The record of the holder's last write, and that write's ETag."""

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
        newest = terms.newest_in(self._host.database)
        epoch = max(0 if record is None else record.epoch, newest) + 1
        until = self._clock() + self.config.ttl
        taken = Record(
            epoch=epoch, holder=holder, url=url, until=until, source=None if record is None else record.source
        )
        return _term(taken) if self._write(taken, etag) else None

    def renew(self, term: Term) -> Term:
        return self._again(term, self._clock() + self.config.ttl)

    def release(self, term: Term) -> None:
        self._again(term, self._clock())
        self._held = None

    def restore(self, term: Term) -> bool:
        record, _ = self._read()
        if record is None or record.epoch != term.epoch:
            raise LostTerm(f'term {term.epoch} is no longer the newest: another daemon took over before it restored')
        source = record.source
        if source is None:  # the first term of the bucket: the home is as this host has it
            self._again(term, None)  # the pin, as for a restore: the term is still this one's before it serves
            return False
        database = self._host.database
        staged = database.with_name(f'{database.name}.restoring')
        staged.unlink(missing_ok=True)
        txid = self.replica.restore(self._url(source.epoch), source.txid, staged)
        for sidecar in ('-wal', '-shm'):
            database.with_name(database.name + sidecar).unlink(missing_ok=True)
        staged.replace(database)
        self._again(term, None, Source(epoch=source.epoch, txid=txid))
        return True

    @contextmanager
    def replicate(self, term: Term, unreplicated: Callable[[str], None]) -> Generator[None]:
        replicating = self.replica.replicate(self._host.database, self._url(term.epoch))
        stop = threading.Event()
        args = (term, stop, unreplicated)
        watch = threading.Thread(target=self._watch, args=args, name='replica-watch', daemon=True)
        watch.start()
        try:
            yield
        finally:
            stop.set()
            watch.join()
            replicating.stop()

    def _watch(self, term: Term, stop: threading.Event, unreplicated: Callable[[str], None]) -> None:
        """Name this term's own replica as the source once it holds the whole database: a restore needs no other.

        S3 that does not answer is asked again at the next poll. A lost term stops the watch: the renewals end the
        daemon then. Past ``replica_wait`` without the replica, it says why to ``unreplicated``, and stops.
        """
        until = self._clock() + self.config.replica_wait
        while not stop.wait(self.config.replica_poll.total_seconds()):
            try:
                if self.named(term):
                    return
            except (self._client_error, self._boto_error, Unreachable) as e:
                print(f'piloop: term {term.epoch} looks again for its replica: {e}', file=sys.stderr)
            except LostTerm:
                return
            if self._clock() >= until:
                why = (
                    f'term {term.epoch}: its replica {self._url(term.epoch)} does not hold the whole database'
                    f" {self.config.replica_wait} after its start, so nothing ships its writes (Litestream's log says why)"
                )
                print(f'piloop: error: {why}', file=sys.stderr, flush=True)
                unreplicated(why)
                return

    def named(self, term: Term) -> bool:
        """Name ``term``'s own replica as the source if it holds the whole database now, and whether it does.

        Raises:
            LostTerm: Another daemon took a newer term.
            Unreachable: The record could not be written now.
        """
        first = f'{self.prefix}{TERMS}/{term.epoch}/{FIRST}'
        if not self.client.list_objects_v2(Bucket=self.bucket, Prefix=first, MaxKeys=1).get('KeyCount'):
            return False
        self._again(term, None, Source(epoch=term.epoch))
        return True

    def begin(self, s: Session, term: Term) -> None:
        """Lift the id sequences past every id of an earlier term: a restore may have lost some (decision 17).

        Raises:
            IdsExhausted: The term's ids would pass 2^53.
        """
        ids.lift(s, ids.floor(term.epoch))

    def current(self) -> Term | None:
        record, _ = self._read()
        if record is None or record.until is None or record.until <= self._clock():
            return None
        return _term(record)

    def _again(self, term: Term, until: datetime | None, source: Source | None = None) -> Term:
        """Write the holder's record again: a later ``until`` (None: as it was), and ``source`` (None: as it was)."""
        with self._lock:
            if self._held is None or self._held[0].epoch != term.epoch:
                raise LostTerm(f'term {term.epoch} is not the one that this process wrote last')
            held, etag = self._held
            again = held.model_copy(update={'until': until or held.until, 'source': source or held.source})
            if not self._write(again, etag):
                self._held = None
                raise LostTerm(f'another daemon wrote s3://{self.bucket}/{self.key} after term {term.epoch}')
            return _term(again)

    def _url(self, epoch: int) -> str:
        return f's3://{self.bucket}/{self.prefix}{TERMS}/{epoch}'

    def _expired(self, record: Term) -> bool:
        return record.until is None or self._clock() >= record.until + self.config.max_skew

    def _read(self) -> tuple[Record | None, str | None]:
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
        return Record.model_validate_json(got['Body'].read()), got['ETag']

    def _write(self, term: Record, etag: str | None) -> bool:
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


def _term(record: Record) -> Term:
    """The term of ``record``: what the daemon holds, without the source, which only this provider reads."""
    return Term.model_validate(record.model_dump(exclude={'source'}))
