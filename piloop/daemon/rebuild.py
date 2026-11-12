"""Make a home's files again from its database's rows and the CAS, after a restore on another host.

The database came from the term before's replica (``piloop.plugins.leader``). The files of its home did not: each
campaign's repo of record, its shared workspace, and the files that each job's reports put under its place. Their rows
name them in the CAS: ``RepoTransfer``, ``WorkspaceSnapshot`` and ``JobFiles``. Each step is idempotent: a home that is
already partly there, on the host that served it before, only gets what it lacks.
"""

import os
import shutil
from contextlib import closing
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import undefer

from piloop.agents.results import JOB_FILES
from piloop.campaign.campaigns import cloned, vcs_of, workspace_dir
from piloop.db.models import Campaign, JobFiles, RepoTransfer, WorkspaceSnapshot
from piloop.db.session import Database
from piloop.ids import CampaignStatus
from piloop.otel import Tracer
from piloop.plugins.cas.contract import Cas
from piloop.plugins.vcs.contract import Pin, Repository, Transfer, UnknownRevision, VcsError
from piloop.store.tree import Manifest, Missing, materialise, write_files


class RebuildFailed(Exception):
    """A part of the home could not be made again: the daemon does not start, and says which."""


def rebuild(db: Database, cas: Cas, home: Path, tracer: Tracer) -> None:
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
                received += _repo(cas, campaign, [t for t in transfers if t.campaign_id == campaign.id])
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


def _repo(cas: Cas, campaign: Campaign, transfers: list[RepoTransfer]) -> int:
    """Clone the campaign's repo where it was, unless it is there, and apply what it received that it lacks."""
    path = Path(campaign.path)
    vcs = vcs_of(campaign)
    try:
        if path.exists():
            repo = vcs.open(path)
        else:
            if campaign.repo is None:
                raise RebuildFailed(f'campaign {campaign.name}: no URL to clone {path} from')
            path.parent.mkdir(parents=True, exist_ok=True)
            repo = vcs.clone(campaign.repo, path, None)
    except VcsError as e:
        raise RebuildFailed(f'campaign {campaign.name}: {path} could not be made again: {e}') from e
    applied = 0
    with closing(repo):
        for t in transfers:
            if not _has(repo, t.head):
                try:
                    repo.receive(cas, Transfer(blob=t.blob, head=t.head), t.base)
                except (VcsError, Missing) as e:
                    raise RebuildFailed(f'campaign {campaign.name}: transfer {t.id} ({t.head}): {e}') from e
                applied += 1
            repo.keep(t.head, Pin(campaign.name, f'received/{t.id}'))
    return applied


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
