"""Make a home's files again from its database's rows and the CAS, after a restore on another host.

The database came from the term before's replica (``piloop.plugins.leader``). The files of its home did not: each
campaign's repo of record, its shared workspace, and the files that each job's reports put under its place. Their rows
name them in the CAS: ``RepoTransfer``, ``WorkspaceSnapshot`` and ``JobFiles``. Each step is idempotent: a home that is
already partly there, on the host that served it before, only gets what it lacks.
"""

import os
import shutil
from collections.abc import Sequence
from contextlib import closing
from functools import partial
from pathlib import Path

from covalence import Cid
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, undefer

from piloop.agents.results import JOB_FILES
from piloop.campaign.campaigns import cloned, vcs_of, workspace_dir
from piloop.db.models import Campaign, JobFiles, RepoTransfer, WorkspaceSnapshot
from piloop.db.session import Database
from piloop.ids import CampaignStatus, Revision
from piloop.otel import Tracer
from piloop.plugins.cas.contract import Cas
from piloop.plugins.secrets.contract import SecretsProvider
from piloop.plugins.vcs.contract import Pin, Repository, Transfer, UnknownRevision, VcsError
from piloop.store.tree import Manifest, Missing, materialise, write_files


class RebuildFailed(Exception):
    """A part of the home could not be made again: the daemon does not start, and says which."""


def rebuild(db: Database, cas: Cas, home: Path, secrets: SecretsProvider, tracer: Tracer) -> None:
    """Make each campaign's repo and workspace, and each job's files, as the database's rows name them.

    Raises:
        RebuildFailed: A repo could not be cloned or take a transfer, or the CAS lacks a blob that a row names.
    """
    with tracer.span('piloop.rebuild') as span, db.session() as s:
        campaigns = s.scalars(
            select(Campaign).where(Campaign.status != CampaignStatus.RETIRED).options(undefer(Campaign.config))
        ).all()
        transfers = s.scalars(select(RepoTransfer).order_by(RepoTransfer.id)).all()
        snapshots = s.scalars(select(WorkspaceSnapshot).order_by(WorkspaceSnapshot.id)).all()
        files = s.scalars(select(JobFiles).order_by(JobFiles.id)).all()
        newest = {w.campaign_id: w.manifest for w in snapshots}  # the last one of each campaign wins
        received = 0
        for campaign in campaigns:
            if cloned(home, campaign):
                credential = _credential(s, secrets, campaign)
                received += _repo(cas, campaign, credential, [t for t in transfers if t.campaign_id == campaign.id])
            if (manifest := newest.get(campaign.id)) is not None:
                _workspace(cas, Manifest.get(cas, manifest), workspace_dir(home, campaign.id))
        for row in files:
            blob = cas.get(row.files)
            if blob is None:
                raise RebuildFailed(f'the files of {row.place} ({row.files}) are not in the CAS')
            try:
                write_files(cas, JOB_FILES.validate_json(blob), home / row.place)
            except Missing as e:
                raise RebuildFailed(f'{row.place}: {e}') from e
        span.set(campaigns=len(campaigns), received=received, job_files=len(files))


def _credential(s: Session, secrets: SecretsProvider, campaign: Campaign) -> SecretStr | None:
    """The value of the home's secret that the campaign was cloned with, or None when it named none."""
    if (name := campaign.clone_secret) is None:
        return None
    if (value := secrets.get(s, None, {name}).get(name)) is None:
        raise RebuildFailed(f'campaign {campaign.name}: the home has no secret {name} to clone it with')
    return value


def _repo(cas: Cas, campaign: Campaign, credential: SecretStr | None, transfers: list[RepoTransfer]) -> int:
    """Make the campaign's repo where it was, unless it is there, and apply what it received that it lacks.

    It clones its URL, or, for a fork of a retired campaign, fetches the pack that it began from (a transfer with no
    base).
    """
    path = Path(campaign.path)
    vcs = vcs_of(campaign)
    pack = any(t.base is None for t in transfers)
    try:
        if path.exists():
            repo = vcs.open(path)
        elif campaign.clone_url is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            repo = vcs.clone(campaign.clone_url, path, credential)
        elif pack:
            repo = vcs.replica(path)
        else:
            raise RebuildFailed(f'campaign {campaign.name}: no URL or pack to make {path} from')
    except VcsError as e:
        raise RebuildFailed(f'campaign {campaign.name}: {path} could not be made again: {e}') from e
    applied = 0
    with closing(repo):
        for t in transfers:
            if not _has(repo, t.head):
                try:
                    if t.base is None:
                        repo.fetch(cas, [t.head], partial(_pack, t.blob))
                    else:
                        repo.receive(cas, Transfer(blob=t.blob, head=t.head), t.base)
                except (VcsError, Missing) as e:
                    raise RebuildFailed(f'campaign {campaign.name}: transfer {t.id} ({t.head}): {e}') from e
                applied += 1
            repo.keep(t.head, Pin(campaign.name, f'received/{t.id}'))
    return applied


def _pack(blob: Cid, _missing: Sequence[Revision], _have: Sequence[Revision]) -> list[Cid]:
    """What a repo made from a pack fetches: the pack, whatever it lacks and has."""
    return [blob]


def _has(repo: Repository, revision: str) -> bool:
    try:
        repo.version(revision)
    except UnknownRevision:
        return False
    return True


def _workspace(cas: Cas, manifest: Manifest, root: Path) -> None:
    """Make ``root`` hold the manifest's files and nothing else: written beside it, then moved in its place."""
    staged = root.with_name(f'{root.name}.rebuilding')
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    materialise(cas, manifest, staged)
    if root.exists():
        shutil.rmtree(root)
    os.replace(staged, root)
