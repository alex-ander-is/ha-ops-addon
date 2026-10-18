"""Network-none Linux fixture: genuine journals, independent worker, file bridge."""
import hashlib
import json
import multiprocessing
import os
import re
from pathlib import Path
import signal
import sys
import tempfile
import time
sys.path[:0]=[str(Path(__file__).resolve().parents[2]/'app'),str(Path(__file__).resolve().parents[1])]
from test_native_recovery import point,write,file,store as recovery_store
from test_native_services import Adapter,fence
from native_services import ServiceStore
from native_status import NativeStatus


def atomic(path,value):
    temporary=path.with_name(path.name+'.pending')
    with temporary.open('w') as stream:
        json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def run(output,nonce,self_test=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',nonce):raise ValueError('Invalid fixture nonce')
    output=Path(output);process=None;started=False;restored=False
    with tempfile.TemporaryDirectory(prefix='ha-ops-linux-browser-') as temporary:
        root=Path(temporary)
        write(root,b'homeassistant/a',b'before')
        recovery,installed=point(root,{b'homeassistant/a':file(b'after')})
        services=ServiceStore(root/'data/native-services')
        status=NativeStatus(services=services,recovery=recovery)
        effects=output/('effects-'+nonce+'.json')
        atomic(effects,{'count':0,'service_commands':[],'states':Adapter().states})
        def worker():
            adapter=Adapter();service=ServiceStore(root/'data/native-services')
            original=adapter.command
            def command(pending,**kwargs):
                result=original(pending,**kwargs)
                atomic(effects,{'count':0,'service_commands':adapter.calls,'states':adapter.states})
                return result
            adapter.command=command
            state=service.prepare(adapter);service.stop(*fence(state),adapter)
            time.sleep(1)
            def fault(stage):
                if stage.startswith('effect_durable:'):
                    atomic(effects,{'count':1,'service_commands':adapter.calls,'states':adapter.states})
                    os._exit(93) # Actual durable effect before journal ACK.
            current=recovery_store(root);current.fault=fault;current.execute()
            os._exit(94)
        def stop(*_):
            if process is not None and process.is_alive():process.terminate();process.join(timeout=3)
            raise SystemExit(0)
        signal.signal(signal.SIGTERM,stop)
        ready=output/('bridge-ready-'+nonce+'.json')
        atomic(ready,{'nonce':nonce,'source':'genuine-linux-native-stores'})
        deadline=time.monotonic()+150
        if self_test:(output/('start-'+nonce)).touch()
        while time.monotonic()<deadline:
            if (output/('stop-'+nonce)).exists():break
            if not started and (output/('start-'+nonce)).exists():
                process=multiprocessing.get_context('fork').Process(target=worker);process.start();started=True
            if process is not None and process.exitcode is not None and not restored:
                process.join()
                state=services.discover();adapter=Adapter();adapter.states.update(json.loads(effects.read_text())['states'])
                adapter.health['core']=dict(target='core',status='unhealthy',recovery_mode=False)
                services.restore(*fence(state),adapter)
                current=json.loads(effects.read_text());current['service_commands']+=adapter.calls;atomic(effects,current)
                restored=True
            projection=status.read()
            current=json.loads(effects.read_text())
            projection['fixture_linux']=dict(nonce=nonce,worker_exit=None if process is None else process.exitcode,effect_count=current['count'],service_command_count=len(current['service_commands']),file_digest=hashlib.sha256((root/'homeassistant/a').read_bytes()).hexdigest())
            atomic(output/('projection-'+nonce+'.json'),projection)
            if self_test and restored:
                assert projection['recovery']['phase']=='forward' and projection['recovery']['pending_intent']
                assert projection['fixture_linux']['effect_count']==1 and process.exitcode==93
                assert any(e['id']=='core' and e['health']=='unhealthy' for e in projection['services']['entries'])
                print(json.dumps({'ok':True,'recovery':projection['recovery'],'fixture_linux':projection['fixture_linux'],'services':projection['services']}),flush=True)
                break
            time.sleep(.1)
        else:raise RuntimeError('Linux browser fixture timed out')
        if process is not None and process.is_alive():process.terminate();process.join(timeout=3)


if __name__=='__main__':run(sys.argv[1],sys.argv[2],len(sys.argv)>3 and sys.argv[3]=='--self-test')
