"""Real authenticated loopback qualification; never uses configured remotes.

This fixture qualifies the proposed packed command family independently of
the publication executor. Passing it alone does not qualify workflow admission.
"""
import json
import os
from pathlib import Path
import resource
import ctypes
import struct
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

import pytest
from network_publication_helpers import receiver_control

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))

from network_publication_helpers import command,isolated_environment,ssh_server,tls_server


linux_network=pytest.mark.skipif(sys.platform!='linux' or not Path('/workspace/output').is_dir(),
    reason='Requires reusable isolated Linux runner with authenticated loopback fixture tools')


def repository(root,object_format,profile):
    work=root/'sender';receiver=root/'receiver.git'
    env=isolated_environment()
    command(['git','init','--object-format='+object_format,str(work)],env=env)
    command(['git','-C',str(work),'config','user.name','Network Fixture'],env=env)
    command(['git','-C',str(work),'config','user.email','fixture@invalid'],env=env)
    if profile=='many':
        for number in range(1500): (work/('tiny-%04d'%number)).write_bytes(str(number).encode())
    elif profile=='compressed': (work/'compressed').write_bytes(b'x'*(8*1024*1024))
    elif profile=='oversized': (work/'large').write_bytes(os.urandom(2*1024*1024))
    else: (work/'small').write_bytes(b'normal server without filters\n')
    command(['git','-C',str(work),'add','.'],env=env)
    command(['git','-C',str(work),'commit','-qm','isolated fixture'],env=env)
    tip=command(['git','-C',str(work),'rev-parse','HEAD'],env=env).stdout.strip().decode()
    if profile=='shallow':
        (work/'small').write_bytes(b'second commit leaves one missing ancestor\n')
        command(['git','-C',str(work),'add','.'],env=env)
        command(['git','-C',str(work),'commit','-qm','shallow child'],env=env)
        tip=command(['git','-C',str(work),'rev-parse','HEAD'],env=env).stdout.strip().decode()
        command(['git','clone','--bare','--depth=1',work.as_uri(),str(receiver)],env=env)
        command(['git','--git-dir='+str(receiver),'update-ref','refs/heads/main',tip],env=env)
    else:
        command(['git','init','--bare','--object-format='+object_format,str(receiver)],env=env)
        command(['git','-C',str(work),'push',str(receiver),tip+':refs/heads/main'],env=env)
    command(['git','--git-dir='+str(receiver),'config','http.receivepack','true'],env=env)
    return receiver,tip


def _inventory(root):
    rows=[]
    for path in root.rglob('*'):
        try:info=path.lstat()
        except FileNotFoundError:continue  # A concurrent Git rename/unlink is expected telemetry.
        if stat.S_ISREG(info.st_mode): rows.append((str(path.relative_to(root)),info.st_ino,info.st_size,info.st_blocks*512))
    return rows


def test_fetch_telemetry_tolerates_a_leaf_retired_before_stat(tmp_path,monkeypatch):
    stable=tmp_path/'stable';stable.write_bytes(b'kept')
    retired=tmp_path/'tmp_idx_retired';retired.write_bytes(b'temporary')
    lstat=Path.lstat;observed=[]
    def concurrent_unlink(path,*args,**kwargs):
        if path==retired and not observed:
            observed.append(True);path.unlink()
        return lstat(path,*args,**kwargs)
    monkeypatch.setattr(Path,'lstat',concurrent_unlink)
    rows=_inventory(tmp_path)
    assert observed and [row[0] for row in rows]==['stable']


def capped_fetch(scratch,url,env,cap,object_format):
    command(['git','init','--bare','--template=','--object-format='+object_format,str(scratch)],env=env)
    baseline=_inventory(scratch)
    libc=ctypes.CDLL(None,use_errno=True)
    events=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
    assert events>=0
    assert libc.inotify_add_watch(events,os.fsencode(scratch),0x100|0x80)>=0
    assert libc.inotify_add_watch(events,os.fsencode(scratch/'objects/pack'),0x100|0x80)>=0
    created=set()
    def drain():
        while True:
            try:data=os.read(events,65536)
            except BlockingIOError:return
            offset=0
            while offset<len(data):
                wd,mask,cookie,length=struct.unpack_from('iIII',data,offset);offset+=16
                name=data[offset:offset+length].split(b'\0',1)[0].decode();offset+=length
                created.add(name)
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE,(cap,cap))
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    argv=['git','--git-dir='+str(scratch)]
    for option in ('core.fsmonitor=false','gc.auto=0','maintenance.auto=false',
                   'fetch.writeCommitGraph=false','pack.writeReverseIndex=false',
                   'fetch.unpackLimit=0','transfer.unpackLimit=0','fetch.fsckObjects=true',
                   'fetch.uriprotocols=','protocol.version=0','http.followRedirects=false',
                   'http.sslVerify=true','http.proxy=','remote.origin.promisor=false'):
        argv+=['-c',option]
    argv+=['fetch','--keep','--no-tags','--no-write-fetch-head','--no-auto-maintenance',
           '--no-recurse-submodules','--no-progress',url,'refs/heads/main']
    process=subprocess.Popen(argv,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             start_new_session=True,preexec_fn=limits)
    observations=[]
    deadline=time.monotonic()+20
    try:
        while process.poll() is None:
            drain()
            observations.append(_inventory(scratch))
            if time.monotonic()>=deadline:
                raise TimeoutError('owned packed-fetch fixture deadline')
            time.sleep(.001)
        out,error=process.communicate(timeout=2)
        observations.append(_inventory(scratch))
        drain()
    finally:
        if process.poll() is None:
            os.killpg(process.pid,__import__('signal').SIGKILL)
            process.wait(timeout=5)
        for stream in (process.stdout,process.stderr): stream.close()
        os.close(events)
    return process.returncode,out,error,baseline,observations,created


