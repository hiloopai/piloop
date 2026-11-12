"""A home made again from its database's rows and the CAS: the repo of record from its clone and its transfers, the
workspace from its newest snapshot, a job's place from its files. What another host does after a restore."""

import os
import shutil
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from covalence import Cid
from git import Repo
from sqlalchemy import delete, update

from piloop.actor import System
from piloop.agents.results import JOB_FILES
from piloop.campaign.campaigns import FromUrl, register_clone, workspace_dir
from piloop.config import load_campaign_config
from piloop.daemon.rebuild import RebuildFailed, rebuild
from piloop.db.models import Campaign, JobFiles, RepoTransfer, WorkspaceSnapshot
from piloop.db.session import Database
from piloop.ids import CampaignId, Revision
from piloop.otel import Tracer
from piloop.plugins.vcs.git import Git
from piloop.store.tree import put_tree
from tests.conftest import disk


def test_a_home_whose_files_are_gone_is_made_again_from_its_rows_and_the_cas_twice_alike(
    db: Database, campaign_path: Path, tmp_path: Path
) -> None:
    home, cas = tmp_path / 'home', disk(tmp_path / 'cas')
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'clone', '-q', '--bare', str(campaign_path), str(origin)], check=True)
    with db.session() as s:
        campaign, _ = register_clone(s, home, Git(), FromUrl(origin.as_uri(), None), System.HUMAN)
        s.commit()
        checkout, campaign_id = Path(campaign.path), campaign.id
    base = Repo(checkout).head.commit.hexsha
    work = tmp_path / 'work'  # an implementer's commit, which only the repo of record received
    subprocess.run(['git', 'clone', '-q', str(checkout), str(work)], check=True)
    (work / 'solve.txt').write_text('base\nbetter\n')
    Repo(work).git.commit('-qam', 'better')
    head = Revision(Repo(work).head.commit.hexsha)
    with closing(Git().open(work)) as repo:
        blob = repo.send(cas, [head], [Revision(base)])
    assert blob is not None
    workspace = tmp_path / 'snapshot'
    (workspace / 'idea1').mkdir(parents=True)
    (workspace / 'idea1' / 'notes.md').write_text('tried lzma\n')
    manifest = put_tree(cas, workspace, os.listdir(workspace)).manifest.put(cas)
    files = cas.put(JOB_FILES.dump_json({'transcript.txt': cas.put(b'hello\n')}))
    with db.session() as s:
        s.add(RepoTransfer(campaign_id=campaign_id, base=base, head=head, blob=blob))
        s.add(WorkspaceSnapshot(campaign_id=campaign_id, manifest=manifest))
        s.add(JobFiles(place='runs/1', files=files))
        s.commit()
    shutil.rmtree(checkout)  # the disk of another host: none of the home's files
    stale = workspace_dir(home, campaign_id)
    stale.mkdir(parents=True)
    (stale / 'gone.txt').write_text('a file that a later merge removed\n')
    for _ in range(2):
        rebuild(db, cas, home, Tracer(None))
        with Repo(checkout) as repo:
            assert repo.commit(head).message.strip() == 'better', 'the transfer it received'
            assert any(r.path == 'refs/piloop/demo/received/1' for r in repo.refs), 'kept from garbage collection'
        assert sorted(p.relative_to(stale).as_posix() for p in stale.rglob('*') if p.is_file()) == ['idea1/notes.md']
        assert (home / 'runs' / '1' / 'transcript.txt').read_text() == 'hello\n'


def test_a_row_whose_blob_the_cas_lacks_fails_the_rebuild_and_says_which(
    db: Database, campaign_path: Path, tmp_path: Path
) -> None:
    home, cas = tmp_path / 'home', disk(tmp_path / 'cas')
    with db.session() as s:
        s.add(JobFiles(place='runs/7', files=Cid('bafkr4ihkr4ld3m4gqkjf4reryxsy2s5tkbxprqkow6fin2iiyvreuzzab4')))
        s.commit()
    with pytest.raises(RebuildFailed, match='the files of runs/7 .* are not in the CAS'):
        rebuild(db, cas, home, Tracer(None))


