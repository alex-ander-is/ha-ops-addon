"""Explicit disposition of observed crash leaves; never inferred ownership."""
import dataclasses
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import uuid

import pytest

from test_native_git import repo, ids
import native_git as n
from native_operations import InvalidState


def debt(adapter, *, links=False):
    binding=ids()
    with pytest.raises(n.CleanupDebt):
        with adapter._resource('transport',**binding) as handle:
            root=Path(os.fsdecode(adapter._resource_path(handle.resource_id)))
            (root/'objects/pack').mkdir(parents=True)
            (root/'refs').mkdir()
            pack=root/'objects/pack'/('tmp_pack_Ab1234' if links else 'pack-'+ 'a'*40+'.pack')
            pack.write_bytes(b'unacknowledged pack bytes')
            (root/'objects/pack'/('pack-'+ 'a'*40+'.idx')).write_bytes(b'unacknowledged index bytes')
            if links:os.link(pack,pack.parent/('pack-'+ 'a'*40+'.pack'))
    return handle.resource_id,binding,root


def review(adapter,rid,binding):return adapter.review_transport_disposition(rid,**binding)
def approve(adapter,grant):return adapter.discard_reviewed_transport(grant,command_id=str(uuid.uuid4()))


@pytest.mark.parametrize('links',[False,True])
def test_unknown_leaves_require_explicit_fresh_review_and_closed_links(repo,links):
    _,adapter=repo;rid,binding,root=debt(adapter,links=links)
    assert adapter.recover_resource(rid,**binding).state=='debt'
    grant=review(adapter,rid,binding)
    assert len(grant.observed_entries)>=6
    assert approve(adapter,grant).state=='removed'
    assert not root.exists() and adapter.resources()==()
    with pytest.raises(n.InvalidInput):approve(adapter,grant)


@pytest.mark.parametrize('change',['bytes','inode','new','outside-link','marker','root','lease'])
def test_review_refuses_current_membership_or_anchor_mutation(repo,change):
    _,adapter=repo;rid,binding,root=debt(adapter)
    grant=review(adapter,rid,binding)
    pack=next((root/'objects/pack').glob('*.pack'))
    if change=='bytes':pack.write_bytes(b'foreign changed')
    elif change=='inode':
        temp=pack.with_name('replacement');temp.write_bytes(pack.read_bytes());os.replace(temp,pack)
    elif change=='new':(root/'objects/pack/tmp_idx_Fo1234').write_bytes(b'foreign')
    elif change=='outside-link':os.link(pack,root.parent/'outside')
    elif change=='marker':(root/'owner.json').write_bytes(b'foreign marker')
    elif change=='root':
        root.rename(root.with_name(root.name+'-saved'));root.mkdir();(root/'foreign').write_bytes(b'foreign')
    elif change=='lease':
        lease=Path(os.fsdecode(adapter._lease_path(rid)));lease.rename(lease.with_suffix('.saved'));lease.write_bytes(b'foreign')
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises((n.InvalidInput,InvalidState,OSError)):approve(adapter,grant)
    assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_copied_foreign_mutated_review_and_live_writer_refused(repo):
    root,adapter=repo;rid,binding,scratch=debt(adapter);grant=review(adapter,rid,binding)
    with pytest.raises(n.InvalidInput):approve(adapter,dataclasses.replace(grant))
    with pytest.raises(n.InvalidInput):approve(n.NativeGit(root),grant)
    detached=grant.observed_entries;detached.clear()
    lease=os.open(adapter._lease_path(rid),os.O_RDWR)
    try:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(n.InvalidInput,match='busy'):review(adapter,rid,binding)
        with pytest.raises(n.InvalidInput,match='busy'):approve(adapter,grant)
    finally:os.close(lease)
    assert approve(adapter,review(adapter,rid,binding)).state=='removed'


@pytest.mark.parametrize('point',['before-receipt','after-receipt','after-unlink','after-marker','after-root'])
def test_disposition_durable_receipt_and_unlink_crash_reopen(repo,monkeypatch,point):
    repo_root,adapter=repo;rid,binding,root=debt(adapter,links=True);grant=review(adapter,rid,binding)
    publish=adapter._publish;unlink=os.unlink;rmdir=os.rmdir;fired=[]
    def save(journal):
        receipt=journal['records'].get(rid,{}).get('disposition')
        if receipt and not fired and point=='before-receipt':fired.append(True);raise KeyboardInterrupt(point)
        result=publish(journal)
        if receipt and not fired and point=='after-receipt':fired.append(True);raise KeyboardInterrupt(point)
        return result
    def remove(path,*args,**kwargs):
        result=unlink(path,*args,**kwargs)
        name=os.fsdecode(path)
        selected=point=='after-unlink' and 'pack_' in name or point=='after-marker' and name=='owner.json'
        if selected and not fired:fired.append(True);raise KeyboardInterrupt(point)
        return result
    def directory(path,*args,**kwargs):
        result=rmdir(path,*args,**kwargs)
        if point=='after-root' and os.fsencode(path)==os.fsencode(root) and not fired:fired.append(True);raise KeyboardInterrupt(point)
        return result
    with monkeypatch.context() as changes:
        changes.setattr(adapter,'_publish',save);changes.setattr(os,'unlink',remove);changes.setattr(os,'rmdir',directory)
        with pytest.raises(KeyboardInterrupt):approve(adapter,grant)
    assert fired
    reopened=n.NativeGit(repo_root)
    if point=='before-receipt':
        assert reopened.recover_resource(rid,**binding).state=='debt'
        assert approve(reopened,review(reopened,rid,binding)).state=='removed'
    else:
        with pytest.raises(n.InvalidInput,match='already-authorized'):review(reopened,rid,binding)
        assert reopened.recover_resource(rid,**binding).state=='removed'
    assert not root.exists() and reopened.resources()==()


