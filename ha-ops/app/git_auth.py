import os
import socket
import shlex
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import fcntl
from urllib.parse import urlsplit,unquote


def generated_deploy_key_exists(key_path, pub_path):
    return key_path.exists() and pub_path.exists()


def load_generated_public_key(pub_path):
    if not pub_path.exists():
        return ""
    return pub_path.read_text().strip()


def git_auth_mode(options, key_path, pub_path):
    if options.get("git_ssh_key", "").strip():
        return "manual"
    if generated_deploy_key_exists(key_path, pub_path):
        return "generated"
    return "none"


def setup_git_ssh_env(env, work_dir, key_text=None, key_path=None):
    if key_text:
        work_dir.mkdir(parents=True, exist_ok=True)
        key_path = work_dir / "manual_deploy_key"
        # The option reader strips surrounding whitespace. OpenSSH's private
        # key parser still requires a terminating newline in the key file.
        key_path.write_text(key_text.rstrip()+'\n')
        os.chmod(key_path, 0o600)

    if key_path:
        env["GIT_SSH_COMMAND"] = f"ssh -i {shlex.quote(os.fspath(key_path))} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return env


def git_env(options, work_dir, generated_key_path, generated_pub_path):
    env = os.environ.copy()
    git_ssh_key = options.get("git_ssh_key", "").strip()
    if git_ssh_key:
        setup_git_ssh_env(env, work_dir, key_text=git_ssh_key)
    elif generated_deploy_key_exists(generated_key_path, generated_pub_path):
        setup_git_ssh_env(env, work_dir, key_path=generated_key_path)
    return env


class PublicationAuthenticationError(RuntimeError):
    """Sanitized ephemeral publication authentication failure."""


@dataclass(frozen=True)
class PublicationAuthentication:
    environment: dict
    pass_fds: tuple


@contextmanager
def publication_auth(options,generated_key_path,generated_pub_path,*,known_hosts=None,
                     askpass=None,ca_file=None):
    """Bridge existing key/HTTPS sources without creating a disk key file.

    This supplies ephemeral authentication only. The publication caller must
    separately qualify and freeze its destination and bound every command.
    Inherited Git configuration, proxies and SSH routing cannot enter the
    command environment through this adapter. Linux anonymous sealed key FDs
    are closed after the caller's complete command/process-group lifetime.
    """
    env={k:v for k,v in os.environ.items() if not k.startswith(('GIT_','SSH_')) and
         k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0',GIT_NO_REPLACE_OBJECTS='1',GIT_NO_LAZY_FETCH='1')
    descriptor=None;primary=None
    try:
        url=options.get('repo_url','')
        if url.lower().startswith('https://'):
            parsed=urlsplit(url)
            if parsed.username is not None:
                username=unquote(parsed.username);password=unquote(parsed.password or '')
                encoded=base64.b64encode((username+':'+password).encode()).decode('ascii')
                # Exactly this authentication header is reintroduced. No
                # caller-authored config count/parameters can redirect Git.
                env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.extraHeader',
                           GIT_CONFIG_VALUE_0='Authorization: Basic '+encoded)
            elif askpass is not None:
                env['GIT_ASKPASS']=os.fspath(askpass)
            if ca_file is not None: env['GIT_SSL_CAINFO']=os.fspath(ca_file)
        else:
            if known_hosts is None:
                raise PublicationAuthenticationError('qualified SSH host-key source required')
            mode=git_auth_mode(options,generated_key_path,generated_pub_path)
            if mode=='manual': key=options['git_ssh_key'].strip().encode()+b'\n'
            elif mode=='generated':
                with open(generated_key_path,'rb') as stream: key=stream.read(65537)
            else: raise PublicationAuthenticationError('configured SSH key unavailable')
            if not key or len(key)>65536 or not hasattr(os,'memfd_create'):
                raise PublicationAuthenticationError('qualified bounded Linux SSH key source required')
            descriptor=os.memfd_create('native-publication-auth',os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
            os.fchmod(descriptor,0o600)
            view=memoryview(key)
            while view:
                written=os.write(descriptor,view)
                if written<=0: raise PublicationAuthenticationError('anonymous SSH key write incomplete')
                view=view[written:]
            fcntl.fcntl(descriptor,fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_SEAL|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_GROW|fcntl.F_SEAL_WRITE)
            # Git closes nonstandard inherited FDs before launching SSH. The
            # owned Python process remains alive through command reaping, so
            # SSH opens its private sealed descriptor via this exact proc path.
            argv=['ssh','-F',os.devnull,'-i','/proc/%d/fd/%d'%(os.getpid(),descriptor),
                  '-o','IdentitiesOnly=yes','-o','IdentityAgent=none','-o','BatchMode=yes',
                  '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+os.fspath(known_hosts),
                  '-o','GlobalKnownHostsFile='+os.devnull,'-o','ProxyCommand=none','-o','ProxyJump=none',
                  '-o','ControlMaster=no','-o','ControlPath=none']
            env['GIT_SSH_COMMAND']=shlex.join(argv)
        yield PublicationAuthentication(env,() if descriptor is None else (descriptor,))
    except BaseException as error:
        primary=error
        raise
    finally:
        if descriptor is not None:
            try: os.close(descriptor)
            except BaseException as error:
                if primary is None: raise
                primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+(
                    'publication-auth-descriptor-close:fd=%d:%s'%(descriptor,type(error).__name__),)


def generate_deploy_key(work_dir, key_path, pub_path, run_command, log):
    work_dir.mkdir(parents=True, exist_ok=True)
    comment = f"ha-ops@{socket.gethostname()}"
    temp_key_path = work_dir / "generated_deploy_key.new"
    temp_pub_path = work_dir / "generated_deploy_key.new.pub"
    for path in [temp_key_path, temp_pub_path]:
        if path.exists():
            path.unlink()

    try:
        result = run_command(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                comment,
                "-f",
                str(temp_key_path),
            ]
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ssh-keygen is not available inside the App image") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed:\n{result.stderr.strip() or result.stdout.strip()}")

    temp_key_path.replace(key_path)
    temp_pub_path.replace(pub_path)
    os.chmod(key_path, 0o600)
    public_key = load_generated_public_key(pub_path)
    log(f"Generated deploy key with comment {comment}")
    return public_key