@pytest.mark.parametrize('protocol',['ssh','https'])
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('profile',['small','many','compressed','oversized'])
@linux_network
def test_authenticated_packed_family_kernel_cap_and_independent_objects(tmp_path,protocol,object_format,profile):
    from native_runtime import qualify
    assert qualify() in ('debian-development-2.39.5','alpine-aarch64-2.54.0','alpine-x86_64-2.54.0')
    assert shutil.which('ssh-keygen')
    receiver,tip=repository(tmp_path,object_format,profile)
    cap=256*1024 if profile=='oversized' else 4*1024*1024
    context=ssh_server(tmp_path) if protocol=='ssh' else tls_server(tmp_path)
    with context as descriptor:
        url=descriptor[0];env=isolated_environment()
        with tempfile.TemporaryDirectory(prefix='native-network-auth-',dir='/workspace/output') as executable:
            if protocol=='ssh':
                _,key,known=descriptor
                env['GIT_SSH_COMMAND']='ssh -F /dev/null -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=%s -o BatchMode=yes'%(shlex.quote(str(key)),shlex.quote(str(known)))
            else:
                _,cert=descriptor
                askpass=Path(executable)/'askpass'
                askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf fixture;; *) printf fixture-password;; esac\n')
                askpass.chmod(0o700)
                env.update(GIT_ASKPASS=str(askpass),GIT_SSL_CAINFO=str(cert))
            scratch=tmp_path/'independent.git'
            code,out,error,baseline,observations,created=capped_fetch(scratch,url,env,cap,object_format)
    base_names={row[0] for row in baseline}
    packs=[]
    for rows in observations:
        new=[row for row in rows if row[0] not in base_names]
        assert all(row[2]<=cap for row in new),(profile,new)
        assert all(row[0].startswith('objects/pack/') for row in new),(profile,new)
        assert len(new)<=5 and len({row[1] for row in new})<=3,(profile,new)
        packs.append(new)
    telemetry=dict(protocol=protocol,object_format=object_format,profile=profile,cap=cap,
        returncode=code,peak_logical=max((sum(row[2] for row in rows) for rows in packs),default=0),
        observed_peak_files=max(map(len,packs),default=0),
        physical_peak=max((sum(row[3] for row in rows) for rows in packs),default=0),
        final_inventory=packs[-1])
    print('NETWORK_PACKED_FAMILY '+json.dumps(telemetry))
    if profile=='oversized':
        assert code!=0,'Kernel cap must interrupt incompressible pack before support is claimed'
        return
    assert code==0,error.decode(errors='replace')
    pack_dir=scratch/'objects/pack'
    assert len(list(pack_dir.glob('*.pack')))==1 and len(list(pack_dir.glob('*.idx')))==1
    assert not list(pack_dir.glob('*.rev')) and not list(pack_dir.glob('*.promisor'))
    assert not (scratch/'FETCH_HEAD').exists() and not (scratch/'shallow').exists()
    assert not [path for path in (scratch/'refs').rglob('*') if path.is_file()]
    raw=command(['git','--git-dir='+str(scratch),'cat-file','commit',tip],env=isolated_environment()).stdout
    import hashlib
    assert hashlib.new(object_format,b'commit '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==tip
    assert not (scratch/'objects/info/alternates').exists()


@pytest.mark.parametrize('protocol',['ssh','https'])
@linux_network
def test_loopback_fixture_refuses_missing_authentication(tmp_path,protocol):
    repository(tmp_path,'sha1','small')
    context=ssh_server(tmp_path) if protocol=='ssh' else tls_server(tmp_path)
    with context as descriptor:
        env=isolated_environment()
        if protocol=='ssh':
            env['GIT_SSH_COMMAND']='ssh -F /dev/null -o BatchMode=yes -o IdentityFile=none -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
        else: env['GIT_SSL_CAINFO']=str(descriptor[1])
        result=subprocess.run(['git','ls-remote',descriptor[0],'refs/heads/main'],env=env,
                              capture_output=True,timeout=10)
        assert result.returncode!=0 and not result.stdout


@pytest.mark.parametrize('protocol',['ssh','https'])
@linux_network
def test_remote_shallow_advertisement_has_separate_temporary_family_and_never_complete_negative(tmp_path,protocol):
    receiver,tip=repository(tmp_path,'sha1','shallow')
    context=ssh_server(tmp_path) if protocol=='ssh' else tls_server(tmp_path)
    with context as descriptor:
        url=descriptor[0];env=isolated_environment()
        with tempfile.TemporaryDirectory(prefix='native-network-shallow-auth-',dir='/workspace/output') as executable:
            if protocol=='ssh':
                _,key,known=descriptor
                env['GIT_SSH_COMMAND']='ssh -F /dev/null -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=%s -o BatchMode=yes'%(shlex.quote(str(key)),shlex.quote(str(known)))
            else:
                _,cert=descriptor
                askpass=Path(executable)/'askpass'
                askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf fixture;; *) printf fixture-password;; esac\n')
                askpass.chmod(0o700)
                env.update(GIT_ASKPASS=str(askpass),GIT_SSL_CAINFO=str(cert))
            scratch=tmp_path/'independent.git'
            code,out,error,baseline,observations,created=capped_fetch(scratch,url,env,1024*1024,'sha1')
    base_names={row[0] for row in baseline}
    extras={row[0] for rows in observations for row in rows if row[0] not in base_names}
    assert all(name.startswith('objects/pack/') or name.startswith('shallow_') or name=='shallow.lock' for name in extras),extras
    assert any(name.startswith('shallow_') for name in created),'Actual shallow temp allocation must be observed through kernel create events'
    raw=command(['git','--git-dir='+str(scratch),'cat-file','commit',tip],env=isolated_environment()).stdout
    parent=next(line[7:].decode() for line in raw.splitlines() if line.startswith(b'parent '))
    absent=subprocess.run(['git','--git-dir='+str(scratch),'cat-file','commit',parent],
                          env=isolated_environment(),capture_output=True,timeout=10)
    assert absent.returncode!=0,'Missing remote ancestry must stay unknown, never complete negative'
    print('NETWORK_SHALLOW_FAMILY '+json.dumps({'protocol':protocol,'returncode':code,'created_names':sorted(extras)}))


@pytest.mark.parametrize('mode',['manual','generated'])
@linux_network
def test_existing_ssh_authentication_sources_reach_real_receiver(tmp_path,mode):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import git_auth
    receiver,tip=repository(tmp_path,'sha1','small')
    with ssh_server(tmp_path) as (url,key,known):
        options={'git_ssh_key':key.read_text()} if mode=='manual' else {}
        source=git_auth.git_env(options,tmp_path/'existing-auth',key,key.with_suffix('.pub'))
        assert git_auth.git_auth_mode(options,key,key.with_suffix('.pub'))==mode
        env=isolated_environment()
        env['GIT_SSH_COMMAND']=source['GIT_SSH_COMMAND']+' -F /dev/null -o BatchMode=yes -o UserKnownHostsFile='+shlex.quote(str(known))
        observed=command(['git','ls-remote','--refs',url,'refs/heads/main'],env=env).stdout
        assert observed==tip.encode()+b'\trefs/heads/main\n'


