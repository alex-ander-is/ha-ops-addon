"""Opt-in actual Apply composition qualification; no live systems."""
import errno,json,os,sys,uuid,subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest import mock
import pytest
sys.path[:0]=[str(Path.cwd()/'ha-ops/tests'),str(Path.cwd()/'ha-ops/app')]
from test_native_apply import rig,browser_receipt,A
from test_native_apply_integration import Lifecycle,ProcessLifecycle
from test_native_ext4 import observe

pytestmark=pytest.mark.skipif(os.environ.get('HA_OPS_EXT4_ROOT')!='/workspace/ext4' or os.environ.get('HA_OPS_EXT4_PROFILE')!='allocation' or os.environ.get('HA_OPS_EXT4_ATTEMPT')!='0',reason='requires isolated opt-in allocation ext4 profile')
ROOT=Path('/workspace/ext4')
def target(name):
 p=ROOT/name;p.mkdir();return p

@pytest.mark.parametrize('scenario',['plain','merge','merge-compensation'])
def test_actual_apply_physical_peak_and_original_reserve(scenario):
 p=target('apply-whole-peak-'+scenario);r,w,review=rig(p)
 adapter=Lifecycle();expected=b'git\n';status='SUCCESS'
 if scenario!='plain':
  from dataclasses import replace
  from test_native_save import reviewed,B
  from native_topology_helpers import git,METADATA
  base=b'first\n'+b'unchanged\n'*10000+b'last\n'
  oid=r.commit({A:base,B:b'base-b\n'});git(r.root,'update-ref',r.source.base_ref,oid);r.source=replace(r.source,base_oid=oid)
  r.remote({A:base.replace(b'first\n',b'git first\n'),B:b'base-b\n'})
  subprocess.run(['git','--git-dir='+str(r.root/METADATA),'push',os.fsdecode(w.transport.endpoint),r.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
  r.live({A:base,B:b'base-b\n'});review=reviewed(r,(A,),direction='apply')
  class Changed(Lifecycle):
   def command(self,pending,**kwargs):
    result=super().command(pending,**kwargs)
    if pending['kind']=='stop' and pending['target']=='core' and not getattr(self,'edited',False):
     self.edited=True;(r.root/os.fsdecode(A)).write_bytes(base.replace(b'last\n',b'live last\n'))
    return result
  adapter=Changed();status='WARNING';expected=base.replace(b'first\n',b'git first\n').replace(b'last\n',b'live last\n')
  if scenario=='merge-compensation':
   adapter.health['core']=dict(target='core',status='unhealthy',recovery_mode=True);status='ERROR';expected=base.replace(b'last\n',b'live last\n')
 baseline=observe(r.root);samples=[];forecasts=[]
 def sample(stage):
  samples.append((stage,observe(r.root)))
  if stage=='slot_durable':forecasts.append(w.recovery.discover()['manifest']['forecast'])
 w.recovery.fault=sample
 with mock.patch.object(w.disk,'snapshot',wraps=w.disk.snapshot) as scans:
  prepared=w.prepare(review,str(uuid.uuid4()))
  with browser_receipt(w,prepared,p):
   with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
   result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
  assert scans.call_count==1
 assert result['status']==status,result
 assert (r.root/os.fsdecode(A)).read_bytes()==expected
 f=forecasts[-1];bound=sum(v[1] for v in f['volumes']);ibound=sum(v[3] for v in f['volumes'])
 baseblocks=sum(x['blocks'] for x in baseline['entries'].values())
 peak=max(max(0,sum(x['blocks'] for x in sample['entries'].values())-baseblocks,baseline['free_bytes']-sample['free_bytes']) for _,sample in samples)
 ipeak=max(max(len(set(sample['entries'])-set(baseline['entries'])),baseline['free_inodes']-sample['free_inodes']) for _,sample in samples)
 assert 0<peak<=bound,(peak,bound)
 assert ipeak<=ibound,(ipeak,ibound)
 for _,sample in samples:assert sample['free_bytes']>=min(v[2] for v in f['volumes'])
 print('APPLY_PHYSICAL '+json.dumps(dict(scenario=scenario,peak=peak,bound=bound,inodes=ipeak,inode_bound=ibound,samples=len(samples),original_capacity_snapshots=scans.call_count)))

@pytest.mark.parametrize('kind',['ENOSPC','EIO'])
@pytest.mark.parametrize('boundary',['generation_created','stage_durable','before_intent:replace','intent_durable:replace','effect:replace'])
def test_apply_effect_before_ack_actual_storage_fault(kind,boundary):
 p=target('apply-storage-'+kind+'-'+boundary.replace(':','-'));r,w,review=rig(p);adapter=Lifecycle();prepared=w.prepare(review,str(uuid.uuid4()))
 filler=p/'owned-filler';reached=[]
 def fault(stage):
  if stage!=boundary or reached:return
  reached.append(stage)
  if kind=='EIO':raise OSError(errno.EIO,'injected exact Apply effect/ACK boundary')
  with filler.open('wb',buffering=0) as out:
   for _ in range(256*1024*1024//4096):
    try:out.write(b'F'*4096)
    except OSError as error:
     assert error.errno==errno.ENOSPC;return
  raise AssertionError('bounded ext4 did not reach kernel ENOSPC')
 w.recovery.fault=fault
 try:
  with (nullcontext() if boundary in ('generation_created','stage_durable') else browser_receipt(w,prepared,p)):
   with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
   result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
  assert reached and result['status']=='ERROR',result
 finally:
  if filler.exists():filler.unlink()
 w.recovery.fault=lambda stage:None
 point=w.recovery.discover()
 if point is None:
  approval=w.review_cleanup(prepared.operation,str(uuid.uuid4()));closed=w.close_recovery(approval)
  assert closed['no_effect'] is True and (r.root/os.fsdecode(A)).read_bytes()==b'base\n'
  print('APPLY_STORAGE_FAULT '+json.dumps(dict(kind=kind,boundary=boundary,explicit_no_effect_close=True)))
  return
 before=list(adapter.calls)
 for _ in range(2):assert w.recovery.discover() is not None
 assert adapter.calls==before
 w.recovery.reconcile(operation_id=prepared.operation,owner_id=w.operations.owner)
 w.recovery.rollback(operation_id=prepared.operation,owner_id=w.operations.owner)
 assert (r.root/os.fsdecode(A)).read_bytes()==b'base\n'
 state=w.services.discover()
 w.services.restore(state['operation'],state['owner'],state['revision'],adapter)
 assert w.recovery.discover() is not None
 print('APPLY_STORAGE_FAULT '+json.dumps(dict(kind=kind,result=result,explicit_rollback_exact=True)))

def crash_child(directory,boundary):
 p=Path(directory);r,w,review=rig(p);adapter=ProcessLifecycle(p/'processes');prepared=w.prepare(review,str(uuid.uuid4()))
 old_checkpoint=w._checkpoint
 def checkpoint(worker,phase,*args,**kwargs):
  value=old_checkpoint(worker,phase,*args,**kwargs)
  if boundary=='completed' and phase=='completed':os._exit(94)
  return value
 w._checkpoint=checkpoint
 old_claim=w.operations.claim
 def claimed(*args,**kwargs):
  value=old_claim(*args,**kwargs)
  if boundary=='claim':os._exit(94)
  return value
 w.operations.claim=claimed
 for name,stage in [('install','install'),('retire','retire')]:
  old=getattr(w.recovery,name)
  def hooked(*args,_old=old,_stage=stage,**kwargs):
   value=_old(*args,**kwargs)
   if boundary==_stage:os._exit(94)
   return value
  setattr(w.recovery,name,hooked)
 old_command=adapter.command
 def command(pending,**kwargs):
  value=old_command(pending,**kwargs)
  if boundary=='stopped' and pending['target']=='core' and pending['kind']=='stop':os._exit(94)
  return value
 adapter.command=command
 w.recovery.fault=lambda phase:os._exit(94) if boundary=='effect' and phase=='effect:replace' else None
 old_cleanup=w._cleanup_spools
 def cleanup(*args,**kwargs):
  value=old_cleanup(*args,**kwargs)
  if boundary=='cleanup':os._exit(94)
  return value
 w._cleanup_spools=cleanup
 old_result=w.operations.apply_cleanup_result
 def result(*args,**kwargs):
  value=old_result(*args,**kwargs)
  if boundary=='result':os._exit(94)
  return value
 w.operations.apply_cleanup_result=result
 with browser_receipt(w,prepared,p):
  try:w.run(prepared,adapter,lambda:{'backups':[]})
  except Exception:pass
  outcome=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
 raise AssertionError(('unreached crash boundary',boundary,outcome))

@pytest.mark.parametrize('boundary',['claim','install','stopped','effect','completed','retire','cleanup','result'])
def test_actual_apply_cross_store_process_interruption_is_observational(boundary):
 p=target('apply-child-'+boundary)
 script='import sys;from test_native_apply_ext4 import crash_child;crash_child(sys.argv[1],sys.argv[2])'
 env=dict(os.environ,PYTHONPATH=os.pathsep.join([str(Path(__file__).parent),str(Path.cwd()/'ha-ops/tests'),str(Path.cwd()/'ha-ops/app')]))
 child=subprocess.run([sys.executable,'-c',script,str(p),boundary],capture_output=True,timeout=45,env=env)
 assert child.returncode==94,(boundary,child.returncode,child.stdout,child.stderr)
 from test_native_preview import bindings
 from native_inventory import Policy
 from native_metadata import MANIFEST_PATH
 from native_recovery import RecoveryStore
 from native_services import ServiceStore
 from native_operations import NativeOperationStore
 root=p/'live';rec=RecoveryStore(bindings(root,Policy(repository_files=(MANIFEST_PATH,))))
 svc=ServiceStore(root/'data/native-services');ops=NativeOperationStore(root/'data')
 adapter=ProcessLifecycle(p/'processes')
 import hashlib
 def documents():
  return {str(q.relative_to(root)):hashlib.sha256(q.read_bytes()).hexdigest() for d in ('native-operations','native-services','native/recovery') for q in (root/'data'/d).rglob('*') if q.is_file()}
 try:
  before=documents();receipts=list(adapter.value['receipts'])
  for _ in range(3):rec.discover();svc.discover();ops.read(observe=True)
  assert documents()==before and adapter.value['receipts']==receipts
  point=rec.discover();state=ops.read(observe=True)
  assert bool(point)==(boundary in ('install','stopped','effect','completed'))
  if boundary=='result':assert list(state['workers'].values())[-1]['apply_result']['status']=='SUCCESS' and state['recovery'] is None
  print('APPLY_CRASH '+json.dumps(dict(boundary=boundary,exit=child.returncode,point_retained=bool(point),reopen_observational=True)))
 finally:adapter.cleanup()