def test_what_cannot_be_made_again_fails_the_rebuild_and_says_why(
    db: Database, campaign_path: Path, tmp_path: Path
) -> None:
    """A campaign with no URL, an origin that is gone, a transfer that the CAS lacks, a job file that it lacks."""
    home, cas = tmp_path / 'home', disk(tmp_path / 'cas')
    checkout = home / 'repos' / '1'
    with db.session() as s:
        s.add(Campaign(name='lost', path=str(checkout), config=load_campaign_config(campaign_path)))
        s.commit()
    with pytest.raises(RebuildFailed, match=f'campaign lost: no URL to clone {checkout} from'):
        rebuild(db, cas, home, Tracer(None))
    with db.session() as s:
        s.execute(update(Campaign).values(repo=(tmp_path / 'gone.git').as_uri()))
        s.commit()
    with pytest.raises(RebuildFailed, match=f'campaign lost: {checkout} could not be made again'):
        rebuild(db, cas, home, Tracer(None))
    Repo.init(checkout).git.commit('-q', '--allow-empty', '-m', 'base')
    base = Repo(checkout).head.commit.hexsha
    with db.session() as s:
        s.add(RepoTransfer(campaign_id=1, base=base, head='f' * 40, blob=disk(tmp_path / 'other').put(b'x')))
        s.commit()
    with pytest.raises(RebuildFailed, match=f'campaign lost: transfer 1 \\({"f" * 40}\\)'):
        rebuild(db, cas, home, Tracer(None))
    with db.session() as s:
        s.execute(delete(RepoTransfer))
        s.add(JobFiles(place='runs/2', files=cas.put(JOB_FILES.dump_json({'log.txt': cas.put(b'x')}))))
        s.add(
            JobFiles(
                place='runs/3', files=cas.put(JOB_FILES.dump_json({'log.txt': disk(tmp_path / 'other').put(b'y')}))
            )
        )
        s.commit()
    with pytest.raises(RebuildFailed, match='^runs/3: the blob'):
        rebuild(db, cas, home, Tracer(None))
    assert (home / 'runs' / '2' / 'log.txt').read_bytes() == b'x', 'what it could make, it made'


def test_a_campaign_with_a_checkout_of_its_own_or_no_snapshot_keeps_what_it_has(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """Under ``local``, nothing restores; the rebuild still leaves a checkout of one's own and an unknown workspace."""
    home = tmp_path / 'home'
    (workspace_dir(home, campaign.id) / 'notes.md').parent.mkdir(parents=True)
    (workspace_dir(home, campaign.id) / 'notes.md').write_text('kept\\n')
    rebuild(db, disk(tmp_path / 'cas'), home, Tracer(None))
    assert (workspace_dir(home, campaign.id) / 'notes.md').read_text() == 'kept\\n'
    assert (Path(campaign.path) / 'piloop.toml').is_file()


def test_a_workspace_rebuilt_where_none_was_and_after_a_rebuild_that_stopped_halfway(
    db: Database, campaign_path: Path, tmp_path: Path
) -> None:
    home, cas = tmp_path / 'home', disk(tmp_path / 'cas')
    files = tmp_path / 'files'
    files.mkdir()
    (files / 'a.txt').write_text('a\\n')
    manifest = put_tree(cas, files, ['a.txt']).manifest.put(cas)
    with db.session() as s:
        s.add(Campaign(name='c', path=str(tmp_path / 'mine'), config=load_campaign_config(campaign_path)))
        s.flush()
        s.add(WorkspaceSnapshot(campaign_id=1, manifest=manifest))
        s.commit()
    root = workspace_dir(home, CampaignId(1))
    (root.with_name(f'{root.name}.rebuilding') / 'half').mkdir(parents=True)
    rebuild(db, cas, home, Tracer(None))
    assert [p.name for p in root.iterdir()] == ['a.txt'] and not root.with_name(f'{root.name}.rebuilding').exists()