@linux_network
def test_original_app_generate_deploy_key_and_legacy_ssh_authentication(tmp_path):
    import git_auth
    receiver, tip = repository(tmp_path, 'sha1', 'small')
    work = tmp_path / 'App generated key with spaces'
    key, public = work / 'deploy-key', work / 'deploy-key.pub'
    executed = []
    def run_command(argv):
        assert argv[0] == 'ssh-keygen' and shutil.which(argv[0]) == '/usr/bin/ssh-keygen'
        executed.append(tuple(argv))
        return command(argv, env=isolated_environment())
    result = git_auth.generate_deploy_key(work, key, public, run_command, lambda message: None)
    assert len(executed) == 1 and result == public.read_text().strip()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o644
    assert not (work / 'generated_deploy_key.new').exists() and not (work / 'generated_deploy_key.new.pub').exists()
    raw = key.read_bytes()
    assert raw.endswith(b'\n') and not raw.endswith(b'\n\n')
    parsed = command(['/usr/bin/ssh-keygen', '-y', '-f', str(key)]).stdout.split()[:2]
    assert parsed == public.read_bytes().split()[:2]
    with ssh_server(tmp_path) as (url, fixture_key, known):
        # The receiver authorizes the key produced by the original App. Its
        # independently generated fixture private key is never the client key.
        fixture_key.with_suffix('.pub').write_bytes(public.read_bytes())
        env = git_auth.git_env({}, work, key, public)
        env['GIT_SSH_COMMAND'] += ' -F /dev/null -o BatchMode=yes -o UserKnownHostsFile=' + shlex.quote(str(known))
        assert command(['/usr/bin/git', 'ls-remote', '--refs', url, 'refs/heads/main'], env=env).stdout == tip.encode() + b'\trefs/heads/main\n'


@pytest.mark.parametrize('url,scheme,host,port,path,user,relative',[
    ('https://name:secret%2Fvalue@EXAMPLE.com/owner/repository.git','https','example.com',443,'/owner/repository.git',None,False),
    ('ssh://git@example.com:2222/owner/repository.git','ssh','example.com',2222,'/owner/repository.git','git',False),
    ('git@example.com:owner/repository.git','ssh','example.com',22,'owner/repository.git','git',True),
    ('https://[::1]:8443/receiver.git','https','::1',8443,'/receiver.git',None,False)])
def test_network_route_captures_only_credential_free_fields(url,scheme,host,port,path,user,relative,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import native_publication as pub
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('route construction started a process'))
    options={'repo_url':url,'repo_branch':'main'}
    route=pub.NetworkRoute.from_options(options)
    assert route==pub.NetworkRoute(scheme,host,port,path,user,'refs/heads/main',relative)
    assert route.check(options) is route
    encoded=json.dumps(route.document())
    assert 'secret' not in encoded and 'name:' not in encoded


@pytest.mark.parametrize('url',[
    'http://example.com/repo.git','file:///repo.git','ext::git upload-pack repo',
    'ssh://git:password@example.com/repo.git','git@alias:repo.git',
    'ssh://git@example.com/repo.git;touch-other','https://example.com/a/../repo.git',
    'https://example.com/repo.git?route=other','https://example.com/repo.git#other',
    'ssh://git@example.com:70000/repo.git','ssh://git@example.com:0/repo.git',
    'ssh://example.com/repo.git','https://example.com/repo%2Fother.git',
    'https://example.com/repo.git\n','https://example.com/repo.git\\other'])
def test_unqualified_network_route_refuses_without_effect(url):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import native_publication as pub
    with pytest.raises(pub.InvalidState): pub.NetworkRoute.from_options({'repo_url':url,'repo_branch':'main'})


@pytest.mark.parametrize('changed',[
    {'repo_url':'ssh://git@other.example/owner/repo.git'},
    {'repo_url':'ssh://other@example.com/owner/repo.git'},
    {'repo_url':'ssh://git@example.com:2222/owner/repo.git'},
    {'repo_url':'ssh://git@example.com/other/repo.git'},
    {'repo_branch':'other'}])
def test_network_route_change_refuses_current_configuration(changed):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import native_publication as pub
    options={'repo_url':'ssh://git@example.com/owner/repo.git','repo_branch':'main'}
    route=pub.NetworkRoute.from_options(options)
    options.update(changed)
    with pytest.raises(pub.PublicationError,match='route changed'):route.check(options)


def test_same_https_route_allows_ephemeral_credential_rotation():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import native_publication as pub
    options={'repo_url':'https://name:old@example.com/repo.git','repo_branch':'main'}
    route=pub.NetworkRoute.from_options(options)
    options['repo_url']='https://name:new@example.com/repo.git'
    assert route.check(options) is route


def test_existing_manual_key_normalizes_one_newline_and_quotes_identity_path(tmp_path):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import git_auth
    work=tmp_path/'space and apostrophe\' key path'
    env=git_auth.setup_git_ssh_env({},work,key_text='PRIVATE\n\n')
    key=work/'manual_deploy_key'
    assert key.read_text()=='PRIVATE\n'
    assert key.stat().st_mode & 0o777==0o600
    assert shlex.split(env['GIT_SSH_COMMAND'])[:3]==['ssh','-i',str(key)]


