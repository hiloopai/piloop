"""The server's half of a worker's fetch: the versions that a clone lacks, from the repo of record into the CAS.

A clone that has versions names its newest (``MAX_HAVE``), and the server puts what it lacks beyond them into the CAS
(``Repository.send``), in the request. An empty clone names none, and lacks everything: the repo's first transfer.
That can take minutes for a large repo, longer than a request lasts, and every new worker asks for it. So it is built
once per repo, and its row (``first_transfers``) keeps its address: each empty clone gets that blob and the versions
beyond it.

Any process that serves the API answers a ``send`` (``FirstTransfers``), and only the daemon builds
(``FirstTransferBuilds``, a consumer of its loop). A request that finds no row, or a failed build, asks for one: the row
is ``requested``, and its event wakes the daemon. The build's end, ``built`` or ``failed``, is an event that wakes the
requests that wait (``settled``), in whichever process they wait. A build past its deadline fails each request, so a hang
fails jobs instead of holding their slots; none starts another until it ends. A failure is raised to the first request
that reads it, which asks again: the next build starts.

After git's bundle URIs and Mercurial's clone bundles: a new clone starts from a bundle that the server built once,
then fetches what is newer.
"""

import threading
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import datetime, timedelta
from typing import ClassVar, Literal

from covalence import Cid
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.daemon.loop import Consumer, Phase, Turn
from piloop.db.events import Topic, emit
from piloop.db.models import EventModel, ReadBy, utcnow
from piloop.db.session import Database, write_transaction
from piloop.ids import Revision, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.cas.contract import Cas
from piloop.plugins.executor.contract import RepoId
from piloop.plugins.executor.kit.clones import NotSent
from piloop.plugins.executor.kit.tables import FirstTransfer, FirstTransferState
from piloop.plugins.vcs.contract import Repository

type Send = Callable[[RepoId, Sequence[Revision], Sequence[Revision]], list[Cid] | None]
"""What the ``send`` route answers a worker that lacks versions: ``FirstTransfers.send``. None: ask again."""


class FirstTransferRequested(EventModel):
    """On the campaign of the repo of record: a request asks for its first transfer. The daemon builds it."""

    kind: Literal['first_transfer.requested'] = 'first_transfer.requested'


class FirstTransferSettled(EventModel):
    """On the campaign of the repo of record: the build of its first transfer ended, ``built`` or ``failed``."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['first_transfer.built', 'first_transfer.failed']
    error: str | None = None


def settled(repo: RepoId) -> tuple[Topic, ...]:
    """What a ``send`` of ``repo`` awaits between two reads: the end of its build."""
    return (Topic(FirstTransferSettled, target=_on(repo)),)


def _on(repo: RepoId) -> TargetRef:
    return TargetRef(TargetKind.CAMPAIGN, repo)


def send_of_record(
    cas: Cas,
    records: Callable[[RepoId], Repository],
    repo: RepoId,
    revisions: Sequence[Revision],
    have: Sequence[Revision],
) -> list[Cid]:
    """Put what ``revisions`` have beyond ``have`` from the repo of record into the CAS, in one blob or none.

    The repo of record is opened with its campaign's own vcs provider (``Host.records``), never one that a worker names.
    """
    with closing(records(repo)) as record:
        blob = record.send(cas, revisions, have)
    return [] if blob is None else [blob]


class FirstTransfers:
    """What the ``send`` route answers: ``send_of_record``, from the repo's first transfer for an empty clone."""

    def __init__(self, db: Database, cas: Cas, records: Callable[[RepoId], Repository], deadline: float) -> None:
        self._db = db
        self._cas = cas
        self._records = records
        self._deadline = timedelta(seconds=deadline)
        """How long a build may take before the requests that wait on it fail (``Workers``)."""

    def send(self, repo: RepoId, revisions: Sequence[Revision], have: Sequence[Revision]) -> list[Cid] | None:
        """What ``revisions`` have beyond ``have``, as ``Fetch`` answers it. None while the first transfer is built.

        Raises:
            NotSent: The build of the first transfer failed, and the next request builds it again. Or it is past its
                deadline: each request fails until it ends, and none starts another.
        """
        if have:
            return send_of_record(self._cas, self._records, repo, revisions, have)
        if (first := self._first(repo)) is None:
            return None
        assert first.blob is not None and first.revision is not None  # a built row's
        return [first.blob, *send_of_record(self._cas, self._records, repo, revisions, [first.revision])]

    def _first(self, repo: RepoId) -> FirstTransfer | None:
        """The repo's built first transfer, or None while it is built: a request that finds none asks for it.

        It reads the row, and the CAS, with no transaction: a request that finds none, a failed one or a blob that the
        CAS lost takes the write lock only to ask, once it read the row again under it and found it as it was. One
        that changed meanwhile, another request's ask or the daemon's build, is the next read's to see.
        """
        with self._db.session() as s:
            seen = s.get(FirstTransfer, repo)
        match seen:
            case FirstTransfer(state=FirstTransferState.BUILT, blob=Cid() as blob) if self._cas.has(blob):
                return seen
            case FirstTransfer(state=FirstTransferState.REQUESTED, started_at=started):
                if utcnow() - started > self._deadline:
                    raise NotSent(
                        f'the first transfer of repo {repo} has built for longer than its deadline of'
                        f' {self._deadline.total_seconds():g} s: the send of the repo of record or its upload to the'
                        ' CAS hangs'
                    )
                return None
        with self._db.session() as s:
            write_transaction(s)
            if _state(s.get(FirstTransfer, repo)) != _state(seen):
                return None
            _request(s, repo)
            s.commit()
        if seen is not None and seen.state is FirstTransferState.FAILED:
            raise NotSent(f'the first transfer of repo {repo} failed: {seen.error}')
        return None


