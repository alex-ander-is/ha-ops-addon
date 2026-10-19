"""Owned Linux/browser relay fixture. No production route or live HA access."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import uuid

sys.path[:0]=[str(Path(__file__).resolve().parents[2]/'app'),str(Path(__file__).resolve().parents[1])]
from test_native_apply import rig
from test_native_apply_integration import ProcessLifecycle
from direct_access import DirectServer,create_handler
from direct_auth import Authority
from native_status import NativeStatus


def main():
    output=Path(sys.argv[1]);nonce=sys.argv[2];origin=sys.argv[3]
    with tempfile.TemporaryDirectory(prefix='native-apply-browser-') as owned:
        root=Path(owned);r,w,review=rig(root);adapter=ProcessLifecycle(root/'processes')
        prepared=w.prepare(review,str(uuid.uuid4()))
        (root/'authorization').mkdir();authority=Authority(root/'authorization')
        status=NativeStatus(w.operations,w.services,w.recovery)
        server=DirectServer(('127.0.0.1',0),create_handler(authority,status,configured_origin=origin,readiness=w.direct))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=authority.issue(authority.nonce(()),(),origin)
        def pause(phase):
            if phase!='ack_durable:replace':return
            (output/('core-stopped-'+nonce+'.json')).write_text(json.dumps(dict(operation=prepared.operation,
                core_running=adapter._running('core'),app_running=adapter._running('app:on'))))
            deadline=time.monotonic()+60
            while not (output/('release-'+nonce)).exists():
                if time.monotonic()>=deadline:raise RuntimeError('Browser fixture release timed out')
                time.sleep(.02)
        w.recovery.fault=pause
        def work():
            try:w.run(prepared,adapter,lambda:{'backups':[]},seconds=90)
            except Exception:pass
            try:result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True,seconds=90)
            except Exception as error:result=dict(status='ERROR',error=type(error).__name__)
            (output/('result-'+nonce+'.json')).write_text(json.dumps(dict(result=result,receipts=adapter.value['receipts'])))
        worker=threading.Thread(target=work,daemon=True);worker.start()
        (output/('browser-ready-'+nonce+'.json')).write_text(json.dumps(dict(nonce=nonce,url=url,
            operation=prepared.operation,listener_port=server.server_port,hostname=socket.gethostname(),source='real Linux NativeApply participants')))
        try:
            deadline=time.monotonic()+160
            while not (output/('stop-'+nonce)).exists() and time.monotonic()<deadline:time.sleep(.05)
        finally:
            (output/('release-'+nonce)).touch();worker.join(10)
            server.shutdown();server.server_close();thread.join();adapter.cleanup()
            if worker.is_alive():raise RuntimeError('Owned worker failed to stop')


if __name__=='__main__':main()