@pytest.mark.parametrize('mode',['manual','generated'])
@linux_network
def test_ephemeral_existing_ssh_sources_use_sealed_anonymous_keys(tmp_path,mode,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import git_auth
    receiver,tip=repository(tmp_path,'sha1','small')
    with ssh_server(tmp_path) as (url,key,known):
        options={'repo_url':url,'git_ssh_key':key.read_text()} if mode=='manual' else {'repo_url':url}
        before={str(p.relative_to(tmp_path)):p.stat().st_ino for p in tmp_path.rglob('*')}
        monkeypatch.setenv('GIT_CONFIG_COUNT','1')
        monkeypatch.setenv('GIT_CONFIG_KEY_0','url.ssh://foreign.example/.insteadOf')
        monkeypatch.setenv('GIT_CONFIG_VALUE_0',url)
        monkeypatch.setenv('GIT_SSH_COMMAND','must-not-run')
        monkeypatch.setenv('SSH_AUTH_SOCK','must-not-use')
        with git_auth.publication_auth(options,key,key.with_suffix('.pub'),known_hosts=known) as auth:
            assert len(auth.pass_fds)==1
            descriptor=auth.pass_fds[0]
            import fcntl
            assert fcntl.fcntl(descriptor,fcntl.F_GET_SEALS) & fcntl.F_SEAL_WRITE
            assert not {'GIT_CONFIG_COUNT','GIT_CONFIG_KEY_0','GIT_CONFIG_VALUE_0','SSH_AUTH_SOCK'} & auth.environment.keys()
            observed=command(['git','ls-remote','--refs',url,'refs/heads/main'],
                             env=auth.environment,pass_fds=auth.pass_fds).stdout
            assert observed==tip.encode()+b'\trefs/heads/main\n'
        with pytest.raises(OSError):os.fstat(descriptor)
        assert {str(p.relative_to(tmp_path)):p.stat().st_ino for p in tmp_path.rglob('*')}==before


@linux_network
def test_ephemeral_https_url_auth_is_removed_from_command_destination(tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import git_auth
    import native_publication as pub
    receiver,tip=repository(tmp_path,'sha1','small')
    with tls_server(tmp_path) as (url,cert):
        options={'repo_url':url.replace('https://','https://fixture:fixture-password@'),'repo_branch':'main'}
        route=pub.NetworkRoute.from_options(options)
        assert 'password' not in json.dumps(route.document())
        monkeypatch.setenv('GIT_CONFIG_COUNT','123')
        monkeypatch.setenv('GIT_CONFIG_PARAMETERS','hostile routing')
        monkeypatch.setenv('HTTPS_PROXY','https://foreign.example')
        with git_auth.publication_auth(options,Path('unused'),Path('unused'),ca_file=cert) as auth:
            assert auth.pass_fds==() and auth.environment['GIT_CONFIG_COUNT']=='1'
            assert 'GIT_CONFIG_PARAMETERS' not in auth.environment and 'HTTPS_PROXY' not in auth.environment
            observed=command(['git','-c','http.followRedirects=false','-c','http.proxy=',
                              'ls-remote','--refs',url,'refs/heads/main'],env=auth.environment).stdout
            assert observed==tip.encode()+b'\trefs/heads/main\n'


@pytest.mark.parametrize('mode', ['url', 'askpass'])
@linux_network
def test_legacy_https_git_env_fetch_and_push_original_clients(tmp_path, monkeypatch, mode):
    import git_auth
    receiver, tip = repository(tmp_path, 'sha1', 'small')
    with tls_server(tmp_path) as (url, certificate):
        if mode == 'url':
            url = url.replace('https://', 'https://fixture:fixture-password@')
        else:
            askpass = tmp_path / 'legacy-askpass'
            askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf fixture;; *) printf fixture-password;; esac\n')
            askpass.chmod(0o700)
            monkeypatch.setenv('GIT_ASKPASS', str(askpass))
        options = {'repo_url': url, 'git_ssh_key': ''}
        env = git_auth.git_env(options, tmp_path / 'auth', tmp_path / 'absent-key', tmp_path / 'absent-key.pub')
        env.update(GIT_SSL_CAINFO=str(certificate), GIT_TERMINAL_PROMPT='0',
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        checkout = tmp_path / 'legacy-checkout'
        command(['/usr/bin/git', 'init', str(checkout)], env=env)
        command(['/usr/bin/git', '-C', str(checkout), 'fetch', url, 'refs/heads/main'], env=env)
        command(['/usr/bin/git', '-C', str(checkout), 'checkout', '--detach', 'FETCH_HEAD'], env=env)
        (checkout / 'legacy').write_text('legacy HTTPS publication\n')
        command(['/usr/bin/git', '-C', str(checkout), 'add', 'legacy'], env=env)
        command(['/usr/bin/git', '-C', str(checkout), '-c', 'user.name=Legacy Fixture',
                 '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'legacy fixture'], env=env)
        current = command(['/usr/bin/git', '-C', str(checkout), 'rev-parse', 'HEAD'], env=env).stdout.strip()
        assert current != tip.encode()
        command(['/usr/bin/git', '-C', str(checkout), 'push', url, 'HEAD:refs/heads/main'], env=env)
        assert command(['/usr/bin/git', '--git-dir=' + str(receiver), 'rev-parse', 'refs/heads/main'], env=env).stdout.strip() == current


@linux_network
def test_original_app_container_recreation_retains_pending_same_commit(tmp_path, monkeypatch):
    """The runtime wrapper removes the original App before running phase two."""
    if not os.environ.get('HA_OPS_RUNTIME_RECEIVER_SOCKET'):
        pytest.skip('Requires the original App and persistent receiver sidecar')
    from dataclasses import asdict
    from network_publication_helpers import receiver_request, ReceiverControl
    import native_publication as pub
    import native_preview as preview
    import native_save as save
    import native_disk as disk
    import native_metadata as metadata
    from native_inventory import Policy
    from native_git import NativeGit
    from native_operations import NativeOperationStore
    from test_native_preview import bindings
    import uuid
    marker = Path('/workspace/output/recreated-publication.json')
    if not marker.exists():
        from test_native_publication import publication_fixture
        rig, receiver, old_transport, review, saver, candidate, store, old_executor, kwargs = publication_fixture(tmp_path)
        command(['git', '--git-dir=' + str(receiver), 'config', 'http.receivepack', 'true'])
        control = receiver_control({'drop_receive_response': True, 'fail_upload_after_receive': True})
        peer = receiver_request(action='start', protocol='https', root=str(tmp_path), control=control.identity)
        url, ca = peer['descriptor']
        options = {'repo_url': url.replace('https://', 'https://fixture:fixture-password@'), 'repo_branch': 'main'}
        transport = pub.NetworkGitCLITransport(rig.adapter, options, Path('unused'), Path('unused'), ca_file=Path(ca))
        executor = pub.NativePublication(saver, store, transport)
        with pytest.raises(pub.PublicationError, match='graph fetch unavailable') as error:
            executor.publish(candidate, review, **kwargs)
        intent = pub.validate_intent(store._prepared_publication.artifact)
        source = asdict(rig.source); source['scope'] = [item.hex() for item in source['scope']]
        marker.write_text(json.dumps({'root': str(rig.root), 'scratch': str(rig.scratch), 'source': source,
            'options': options, 'ca': ca, 'control': control.identity, 'peer': peer['token'],
            'operation': error.value.operation_id, 'commit': intent.commit_oid, 'pin': intent.commit_pin,
            'state': store.read(), 'local_ref': kwargs['local_ref'], 'expected_local': kwargs['expected_local']}))
        assert rig.adapter.read_ref(intent.commit_pin).oid == intent.commit_oid
        return
    retained = json.loads(marker.read_text())
    root = Path(retained['root']); policy = Policy(repository_files=(metadata.MANIFEST_PATH,))
    adapter = NativeGit(root, policy=policy)
    source_data = retained['source']; source_data['scope'] = tuple(bytes.fromhex(item) for item in source_data['scope'])
    source = preview.SourceObservation(**source_data)
    owner = preview.NativePreview(adapter, lambda: source, bindings(root, policy), scratch_root=Path(retained['scratch']))
    saver = save.NativeSave(owner, disk.NativeDisk(root)); store = NativeOperationStore(root / 'data')
    assert store.read() == retained['state']
    assert adapter.read_ref(retained['pin']).oid == retained['commit']
    assert adapter.read_ref(retained['local_ref']).oid == retained['expected_local']
    transport = pub.NetworkGitCLITransport(adapter, retained['options'], Path('unused'), Path('unused'), ca_file=Path(retained['ca']))
    calls = []; original = transport._command
    monkeypatch.setattr(transport, '_command', lambda *a, **k: calls.append(a) or original(*a, **k))
    store.lifecycle(store.read()['revision'], 'explicit-App-container-recreation', {})
    executor = pub.NativePublication(saver, store, transport)
    assert not calls and executor.diagnose(retained['operation'])['execution_authority'] is False
    control = ReceiverControl({}); control.identity = retained['control']; control.clear()
    monkeypatch.setattr(transport, 'push', lambda *a, **k: pytest.fail('recreated recovery retried accepted commit'))
    grant = executor.approve_recovery(retained['operation'], expected_revision=store.read()['revision'],
        command_id=str(uuid.uuid4()), pending_effect='reconcile')
    result = executor.resume(grant)
    assert result['status'] == 'completed' and result['commit_oid'] == retained['commit']
    assert adapter.read_ref(retained['local_ref']).oid == retained['commit'] and store.read()['recovery'] is None
    receiver_request(action='stop', token=retained['peer'])


def test_ephemeral_https_askpass_is_explicit_and_routing_env_is_removed(monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import git_auth
    monkeypatch.setenv('GIT_ASKPASS','ambient-askpass')
    monkeypatch.setenv('GIT_SSL_NO_VERIFY','1')
    monkeypatch.setenv('GIT_DIR','foreign')
    monkeypatch.setenv('HTTPS_PROXY','foreign')
    with git_auth.publication_auth({'repo_url':'https://example.com/repo.git'},Path('unused'),Path('unused'),
                                 askpass='explicit-askpass',ca_file='explicit-ca') as auth:
        assert auth.environment['GIT_ASKPASS']=='explicit-askpass'
        assert auth.environment['GIT_SSL_CAINFO']=='explicit-ca'
        assert not {'GIT_SSL_NO_VERIFY','GIT_DIR','HTTPS_PROXY'} & auth.environment.keys()


@pytest.mark.parametrize('protocol,mode',[('ssh','manual'),('ssh','generated'),('https','url')])
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@linux_network
def test_network_native_publication_one_baseline_fetched_proof_and_exact_commit(tmp_path,monkeypatch,protocol,mode,object_format):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import native_publication as pub
    from test_native_publication import publication_fixture
    rig,receiver,local,review,saver,candidate,store,original,kwargs=publication_fixture(tmp_path,object_format=object_format)
    command(['git','--git-dir='+str(receiver),'config','http.receivepack','true'],env=isolated_environment())
    context=ssh_server(tmp_path) if protocol=='ssh' else tls_server(tmp_path)
    snapshots=[];projects=[];snapshot=saver.disk.snapshot;project=saver.disk.project
    monkeypatch.setattr(saver.disk,'snapshot',lambda:snapshots.append(True) or snapshot())
    monkeypatch.setattr(saver.disk,'project',lambda *a,**k:projects.append(a[0]) or project(*a,**k))
    with context as descriptor:
        url=descriptor[0]
        options={'repo_url':url,'repo_branch':'main'}
        if protocol=='ssh':
            _,key,known=descriptor
            if mode=='manual':options['git_ssh_key']=key.read_text()
            transport=pub.NetworkGitCLITransport(rig.adapter,options,key,key.with_suffix('.pub'),known_hosts=known)
        else:
            _,cert=descriptor
            options['repo_url']=url.replace('https://','https://fixture:fixture-password@')
            transport=pub.NetworkGitCLITransport(rig.adapter,options,Path('unused'),Path('unused'),ca_file=cert)
        executor=pub.NativePublication(saver,store,transport)
        with pytest.raises(pub.PublicationError,match='before admission'):
            transport.reachability(transport.advertise(),candidate.remote.remote_oid)
        result=executor.publish(candidate,review,**kwargs)
        assert result['status']=='completed'
        assert transport.advertise().oid==result['commit_oid']
    assert snapshots==[True] and projects==['publication']
    state=store.read();worker=state['workers'][result['operation_id']]
    assert worker['phase']=='completed' and state['recovery'] is None
    intent=pub.validate_intent(store._prepared_publication.artifact)
    assert intent.raw_commit==pub.commit_bytes(candidate,kwargs['author'],kwargs['committer'],kwargs['message'])
    assert rig.adapter.read_ref(kwargs['local_ref']).oid==result['commit_oid']
    encoded=store._prepared_publication.artifact.decode()
    assert 'fixture-password' not in encoded and 'BEGIN OPENSSH PRIVATE KEY' not in encoded
    assert rig.adapter.resources()==()


@contextmanager
def native_network_fixture(tmp_path,protocol,*,control=None):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    import native_publication as pub
    from test_native_publication import publication_fixture
    rig,receiver,local,review,saver,candidate,store,original,kwargs=publication_fixture(tmp_path)
    command(['git','--git-dir='+str(receiver),'config','http.receivepack','true'],env=isolated_environment())
    context=ssh_server(tmp_path) if protocol=='ssh' else tls_server(tmp_path,control)
    with context as descriptor:
        options={'repo_url':descriptor[0],'repo_branch':'main'}
        if protocol=='ssh':
            _,key,known=descriptor
            transport=pub.NetworkGitCLITransport(rig.adapter,options,key,key.with_suffix('.pub'),known_hosts=known)
        else:
            _,cert=descriptor
            options['repo_url']=options['repo_url'].replace('https://','https://fixture:fixture-password@')
            transport=pub.NetworkGitCLITransport(rig.adapter,options,Path('unused'),Path('unused'),ca_file=cert)
        yield rig,receiver,transport,review,saver,candidate,store,pub.NativePublication(saver,store,transport),kwargs


@pytest.mark.parametrize('protocol',['ssh','https'])
@pytest.mark.parametrize('failure',['receiver-reject','stale-lease','lost-ack'])
@linux_network
def test_real_network_refusal_race_and_acceptance_without_ack(tmp_path,protocol,failure):
    import native_publication as pub
    from test_native_publication import next_commit
    control=receiver_control()
    with native_network_fixture(tmp_path,protocol,control=control) as (rig,receiver,t,r,s,c,o,x,kw):
        classifier=t._push_kind
        def classify(intent,code,out,error):
            print('NETWORK_REFUSAL_TRANSCRIPT '+repr(out))
            return classifier(intent,code,out,error)
        t._push_kind=classify
        with tempfile.TemporaryDirectory(prefix='native-network-hooks-',dir='/workspace/output') as hooks:
            if failure=='receiver-reject':
                hook=Path(hooks)/'pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o700)
                command(['git','--git-dir='+str(receiver),'config','core.hooksPath',hooks])
            elif failure=='lost-ack':
                if protocol=='https':control['drop_receive_response']=True
                else:
                    hook=Path(hooks)/'post-receive';hook.write_text('#!/bin/sh\nkill -KILL "$PPID"\n');hook.chmod(0o700)
                    command(['git','--git-dir='+str(receiver),'config','core.hooksPath',hooks])
            else:
                descendant,raw=next_commit(rig,c.remote.remote_oid,message=b'actual remote race\n')
                command(['git','--git-dir='+str(receiver),'hash-object','-t','commit','-w','--stdin'],input=raw)
                def race(point):
                    if point=='publication-effect_started' and pub._parse(o._prepared_publication._current)['workers'][o._prepared_publication.operation_id]['publication_attempt']=='possible':
                        command(['git','--git-dir='+str(receiver),'update-ref','refs/heads/main',descendant,c.remote.remote_oid])
                o.fault=race
            result=x.publish(c,r,**kw)
            expected='completed' if failure=='lost-ack' else 'refused'
            assert result['status']==expected
            state=o.read();worker=state['workers'][result['operation_id']]
            assert state['recovery'] is None and rig.adapter.resources()==()
            assert rig.adapter.read_ref(kw['local_ref']).oid==(result['commit_oid'] if expected=='completed' else kw['expected_local'])
            assert worker['publication_attempt']=='possible'
            if expected=='completed':assert worker['publication_outcome']['kind']=='published'


@linux_network
def test_initial_network_fetch_failure_preserves_review_and_no_accepted_worker(tmp_path):
    import native_publication as pub
    control=receiver_control({'fail_upload':True})
    with native_network_fixture(tmp_path,'https',control=control) as (rig,receiver,t,r,s,c,o,x,kw):
        with pytest.raises(pub.PublicationError,match='graph fetch unavailable'):x.publish(c,r,**kw)
        assert not (rig.root/'data/native-operations/state.json').exists()
        assert rig.adapter.resources()==() and x.transport_debts()==()
        control['fail_upload']=False
        assert x.publish(c,r,**kw)['status']=='completed'


@linux_network
def test_preacceptance_owned_scratch_debt_has_explicit_offline_settlement(tmp_path,monkeypatch):
    import native_publication as pub
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        real=os.rmdir;fired=[]
        def fail(path,*args,**kwargs):
            if path==b'pack' and kwargs.get('dir_fd') is not None and not fired:
                fired.append(True);raise OSError(5,'owned scratch directory durability failure')
            return real(path,*args,**kwargs)
        monkeypatch.setattr(os,'rmdir',fail)
        with pytest.raises(Exception):x.publish(c,r,**kw)
        assert fired and not (rig.root/'data/native-operations/state.json').exists()
        debts=x.transport_debts();assert len(debts)==1 and debts[0]['phase']=='debt'
        monkeypatch.setattr(os,'rmdir',real)
        monkeypatch.setattr(t,'advertise',lambda:pytest.fail('local scratch settlement contacted receiver'))
        debt=debts[0]
        result=x.settle_transport_resource(debt['resource_id'],operation_id=debt['operation'],owner_id=debt['owner'])
        assert result.state=='removed' and x.transport_debts()==()


@linux_network
def test_network_tmpfs_physical_peak_fits_single_frozen_forecast(tmp_path,monkeypatch):
    from test_native_ext4 import observe
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        before=observe(rig.root);samples=[];original=rig.adapter._capture
        def sample(handle,*args,**kwargs):
            result=original(handle,*args,**kwargs)
            if t._admitted is not None:
                plan=t._admitted[1].plan;after=observe(rig.root)
                physical=sum(row['blocks'] for row in after['entries'].values())-sum(row['blocks'] for row in before['entries'].values())
                new=len(set(after['entries'])-set(before['entries']))
                bound=sum(row.additional_peak for row in plan.decisions)
                inodes=sum(row.required_inodes for row in plan.decisions)
                assert 0<=physical<=bound and new<=inodes
                assert plan.network_cap>0
                samples.append((physical,new,bound,inodes,plan.network_cap))
            return result
        monkeypatch.setattr(rig.adapter,'_capture',sample)
        assert x.publish(c,r,**kw)['status']=='completed'
        assert len(samples)>=6 and max(row[0] for row in samples)>0
        assert len({row[2:] for row in samples})==1
        print('NETWORK_PUBLICATION_ALLOCATION '+json.dumps(samples))
        return samples


@linux_network
@pytest.mark.parametrize('retained_scratch',[False,True])
def test_receiver_acceptance_readback_failure_lifecycle_and_explicit_same_commit_reconcile(tmp_path,monkeypatch,retained_scratch):
    import native_publication as pub
    from native_operations import NativeOperationStore
    import uuid
    control=receiver_control({'drop_receive_response':True,'fail_upload_after_receive':True})
    with native_network_fixture(tmp_path,'https',control=control) as (rig,receiver,t,r,s,c,o,x,kw):
        rmdir=os.rmdir;cleanup_failed=[]
        def retain_owned_pack(path,*args,**kwargs):
            if (retained_scratch and control.get('fail_upload') and path==b'pack'
                    and kwargs.get('dir_fd') is not None and not cleanup_failed):
                cleanup_failed.append(True)
                raise OSError(5,'owned readback scratch cleanup interrupted')
            return rmdir(path,*args,**kwargs)
        monkeypatch.setattr(os,'rmdir',retain_owned_pack)
        with pytest.raises(pub.PublicationError,match='graph fetch unavailable') as error:x.publish(c,r,**kw)
        op=error.value.operation_id;state=o.read();worker=state['workers'][op]
        if retained_scratch:assert cleanup_failed and len(x.transport_debts())==1
        intent=pub.validate_intent(o._prepared_publication.artifact)
        assert worker['phase']=='recovery_required' and worker['publication_attempt']=='possible'
        assert worker['publication_outcome'] is None and rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
        assert rig.adapter.read_ref(intent.commit_pin).oid==intent.commit_oid
        calls=[];original=t._command
        monkeypatch.setattr(t,'_command',lambda *a,**k:calls.append(a) or original(*a,**k))
        replacement=NativeOperationStore(rig.root/'data')
        replacement.lifecycle(state['revision'],'explicit-restart-version-fixture',{})
        resumed=pub.NativePublication(s,replacement,t)
        assert resumed.diagnose(op)['execution_authority'] is False and not calls
        control.clear()
        current=replacement.read()
        debts=resumed.transport_debts()
        if debts:
            # Failed graph readback may retain exact owned scratch. Lifecycle
            # and reconcile cannot silently consume this separate local debt.
            from native_disk import Blocked as DiskBlocked
            grant=resumed.approve_recovery(op,expected_revision=current['revision'],command_id=str(uuid.uuid4()),pending_effect='reconcile')
            with pytest.raises(DiskBlocked,match='prior-transport-scratch-requires-explicit-settlement'):
                resumed.resume(grant)
            before_calls=len(calls)
            for debt in debts:
                assert debt['operation']==op and debt['owner']==worker['owner'] and debt['phase']=='debt'
                result=resumed.settle_transport_resource(debt['resource_id'],operation_id=op,owner_id=debt['owner'])
                assert result.state=='removed' and not result.cleanup_errors
            assert len(calls)==before_calls and not resumed.transport_debts()
            assert replacement.read()==current
        grant=resumed.approve_recovery(op,expected_revision=current['revision'],command_id=str(uuid.uuid4()),pending_effect='reconcile')
        result=resumed.resume(grant)
        assert result['status']=='completed' and result['commit_oid']==intent.commit_oid
        assert rig.adapter.read_ref(kw['local_ref']).oid==intent.commit_oid
        assert not any(args[0][0]=='push' for args in calls),'Reconcile must not retry the accepted commit'
        assert replacement.read()['recovery'] is None and rig.adapter.resources()==()


@linux_network
def test_network_terminal_cleanup_survives_removed_credentials_and_ca(tmp_path,monkeypatch):
    import native_publication as pub
    import uuid
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        def stop(point):
            if point=='publication-completed':raise KeyboardInterrupt('terminal cleanup pending')
        o.fault=stop
        with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
        op=error.value.operation_id;state=o.read();o.fault=lambda point:None
        t.options['repo_url']=t.options['repo_url'].replace('fixture-password','expired-password')
        Path(t.ca_file).unlink()
        reopened=pub.NetworkGitCLITransport(rig.adapter,t.options,Path('missing'),Path('missing'),ca_file=t.ca_file)
        monkeypatch.setattr(reopened,'_command',lambda *a,**k:pytest.fail('terminal cleanup contacted remote'))
        executor=pub.NativePublication(s,o,reopened)
        assert executor.diagnose(op)['execution_authority'] is False
        grant=executor.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
        assert executor.resume(grant)['status']=='completed' and o.read()['recovery'] is None


@linux_network
def test_sigkill_after_fetched_objects_before_journal_ack_preserves_unknown_data(tmp_path):
    import signal
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        pid=os.fork()
        if pid==0:
            original=rig.adapter._capture
            def kill_before_ack(handle,*args,**kwargs):
                if list(Path(os.fsdecode(rig.adapter._resource_path(handle.resource_id))).glob('objects/pack/*.idx')):
                    os.kill(os.getpid(),signal.SIGKILL)
                return original(handle,*args,**kwargs)
            rig.adapter._capture=kill_before_ack
            try:x.publish(c,r,**kw)
            finally:os._exit(2)
        _,status=os.waitpid(pid,0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status)==signal.SIGKILL
        assert not (rig.root/'data/native-operations/state.json').exists()
        records=x.transport_debts();assert len(records)==1
        record=records[0];resource_root=rig.adapter._resource_path(record['resource_id'])
        pack=next(Path(os.fsdecode(resource_root)).glob('objects/pack/*.pack'))
        before=pack.read_bytes()
        outcome=rig.adapter.recover_resource(record['resource_id'],operation_id=record['operation'],owner_id=record['owner'])
        assert outcome.state=='debt' and pack.read_bytes()==before
        retained=x.transport_debts()[0]
        assert retained['resource_id']==record['resource_id'] and retained['errors']
        # A fresh explicit disposition observes unknown bytes rather than
        # manufacturing an old ownership receipt. Only its durable approval
        # permits cleanup; the unchanged reviewed candidate can then publish.
        import uuid
        grant=x.review_transport_disposition(record['resource_id'],operation_id=record['operation'],owner_id=record['owner'])
        assert x.approve_transport_disposition(grant,command_id=str(uuid.uuid4())).state=='removed'
        assert x.transport_debts()==()
        assert x.publish(c,r,**kw)['status']=='completed'


@pytest.mark.parametrize('fault',['short','enospc'])
@linux_network
def test_network_scratch_control_io_failure_preserves_candidate_and_explicit_debt(tmp_path,monkeypatch,fault):
    import errno
    import uuid
    import native_publication as pub
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        original=os.write;reached=[]
        def fail(fd,data):
            path=os.readlink('/proc/self/fd/'+str(fd))
            if path.endswith('/config') and path.startswith(os.fsdecode(rig.adapter.resources_root)+'/'):
                reached.append(path)
                if fault=='enospc':raise OSError(errno.ENOSPC,'injected scratch control ENOSPC')
                return original(fd,data[:-1])
            return original(fd,data)
        with monkeypatch.context() as changes:
            changes.setattr(os,'write',fail)
            with pytest.raises((OSError,pub.PublicationError)):x.publish(c,r,**kw)
        assert reached and not (rig.root/'data/native-operations/state.json').exists()
        assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
        rows=x.transport_debts();assert len(rows)==1
        row=rows[0]
        grant=x.review_transport_disposition(row['resource_id'],operation_id=row['operation'],owner_id=row['owner'])
        assert x.approve_transport_disposition(grant,command_id=str(uuid.uuid4())).state=='removed'
        assert x.publish(c,r,**kw)['status']=='completed'


@pytest.mark.parametrize('attack',['redirect','bad-ca','ambient-routing','route-after-admission'])
@linux_network
def test_actual_network_tls_and_routing_fences(tmp_path,monkeypatch,attack):
    import native_publication as pub
    control=receiver_control()
    with native_network_fixture(tmp_path,'https',control=control) as (rig,receiver,t,r,s,c,o,x,kw):
        if attack=='ambient-routing':
            for key,value in {'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'url.http://invalid.insteadOf',
                    'GIT_CONFIG_VALUE_0':'https://','GIT_SSH_COMMAND':'false','https_proxy':'http://127.0.0.1:1',
                    'GIT_SSL_NO_VERIFY':'1','GIT_CONFIG_GLOBAL':'/missing/hostile'}.items():monkeypatch.setenv(key,value)
            assert x.publish(c,r,**kw)['status']=='completed'
            return
        if attack=='redirect':control['redirect']='http://127.0.0.1:1/foreign.git'
        elif attack=='bad-ca':
            other=tmp_path/'other-tls';other.mkdir()
            with tls_server(other) as (_,ca):
                t=pub.NetworkGitCLITransport(rig.adapter,t.options,Path('unused'),Path('unused'),ca_file=ca)
                x=pub.NativePublication(s,o,t)
                with pytest.raises(pub.PublicationError):x.publish(c,r,**kw)
            assert not (rig.root/'data/native-operations/state.json').exists()
            return
        else:
            original=rig.adapter._capture
            def change(handle,*args,**kwargs):
                result=original(handle,*args,**kwargs)
                t.options['repo_url']=t.options['repo_url'].replace('receiver.git','foreign.git')
                return result
            monkeypatch.setattr(rig.adapter,'_capture',change)
        with pytest.raises((pub.PublicationError,RuntimeError)):x.publish(c,r,**kw)
        assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
        assert command(['git','--git-dir='+str(receiver),'rev-parse','refs/heads/main']).stdout.strip().decode()==c.remote.remote_oid


@linux_network
def test_actual_send_child_timeout_kills_owned_group_and_explicit_same_commit_recovery(tmp_path,monkeypatch):
    import native_publication as pub
    import uuid
    control=receiver_control({'stall_receive':True})
    with native_network_fixture(tmp_path,'https',control=control) as (rig,receiver,t,r,s,c,o,x,kw):
        t=pub.NetworkGitCLITransport(rig.adapter,t.options,Path('unused'),Path('unused'),ca_file=t.ca_file,timeout=.75)
        x=pub.NativePublication(s,o,t)
        groups=[];original=pub.os.killpg
        def kill(group,sig):groups.append((group,sig));return original(group,sig)
        monkeypatch.setattr(pub.os,'killpg',kill)
        with pytest.raises(pub.PublicationError,match='uncertain') as caught:x.publish(c,r,**kw)
        assert control['receive_started'] and groups
        op=caught.value.operation_id
        state=o.read();assert state['workers'][op]['phase']=='recovery_required'
        intent=pub.validate_intent(o._prepared_publication.artifact)
        assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
        assert rig.adapter.resources()==()
        control.clear()
        grant=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='push')
        result=x.resume(grant)
        assert result['status']=='completed' and result['commit_oid']==intent.commit_oid


@pytest.mark.parametrize('protocol',['ssh','https'])
@linux_network
def test_network_descendant_acceptance_local_cas_conflict_and_fresh_reconcile(tmp_path,monkeypatch,protocol):
    import native_publication as pub
    import uuid
    from test_native_publication import next_commit
    from native_topology_helpers import git
    with native_network_fixture(tmp_path,protocol) as (rig,receiver,t,r,s,c,o,x,kw):
        original=t.push;pushed=[]
        other,_=next_commit(rig,kw['expected_local'],message=b'unrelated local change\n')
        def push(intent):
            outcome=original(intent);pushed.append(intent.commit_oid)
            child,raw=next_commit(rig,intent.commit_oid,message=b'receiver descendant\n')
            command(['git','--git-dir='+str(receiver),'hash-object','-t','commit','-w','--stdin'],input=raw)
            command(['git','--git-dir='+str(receiver),'update-ref','refs/heads/main',child,intent.commit_oid])
            git(rig.root,'update-ref',kw['local_ref'],other)
            return outcome
        with monkeypatch.context() as changes:
            changes.setattr(t,'push',push)
            with pytest.raises(Exception,match='conflict|changed-destination-namespace') as caught:x.publish(c,r,**kw)
        op=caught.value.operation_id;state=o.read()
        assert state['workers'][op]['phase']=='recovery_required' and pushed
        assert rig.adapter.read_ref(kw['local_ref']).oid==other
        git(rig.root,'update-ref',kw['local_ref'],kw['expected_local'],other)
        monkeypatch.setattr(t,'push',lambda *a,**k:pytest.fail('reconcile retried send'))
        grant=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='reconcile')
        assert x.resume(grant)['commit_oid']==pushed[0]
        assert rig.adapter.resources()==()


@linux_network
def test_actual_receiver_corruption_after_send_is_uncertain_and_pins_survive(tmp_path,monkeypatch):
    import native_publication as pub
    import uuid
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        original=t.push;saved=[]
        def corrupt(intent):
            outcome=original(intent)
            path=receiver/'objects'/intent.commit_oid[:2]/intent.commit_oid[2:]
            assert path.is_file(),'small receiver pack is unpacked by qualified default receiver'
            saved.append((path,path.read_bytes()));path.write_bytes(b'corrupted receiver object')
            return outcome
        with monkeypatch.context() as changes:
            changes.setattr(t,'push',corrupt)
            with pytest.raises(pub.PublicationError,match='graph fetch unavailable') as caught:x.publish(c,r,**kw)
        op=caught.value.operation_id;state=o.read();intent=pub.validate_intent(o._prepared_publication.artifact)
        assert state['workers'][op]['phase']=='recovery_required'
        assert rig.adapter.read_ref(intent.commit_pin).oid==intent.commit_oid
        assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
        for path,raw in saved:path.write_bytes(raw)
        monkeypatch.setattr(t,'push',lambda *a,**k:pytest.fail('corruption reconcile retried send'))
        grant=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='reconcile')
        assert x.resume(grant)['commit_oid']==intent.commit_oid


@linux_network
def test_parent_sigkill_during_actual_fetch_durable_orphan_group_blocks_disposition(tmp_path):
    import signal
    import uuid
    from native_git import InvalidInput
    control=receiver_control({'stall_upload':True})
    with native_network_fixture(tmp_path,'https',control=control) as (rig,receiver,t,r,s,c,o,x,kw):
        client=os.fork()
        if client==0:
            try:x.publish(c,r,**kw)
            finally:os._exit(2)
        group=None
        try:
            deadline=time.monotonic()+10
            while not control.get('upload_started') and time.monotonic()<deadline:time.sleep(.01)
            assert control.get('upload_started')
            os.kill(client,signal.SIGKILL);os.waitpid(client,0);client=None
            row=x.transport_debts()[0]
            with rig.adapter._journal() as journal:group=journal['records'][row['resource_id']]['writer']
            with pytest.raises(InvalidInput,match='writer-busy'):
                x.review_transport_disposition(row['resource_id'],operation_id=row['operation'],owner_id=row['owner'])
        finally:
            if client is not None:os.kill(client,signal.SIGKILL);os.waitpid(client,0)
            if group is not None:
                os.killpg(group,signal.SIGKILL)
                # PID 1 in the isolated container owns adopted descendants.
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:
                        pid,_=os.waitpid(-group,os.WNOHANG)
                    except ChildProcessError:pass
                    try:
                        rig.adapter._network_group_quiet(group)
                        break
                    except InvalidInput as error:
                        if str(error)!='transport-unreaped-writer-busy':raise
                    time.sleep(.01)
            control['release_upload']=True
        grant=x.review_transport_disposition(row['resource_id'],operation_id=row['operation'],owner_id=row['owner'])
        assert x.approve_transport_disposition(grant,command_id=str(uuid.uuid4())).state=='removed'
        control.clear()
        assert x.publish(c,r,**kw)['status']=='completed'


@linux_network
def test_linux_admitted_kernel_cap_cannot_be_mutated_before_fetch(tmp_path,monkeypatch):
    from native_disk import Blocked
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        original=t._command;reached=[]
        def mutate(args,**kwargs):
            if args[0]!='fetch':return original(args,**kwargs)
            plan=t._admitted[1].plan;old=plan.network_cap
            object.__setattr__(plan,'network_cap',old+4096);reached.append(True)
            try:return original(args,**kwargs)
            finally:object.__setattr__(plan,'network_cap',old)
        monkeypatch.setattr(t,'_command',mutate)
        with pytest.raises(Blocked):x.publish(c,r,**kw)
        assert reached and not (rig.root/'data/native-operations/state.json').exists()
        assert rig.adapter.resources()==()


@linux_network
def test_reviewed_disposition_refuses_lifecycle_state_change_and_reopen(tmp_path):
    import uuid
    import native_publication as pub
    from native_operations import NativeOperationStore
    from test_native_transport_disposition import debt
    with native_network_fixture(tmp_path,'https') as (rig,receiver,t,r,s,c,o,x,kw):
        rid,binding,root=debt(rig.adapter)
        state=o.read()
        grant=x.review_transport_disposition(rid,**binding)
        o.lifecycle(state['revision'],'explicit-version-transition',{})
        with pytest.raises(pub.PublicationError,match='current-transport'):x.approve_transport_disposition(grant,command_id=str(uuid.uuid4()))
        reopened=pub.NativePublication(s,NativeOperationStore(rig.root/'data'),t)
        with pytest.raises(pub.PublicationError,match='current-transport'):reopened.approve_transport_disposition(grant,command_id=str(uuid.uuid4()))
        fresh=reopened.review_transport_disposition(rid,**binding)
        assert reopened.approve_transport_disposition(fresh,command_id=str(uuid.uuid4())).state=='removed'
