"""Read-only projections from explicitly qualified local store composition."""
import copy
import hashlib
import stat
import os
from pathlib import Path
import threading


class NativeStatus:
    def __init__(self, operations=None, services=None, recovery=None):
        # Production does not yet supply a qualified native live binding.
        self.operations, self.services, self.recovery = operations, services, recovery
        self.lock = threading.RLock()
        self._generation, self._blobs = None, ()
        self.recovery_key = None
        self.recovery_value = None
        self.service_value = None
        self.initial_errors = {}
        self.data_dir = None

    def _key(self):
        store = self.recovery
        root = Path(os.fsdecode(store.binding.root)) / os.fsdecode(store.directory)
        paths = [root, root / 'active.json', root / 'authority.key']
        # Fixed control paths plus retained blob names from the last validated
        # manifest. No recursive directory enumeration on ordinary polling.
        if self._generation:
            generation = root / self._generation
            paths += [generation, generation / 'manifest.json', generation / 'journal.json']
            paths += [generation / name for name in self._blobs]
        result = []
        for path in paths:
            try:
                s = path.lstat()
                content = None
                if path.name in ('active.json', 'authority.key'):
                    # The fixed-size pointer/key controls are cheap to fingerprint
                    # even when same-size writes share a timestamp tick. Published
                    # manifests/journals change via atomic inode replacement.
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    try:
                        held = os.fstat(fd)
                        if not stat.S_ISREG(held.st_mode) or held.st_size > 4 * 1024 * 1024:
                            raise ValueError('Unsafe recovery control')
                        digest = hashlib.sha256()
                        total = 0
                        while True:
                            chunk = os.read(fd, 65536)
                            if not chunk: break
                            total += len(chunk)
                            if total > 4 * 1024 * 1024: raise ValueError('Recovery control grew beyond limit')
                            digest.update(chunk)
                        content = digest.hexdigest()
                    finally:
                        os.close(fd)
                result.append((str(path), s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns, content))
            except FileNotFoundError:
                result.append((str(path), None))
        return tuple(result)

    def _attach_existing(self):
        if self.data_dir is None:
            return
        from native_operations import NativeOperationStore
        from native_services import ServiceStore
        # Retry absent/previously unsafe readers on each observation. Constructors
        # are gated by an actual journal, never by a display request alone.
        if self.operations is None and (self.data_dir / 'native-operations' / 'state.json').is_file():
            self.operations = NativeOperationStore(self.data_dir)
        if self.services is None and (self.data_dir / 'native-services' / 'state.json').is_file():
            try:
                self.services = ServiceStore(self.data_dir / 'native-services', create=False)
                self.initial_errors.pop('services', None)
            except (OSError, ValueError):
                self.initial_errors['services'] = dict(status='blocked', message='Service journal unavailable')

    def read(self):
        with self.lock:
            self._attach_existing()
            result = dict(available=False, reason='Native live adapter is not qualified. Native actions remain unavailable.', actions_enabled=False, operation=None, services=None, recovery=None)
            result.update(copy.deepcopy(self.initial_errors))
            if self.operations is not None and (self.operations.data_root / 'native-operations' / 'state.json').is_file():
                try:
                    projection = self.operations.refresh(observe=True)
                    result['operation'] = {key: projection[key] for key in ('revision', 'worker')}
                    result['operation']['candidates'] = {domain: None if value is None else {key: value[key] for key in ('id', 'status')} for domain, value in projection['candidates'].items()}
                except Exception:
                    result['operation'] = dict(status='blocked', message='Native operation journal unavailable')
            if self.services is not None:
                try:
                    raw = self.services.discover()
                    if raw:
                        self.service_value = {key: copy.deepcopy(raw.get(key)) for key in ('operation', 'revision', 'phase', 'pending')}
                        self.service_value['entries'] = [{key: item.get(key) for key in ('id', 'original', 'stop_disposition', 'health', 'error', 'uncertain')} for item in raw['entries']]
                    else:
                        self.service_value = None
                    result['services'] = copy.deepcopy(self.service_value)
                except Exception as exc:
                    result['services'] = dict(status='busy' if type(exc).__name__ == 'Conflict' else 'blocked', previous=copy.deepcopy(self.service_value), message='Service worker busy' if type(exc).__name__ == 'Conflict' else 'Service journal unavailable')
            if self.recovery is not None:
                try:
                    key = self._key()
                    if key != self.recovery_key:
                        self.recovery_key, self.recovery_value = None, None
                        # The first discovery may teach us generation/blob paths.
                        # Validate again with that complete watch set held constant.
                        # Never publish an old projection under a later source key.
                        for _ in range(2):
                            before = self._key()
                            watched = self._generation, self._blobs
                            raw = self.recovery.discover()
                            generation = raw['journal']['generation'] if raw else None
                            blobs = tuple(sorted(name for name in raw['manifest']['cleanup']['files'] if name.startswith('blob-'))) if raw else ()
                            self._generation, self._blobs = generation, blobs
                            if watched != (generation, blobs):
                                continue
                            after = self._key()
                            if before != after:
                                continue
                            if raw:
                                journal = raw['journal']
                                self.recovery_value = {name: journal.get(name) for name in ('operation', 'generation', 'revision', 'phase')}
                                self.recovery_value['changed_count'] = len(journal['changed'])
                                self.recovery_value['pending_intent'] = journal['pending'] is not None
                            self.recovery_key = before
                            break
                        else:
                            result['recovery'] = dict(status='busy', message='Recovery journal changed during validation; retrying')
                            return result
                    result['recovery'] = copy.deepcopy(self.recovery_value)
                except Exception:
                    self.recovery_key, self.recovery_value = None, None
                    result['recovery'] = dict(status='blocked', message='Recovery journal validation failed')
            return result


def existing_status(data_dir):
    """Open existing journals only; never initialize a dormant native store."""
    status = NativeStatus()
    status.data_dir = Path(data_dir)
    return status