def _state(row: FirstTransfer | None) -> tuple[FirstTransferState, datetime, Cid | None] | None:
    """What a request read of a row: whether another request or the build changed it since."""
    return None if row is None else (row.state, row.started_at, row.blob)


def _request(s: Session, repo: RepoId) -> None:
    """Ask the daemon for the first transfer of ``repo``, in place of what its row had. The caller commits."""
    asked = FirstTransfer(
        campaign_id=repo, state=FirstTransferState.REQUESTED, started_at=utcnow(), revision=None, blob=None, error=None
    )
    s.merge(asked)
    emit(s, FirstTransferRequested(), _on(repo), System.DAEMON)


class FirstTransferBuilds:
    """The daemon's consumer that builds each requested first transfer, each in a thread of its own."""

    def __init__(self, db: Database, cas: Cas, records: Callable[[RepoId], Repository], tracer: Tracer) -> None:
        self._db = db
        self._cas = cas
        self._records = records
        self._tracer = tracer
        self._lock = threading.Lock()
        self._building: set[RepoId] = set()
        """The repos whose build runs in this process: a turn starts no second one."""

    def consumer(self) -> Consumer:
        """Its job in the daemon's loop: it wakes on each request, and runs once when the daemon starts."""
        return Consumer('first_transfers', frozenset({FirstTransferRequested}), self.run, Phase.UPKEEP)

    def run(self, turn: Turn) -> datetime | None:
        """Start the build of each requested first transfer that no thread of this daemon builds.

        A build's start is its row's ``started_at``: one that an earlier daemon asked for counts from this start.
        """
        requested = select(FirstTransfer.campaign_id).where(FirstTransfer.state == FirstTransferState.REQUESTED)
        with self._db.session() as s:
            asked = s.scalars(requested).all()
        with self._lock:
            new = [RepoId(r) for r in asked if r not in self._building]
            self._building.update(new)
        if new:
            with self._db.session() as s:
                write_transaction(s)
                s.execute(
                    update(FirstTransfer)
                    .where(FirstTransfer.campaign_id.in_(new), FirstTransfer.state == FirstTransferState.REQUESTED)
                    .values(started_at=utcnow())
                )
                s.commit()
        for repo in new:
            threading.Thread(target=self._build, args=(repo,), name=f'first-transfer-{repo}', daemon=True).start()
        return None

    def _build(self, repo: RepoId) -> None:
        """Put the repo of record's head and all before it into the CAS, and write the row: ``built``, or ``failed``."""
        try:
            with self._tracer.span('piloop.first_transfer', repo=repo):
                with closing(self._records(repo)) as record:
                    head = record.head().revision
                    blob = record.send(self._cas, [head], [])
                assert blob is not None  # with nothing had, the head is sent
            self._settle(repo, FirstTransferSettled(kind='first_transfer.built'), revision=head, blob=blob)
        except Exception as e:
            self._settle(repo, FirstTransferSettled(kind='first_transfer.failed', error=f'{type(e).__name__}: {e}'))
        finally:
            with self._lock:
                self._building.discard(repo)

    def _settle(
        self, repo: RepoId, event: FirstTransferSettled, revision: Revision | None = None, blob: Cid | None = None
    ) -> None:
        """Write the end of the build on the row, with its event, which wakes the requests that wait."""
        with self._db.session() as s:
            write_transaction(s)
            if (row := s.get(FirstTransfer, repo)) is None:  # its campaign is gone
                return
            row.state = FirstTransferState.BUILT if blob is not None else FirstTransferState.FAILED
            row.revision, row.blob, row.error = revision, blob, event.error
            emit(s, event, _on(repo), System.DAEMON)
            s.commit()
