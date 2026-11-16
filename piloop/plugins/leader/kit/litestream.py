"""The bucket leader's replica adapter: Litestream in a child process, with a private control socket."""

import json
import math
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from piloop.plugins.leader.contract import Unreachable

TAIL = 5
SYNC_SECONDS = 5
STOP_SECONDS = 32
"""Allow Litestream 0.5.17's 30s shutdown-sync retry window, then fail cleanup rather than hang."""


class Replicating(Protocol):
    """A replication that runs: prove its last write before stopping it."""

    def exited(self) -> str | None:
        """Why the replication ended by itself, or None while it runs."""
        ...

    def sync(self) -> str:
        """Ship all quiesced database writes and return the proven remote hex TXID.

        Raises:
            Unreachable: Replication could not be proved within its bound.
        """
        ...

    def stop(self) -> None:
        """End the process. Its exit alone does not prove successful replication."""
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
    """``litestream`` (0.5) in a child process: what the hosted image holds.

    Each ``s3://`` URL names the bucket's region: the region of the provider's own S3 client. Without one, Litestream
    asks S3 for it (``s3:GetBucketLocation``), a right that the home's objects do not otherwise need, and exits when
    it is refused.
    """

    def __init__(self, executable: str, region: str | None) -> None:
        self._executable = executable
        self._region = region

    def _located(self, url: str) -> str:
        return f'{url}?region={self._region}' if self._region and url.startswith('s3://') else url

    def restore(self, url: str, txid: str | None, out: Path) -> str:
        located = self._located(url)
        argv = [self._executable, 'restore', '-json', '-o', str(out), *(['-txid', txid] if txid else []), located]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise Unreachable(f'litestream restore {located}: {done.stderr.strip()[-500:]}')
        return str(json.loads(done.stdout)['txid'])

    def replicate(self, database: Path, url: str) -> Replicating:
        """Start Litestream with no local state: what an earlier replication left there belongs to another replica.

        That replication may be an earlier term's on this host, or a sidecar's. Once Litestream's L0 retention dropped
        TXID 1 from that state, a new replica gets one snapshot and then fails every upload: no later write ships.
        Nothing needs that state: each term's replica is new, and starts at TXID 1.
        """
        with suppress(FileNotFoundError):
            shutil.rmtree(database.with_name(f'.{database.name}-litestream'))
        return _Child(self._executable, database.absolute(), self._located(url))


class _Synced(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')

    db_path: str
    txid: int = Field(ge=0, le=2**64 - 1)
    replica_txid: int = Field(ge=0, le=2**64 - 1)
    duration_ms: int = Field(ge=0)


class _Child:
    """Litestream's ``replicate``. Its log goes on to the daemon's, and its last lines say why when it exits."""

    def __init__(self, executable: str, database: Path, url: str) -> None:
        self._executable, self._database = executable, database.resolve()
        self._source_database = database
        self._identity = database.stat()
        self._private = tempfile.TemporaryDirectory(prefix='piloop-replica-')
        private = Path(self._private.name)
        self._socket = private / 'control.sock'
        config = private / 'config.json'
        config.touch(mode=0o600)
        config.write_text(
            json.dumps(
                {
                    'socket': {'enabled': True, 'path': str(self._socket), 'permissions': 0o600},
                    'dbs': [{'path': str(self._database), 'replica': {'url': url}}],
                }
            )
        )
        try:
            self.process = subprocess.Popen(
                [executable, 'replicate', '-no-expand-env', '-config', str(config)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError:
            self._private.cleanup()
            raise
        self._tail: deque[str] = deque(maxlen=TAIL)
        self._log = threading.Thread(target=self._relay, name='litestream-log', daemon=True)
        self._log.start()

    def _relay(self) -> None:
        assert self.process.stderr is not None
        with self.process.stderr:
            for line in self.process.stderr:
                self._tail.append(line.rstrip())
                sys.stderr.write(line)

    def exited(self) -> str | None:
        if (code := self.process.poll()) is None:
            return None
        # Log delivery must not hold the daemon's ownership/lifecycle behind a blocked stderr.
        self._log.join(timeout=1)
        return f'Litestream exited with status {code}: ' + ' / '.join(self._tail)

    def stop(self) -> None:
        """Bounded cleanup; Litestream 0.5.17 can exit zero after a failed final flush."""
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired as error:
                self.process.kill()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired as reap:
                    raise Unreachable('Litestream could not be reaped after forced cleanup') from reap
                raise Unreachable('Litestream did not stop within its grace; replication is not proved') from error
        finally:
            self._log.join(timeout=1)
            self._private.cleanup()

    def sync(self) -> str:
        """The control operation syncs WAL→LTX→remote and reports both resulting positions.

        A private socket/config binds it to this child, database generation and epoch-specific URL.
        A stopped child or a different inode cannot supply this term's final-write proof.
        """
        try:
            deadline = time.monotonic() + SYNC_SECONDS
            self._same_database()
            # Popen returns before the private control listener binds. Wait for that actual
            # boundary within the same sync budget; a quick healthy stop must not race startup.
            while not self._socket.exists():
                self._same_database()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Unreachable('Litestream final sync control socket did not become ready')
                time.sleep(min(0.01, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Unreachable('Litestream final sync exhausted its control readiness budget')
            done = subprocess.run(
                [
                    self._executable,
                    'sync',
                    '-wait',
                    '-json',
                    '-timeout',
                    str(max(1, math.ceil(remaining))),
                    '-socket',
                    str(self._socket),
                    str(self._database),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=remaining + 1,
            )
            if done.returncode != 0:
                raise Unreachable(f'Litestream final sync failed: {done.stderr.strip()[-500:]}')
            proof = _Synced.model_validate_json(done.stdout)
            self._same_database()
        except (OSError, subprocess.TimeoutExpired, ValidationError) as error:
            raise Unreachable(f'Litestream final sync of {self._database}: {error}') from error
        if proof.db_path != str(self._database) or proof.txid != proof.replica_txid or proof.txid == 0:
            # No restorable position is zero. Litestream initializes even a zero-byte SQLite DB
            # at TXID 1; an absent DB fails SyncAndWait rather than proving a snapshot.
            raise Unreachable(f'Litestream final sync has no matching initialized replica: {proof}')
        return f'{proof.txid:016x}'

    def _same_database(self) -> None:
        if self.process.poll() is not None:
            raise Unreachable('Litestream final sync does not belong to the running database generation')
        for path in (self._source_database, self._database):
            current = path.stat()
            if (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino):
                raise Unreachable('Litestream final sync does not belong to the running database generation')