def test_durable_authorization_never_deletes_reoccupied_leaf(repo,monkeypatch):
    repo_root,adapter=repo;rid,binding,root=debt(adapter);grant=review(adapter,rid,binding)
    original=adapter._publish
    def halt(journal):
        original(journal)
        if journal['records'].get(rid,{}).get('disposition'):raise KeyboardInterrupt('receipt durable')
    with monkeypatch.context() as changes:
        changes.setattr(adapter,'_publish',halt)
        with pytest.raises(KeyboardInterrupt):approve(adapter,grant)
    pack=next((root/'objects/pack').glob('*.pack'));pack.unlink();pack.write_bytes(b'foreign reoccupation')
    outcome=n.NativeGit(repo_root).recover_resource(rid,**binding)
    assert outcome.state=='debt' and pack.read_bytes()==b'foreign reoccupation'


@pytest.mark.parametrize('change',['entries','command','operation','owner','root','lease','digest'])
def test_persisted_disposition_integrity_refuses_before_any_cleanup(repo,monkeypatch,change):
    repo_root,adapter=repo;rid,binding,root=debt(adapter);grant=review(adapter,rid,binding)
    publish=adapter._publish
    def halt(journal):
        publish(journal)
        if journal['records'].get(rid,{}).get('disposition'):
            raise KeyboardInterrupt('durable receipt before cleanup')
    with monkeypatch.context() as changes:
        changes.setattr(adapter,'_publish',halt)
        with pytest.raises(KeyboardInterrupt):approve(adapter,grant)
    manifest=Path(os.fsdecode(adapter.resources_root))/ 'journal.json'
    journal=json.loads(manifest.read_bytes());record=journal['records'][rid]
    receipt=record['disposition']
    if change=='entries':
        pack=next((root/'objects/pack').glob('*.pack'))
        pack.write_bytes(b'changed bytes outside the approved observation')
        receipt['entries'][n._encode(os.fsencode(pack.relative_to(root)))][3]=hashlib.sha256(pack.read_bytes()).hexdigest()
    elif change=='command':receipt['command_id']=str(uuid.uuid4())
    elif change in ('operation','owner'):record[change]=str(uuid.uuid4())
    elif change in ('root','lease'):record[change][1]+=1
    else:receipt['review_digest']='0'*64
    manifest.write_text(json.dumps(journal))
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(n.InvalidInput,match='disposition-integrity'):
        n.NativeGit(repo_root).recover_resource(rid,**binding)
    assert root.exists()
    assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_orphan_writer_group_blocks_review_until_verified_absent(repo):
    _,adapter=repo;rid,binding,root=debt(adapter)
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
    try:
        with adapter._journal() as journal:
            journal['records'][rid]['errors'].append('unreaped-network-group:'+str(process.pid))
            adapter._publish(journal)
        with pytest.raises(n.InvalidInput,match='writer-busy'):review(adapter,rid,binding)
        assert adapter.recover_resource(rid,**binding).state=='debt'
    finally:
        os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
    assert approve(adapter,review(adapter,rid,binding)).state=='removed'


def test_unreaped_group_failure_never_acknowledges_unknown_leaves_or_suppresses_error(repo):
    _,adapter=repo;binding=ids()
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
    try:
        with pytest.raises(RuntimeError,match='failed termination'):
            with adapter._resource('transport',**binding) as handle:
                root=Path(os.fsdecode(adapter._resource_path(handle.resource_id)))
                (root/'objects/pack').mkdir(parents=True)
                with adapter._capture_after(handle):
                    (root/'objects/pack/tmp_pack_Ab1234').write_bytes(b'unknown still potentially written')
                    error=RuntimeError('failed termination');error.unreaped_network_group=process.pid;raise error
        with adapter._journal() as journal:
            row=journal['records'][handle.resource_id]
            assert row['phase']=='debt' and len(row['entries'])==1
        with pytest.raises(n.InvalidInput,match='writer-busy'):review(adapter,handle.resource_id,binding)
    finally:
        os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
    assert approve(adapter,review(adapter,handle.resource_id,binding)).state=='removed'
