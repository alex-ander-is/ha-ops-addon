#!/usr/bin/env python3
"""Build and qualify the original installable App on the named local VM.

All mutation goes through ha_ops_dev's explicit local endpoint and admission.
Receiver tooling is a remote peer, never part of the App client filesystem.
"""
import argparse
import hashlib
import io
import tarfile
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
import stat
import tempfile

import ha_ops_dev as vm

APP = vm.ROOT / 'ha-ops'
BASE_INDEX = 'ghcr.io/home-assistant/base:3.24-2026.08.0@sha256:93ef607824e3f27e868f11b10938283a98bf880ed57bcf8eaa81c6c2d521f6f5'
BASES = dict.fromkeys(('aarch64', 'amd64'), BASE_INDEX)
PACKAGES = 'bash curl diffutils git=2.54.0-r0 mosquitto-clients openssh-client openssh-keygen py3-pip py3-yaml python3 rsync'
CACHE = vm.LOCAL / 'runtime-cache'
DEPENDENCY_TREES = {}
DEPENDENCY_OWNERS = {}
DEPENDENCY_DIRECTORIES = {}
DEPENDENCY_IDENTITIES = {}
DEPENDENCY_DIRECTORY_IDENTITIES = {}
DEPENDENCY_FILE_IDENTITIES = {}
DEPENDENCY_CLOSE_DEBT = {}



def attach_cleanup(primary, failures):
    if failures:
        primary.cleanup_errors = tuple(getattr(primary, 'cleanup_errors', ())) + tuple(failures)


def acquisition_debt(error, phase, path):
    row = {'phase': phase, 'path': str(path), 'status': 'retained',
           'reason': 'Creation identity unavailable or acquisition incomplete; inspect only this exact path'}
    error.acquisition_debt = tuple(getattr(error, 'acquisition_debt', ())) + (row,)
    attach_cleanup(error, [row])


def tree_manifest(path):
    """Immutable fixture proof; output alone may contain dynamic test data."""
    vm.no_symlinks(path)
    rows = {}
    for item in [path, *sorted(path.rglob('*'))]:
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise vm.Refused('Unowned runtime fixture entry: ' + str(item))
        rows[str(item.relative_to(path))] = [info.st_dev, info.st_ino, info.st_mode,
            hashlib.sha256(item.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None]
    return rows



def entry_identity(info):
    return [info.st_dev, info.st_ino, info.st_mode]


def require_private_file(handle):
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise vm.Refused('File lost exclusive regular-file ownership')
    return info



class DescriptorScope:
    """Enroll before fallible handoffs; a close attempt consumes FD ownership."""
    def __init__(self):
        self.handles = []

    def own(self, handle):
        self.handles.append(handle)
        return handle

    def detach(self, handle):
        self.handles.remove(handle)
        return handle

    def close(self, handle):
        # close may already have released/reused the number when it raises.
        # Never retry that integer or leave it enrolled for another close.
        self.detach(handle)
        os.close(handle)

    def close_all(self, primary=None, *, phase='copy descriptor close'):
        handles = list(reversed(self.handles))
        self.handles.clear()
        close_copy_handles(handles, primary, phase=phase)

def open_directory_receipt(parent, name, receipt):
    scope = DescriptorScope()
    first = None
    try:
        handle = scope.own(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent))
        visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if entry_identity(os.fstat(handle)) != entry_identity(receipt) or entry_identity(visible) != entry_identity(receipt):
            raise vm.Refused('Created directory changed before descriptor open')
        return scope.detach(handle)
    except BaseException as error:
        first = error
        raise
    finally: scope.close_all(first)


def require_held_root(path, owned):
    current = os.stat(owned.name, dir_fd=owned.parent, follow_symlinks=False)
    held = os.fstat(owned.root)
    if entry_identity(current) != entry_identity(held): raise vm.Refused('Runtime root changed before mutation')
    vm.no_symlinks(path)
    visible = path.stat()
    if (visible.st_dev, visible.st_ino) != (held.st_dev, held.st_ino): raise vm.Refused('Runtime root path changed before mutation')


def close_runtime_anchor(anchor, primary=None):
    """Consume both anchor slots before one close attempt per original integer."""
    scope = DescriptorScope()
    # Reverse finalization order closes root first, then independently parent.
    for handle in dict.fromkeys((getattr(anchor, 'parent', None), getattr(anchor, 'root', None))):
        if handle is not None: scope.own(handle)
    anchor.root = None
    anchor.parent = None
    before = len(getattr(primary, 'cleanup_errors', ())) if primary is not None else 0
    try: scope.close_all(primary, phase='runtime anchor close')
    except BaseException as error:
        anchor.close_failure = vm.failure_payload(error)
        raise
    if primary is not None:
        errors = list(getattr(primary, 'cleanup_errors', ()))[before:]
        if errors:
            anchor.close_failure = {'cleanup_errors': errors}
            return False
    return True


def close_dependency_anchor(destination, owned, primary=None, *, namespace_status='retained'):
    closed = False
    try:
        closed = close_runtime_anchor(owned, primary)
        return closed
    finally:
        if not closed:
            DEPENDENCY_CLOSE_DEBT[destination] = {
                'namespace_status': namespace_status,
                'anchor_slots_consumed': owned.root is None and owned.parent is None,
                'close_outcome': 'uncertain',
                'failure': getattr(owned, 'close_failure', None),
                'retry': 'Do not retry consumed descriptor integers; preserve this receipt and inspect the close failure'}

class RuntimeFixture:
    """One creation-held anchor and an exact acquired namespace, never a rescan grant."""
    def __init__(self, path):
        self.path = path
        try: self.owned = vm.OwnedDirectory(path)
        except BaseException as error:
            acquisition_debt(error, 'fixture anchor acquisition', path)
            raise
        self.entries = {}

    def remember(self, name, *, dynamic=False):
        self.entries[name] = (dynamic, tree_manifest(self.path / name))

    def require_root(self):
        require_held_root(self.path, self.owned)

    def mkdir(self, name, *, mode=0o700, dynamic=False):
        self.require_root()
        path = self.path / name
        scope = DescriptorScope()
        first = None
        try:
            os.mkdir(name, mode, dir_fd=self.owned.root)
            receipt = os.stat(name, dir_fd=self.owned.root, follow_symlinks=False)
            self.entries[name] = (dynamic, {'.': [*entry_identity(receipt), None]})
            handle = scope.own(open_directory_receipt(self.owned.root, name, receipt))
            if os.listdir(handle): raise vm.Refused('New owned directory is not empty')
            self.require_root()
        except BaseException as error:
            first = error
            acquisition_debt(error, 'directory receipt acquisition', path)
            raise
        finally: scope.close_all(first)

    def chmod(self, name, relative, mode):
        self.require_root()
        expected = self.entries[name][1][str(relative)]
        scope = DescriptorScope()
        first = None
        try:
            current = scope.own(os.dup(self.owned.root))
            pieces = [name, *relative.parts] if str(relative) != '.' else [name]
            location = Path('.')
            for index, part in enumerate(pieces):
                following = scope.own(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current))
                if index: location /= part
                proof = self.entries[name][1][str(location)]
                info = os.fstat(following)
                if [info.st_dev, info.st_ino, info.st_mode] != proof[:3]:
                    raise vm.Refused('Owned directory changed before chmod')
                # Both descriptors are enrolled before closing the parent.
                scope.close(current)
                current = following
            self.require_root()
            try: os.fchmod(current, mode)
            except BaseException as error:
                first = error
                raise
            finally:
                try:
                    info = os.fstat(current)
                    if stat.S_IMODE(info.st_mode) in (stat.S_IMODE(expected[2]), mode): expected[2] = info.st_mode
                except BaseException as error:
                    if first is None: raise
                    attach_cleanup(first, [{'phase': 'owned chmod receipt', 'failure': vm.failure_payload(error)}])
        except BaseException as error:
            first = error
            raise
        finally: scope.close_all(first, phase='owned chmod descriptor close')

    def write(self, name, payload):
        self.require_root()
        previous = self.entries.get(name)
        flags = os.O_RDWR | os.O_NOFOLLOW
        flags |= 0 if previous else os.O_CREAT | os.O_EXCL
        scope = DescriptorScope()
        first, written, registered, truncated = None, 0, False, False
        try:
            handle = scope.own(os.open(name, flags, 0o644, dir_fd=self.owned.root))
            info = require_private_file(handle)
            if previous:
                if [info.st_dev, info.st_ino, info.st_mode] != previous[1]['.'][:3]:
                    raise vm.Refused('Owned file replaced before truncate')
                digest = hashlib.sha256()
                while True:
                    block = os.read(handle, 65536)
                    if not block: break
                    digest.update(block)
                if digest.hexdigest() != previous[1]['.'][3]: raise vm.Refused('Owned file content changed before truncate')
            self.entries[name] = (False, {'.': [info.st_dev, info.st_ino, info.st_mode,
                previous[1]['.'][3] if previous else hashlib.sha256(b'').hexdigest()]})
            registered = True
            self.require_root()
            require_private_file(handle)
            os.ftruncate(handle, 0)
            os.lseek(handle, 0, os.SEEK_SET)
            truncated = True
            while written < len(payload):
                require_private_file(handle)
                count = os.write(handle, payload[written:])
                if count <= 0: raise OSError('Short owned runtime file write')
                written += count
        except BaseException as error:
            first = error
            if not registered and not previous: acquisition_debt(error, 'file receipt acquisition', self.path / name)
            raise
        finally:
            if registered and truncated: self.entries[name][1]['.'][3] = hashlib.sha256(payload[:written]).hexdigest()
            scope.close_all(first, phase='owned runtime file close')

    def validate(self):
        vm.no_symlinks(self.path)
        current = self.path.stat()
        held = os.fstat(self.owned.root)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise vm.Refused('Runtime fixture root changed; preserved: ' + str(self.path))
        if set(os.listdir(self.owned.root)) != set(self.entries):
            raise vm.Refused('Unknown runtime fixture namespace; preserved: ' + str(self.path))
        for name, (dynamic, expected) in self.entries.items():
            if dynamic:
                info = (self.path / name).lstat()
                if [info.st_dev, info.st_ino, info.st_mode] != expected['.'][:3]:
                    raise vm.Refused('Runtime output identity changed')
            elif tree_manifest(self.path / name) != expected:
                raise vm.Refused('Runtime input changed; preserved: ' + name)

    def remove(self):
        self.validate()
        self.owned.remove()

    def close(self):
        close_runtime_anchor(self.owned)


def cleanup_dependencies(destination):
    owned = DEPENDENCY_OWNERS.get(destination)
    if owned is None: raise vm.Refused('Missing creation-held dependency identity; manual exact debt review required')
    removed, first = False, None
    try:
        if owned.root is None: raise vm.Refused('Missing open creation-held dependency identity; manual exact debt review required')
        current = destination.stat()
        held = os.fstat(owned.root)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise vm.Refused('Test dependency root changed; preserved')
        validate_dependencies(destination)
        owned.remove()
        removed = True
    except BaseException as error:
        first = error
        raise
    finally:
        closed = False
        try: closed = close_dependency_anchor(destination, owned, first, namespace_status='removed' if removed else 'retained')
        finally:
            if closed and removed:
                # Drop ownership only after both anchor close attempts completed.
                for registry in (DEPENDENCY_TREES, DEPENDENCY_OWNERS, DEPENDENCY_DIRECTORIES,
                        DEPENDENCY_IDENTITIES, DEPENDENCY_DIRECTORY_IDENTITIES, DEPENDENCY_FILE_IDENTITIES,
                        DEPENDENCY_CLOSE_DEBT): registry.pop(destination, None)


def _finish_runtime(result, report, owner, fixture, dependencies=None, consumers=(), primary=None,
                   volume=None, ext4_profile=None, ext4_acquired=False, host_safe=True, proof_complete=True):
    """Publish causal proof, fence consumers, remove exact fixtures, publish receipt."""
    failures = []
    attempts = []
    body_primary = primary
    cancellation = None
    anchor_primary = None
    def record_cancellation(error):
        nonlocal primary, cancellation
        if isinstance(error, (KeyboardInterrupt, SystemExit)) and primary is None:
            primary = cancellation = error
        if primary is not None:
            result.update(status='failed', failure=vm.failure_payload(primary), error=str(primary))
    def attempt(phase, operation, retry=False):
        for number in range(2 if retry else 1):
            try:
                operation()
                attempts.append({'phase': phase, 'attempt': number + 1, 'status': 'removed'})
                return True
            except BaseException as error:
                row = {'phase': phase, 'attempt': number + 1, 'failure': vm.failure_payload(error)}
                attempts.append(row)
                record_cancellation(error)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    failures.append(row)
                    return False
                if number == (1 if retry else 0): failures.append(row)
        return False
    absent = host_safe
    if not host_safe: failures.append({'phase': 'container build', 'failure': {'error': 'Docker build completion unknown; preserve source context'}})
    result.update(fixture=str(fixture.path) if fixture else result.get('fixture'), cleanup_attempts=attempts,
                  cleanup_errors=failures, cleanup={'status': 'retained',
                  'dependencies': {'path': str(dependencies), 'status': 'retained'} if dependencies else None})
    if dependencies is not None:
        debt = result['cleanup']['dependencies']
        debt.update(identity=DEPENDENCY_IDENTITIES.get(dependencies), expected_files=dict(DEPENDENCY_TREES.get(dependencies, {})),
            expected_directories=sorted(DEPENDENCY_DIRECTORIES.get(dependencies, ())),
            file_identities=dict(DEPENDENCY_FILE_IDENTITIES.get(dependencies, {})),
            directory_identities={name: entry_identity(info) for name, info in DEPENDENCY_DIRECTORY_IDENTITIES.get(dependencies, {}).items()})
        debt['retry'] = 'Compare original identity and expected manifest before retrying only ' + str(dependencies) + '; resolve changed/foreign entries without adopting replacements'
    if fixture is not None:
        info = os.fstat(fixture.owned.root)
        result['cleanup']['identity'] = [info.st_dev, info.st_ino]
        result['cleanup']['input_manifest'] = {name: proof for name, (dynamic, proof) in fixture.entries.items() if not dynamic}
    if primary is not None:
        result.update(status='failed', failure=vm.failure_payload(primary), error=str(primary))
        if fixture is None and result.get('fixture') and os.path.lexists(result['fixture']):
            acquisition_debt(primary, 'unanchored runtime root', result['fixture'])
        result['acquisition_debt'] = list(getattr(primary, 'acquisition_debt', ()))
        result['failure'] = vm.failure_payload(primary)
    result['cleanup']['consumers'] = [{'identity': name, 'status': 'retained'} for name in dict.fromkeys(consumers)]
    result['cleanup']['acquisition_debt'] = result.get('acquisition_debt', [])
    result['cleanup']['volume'] = {'name': volume, 'status': 'retained'} if volume else None
    result['cleanup']['ext4'] = {'run': owner, 'status': 'retained'} if ext4_acquired else None
    if not proof_complete:
        failures.append({'phase': 'runtime proof export', 'failure': {'error': 'Required output export incomplete; retain exact output storage and binds'}})
    execution_status = result.get('status', 'failed')
    result['execution_status'] = execution_status
    result['status'] = 'finalizing'
    fallback = vm.LOCAL / ('runtime-cleanup-' + owner + '.json')
    durable = None
    def publish(path, phase):
        try:
            vm.write_report(path, result)
            return True
        except BaseException as error:
            record_cancellation(error)
            failures.append({'phase': phase, 'failure': vm.failure_payload(error)})
            return False
    closed = False
    def close_handles():
        nonlocal closed, primary, anchor_primary
        if closed: return
        closed = True
        for phase, handle in [('host fixture close', fixture), ('dependency close', DEPENDENCY_OWNERS.get(dependencies))]:
            if handle is not None:
                try:
                    if isinstance(handle, RuntimeFixture): handle.close()
                    else: close_dependency_anchor(dependencies, handle)
                except BaseException as error:
                    record_cancellation(error)
                    if primary is None:
                        primary = anchor_primary = error
                        record_cancellation(error)
                    failures.append({'phase': phase, 'failure': vm.failure_payload(error)})
    try:
        if publish(report, 'proof report'): durable = report
        elif report != fallback and publish(fallback, 'fallback proof report'): durable = fallback
        if durable and proof_complete and host_safe and cancellation is None:
            for row in reversed(result['cleanup']['consumers']):
                container = row['identity']
                def remove(container=container):
                    vm.remove_owned(container, owner)
                    if vm.owned_container(container, owner): raise vm.Refused('Runtime consumer remains: ' + container)
                removed = attempt('container ' + container, remove, retry=True)
                row['status'] = 'removed' if removed else 'retained'
                absent = removed and absent
                if cancellation is not None: break
        if durable and absent and proof_complete and cancellation is None and not any(row['phase'].startswith(('container ', 'tmpfs', 'ext4')) for row in failures):
            if volume and absent:
                def remove_volume():
                    inspection = vm.docker('volume', 'inspect', volume, check=False)
                    if inspection.returncode:
                        if b'no such volume' in inspection.stderr.lower(): return
                        raise vm.Refused('Tmpfs inspection failed')
                    info = json.loads(inspection.stdout)[0]
                    if info.get('Labels', {}).get('ha-ops.dev.run') != owner: raise vm.Refused('Tmpfs owner changed before cleanup')
                    vm.docker('volume', 'rm', volume)
                    inspection = vm.docker('volume', 'inspect', volume, check=False)
                    if inspection.returncode == 0 or b'no such volume' not in inspection.stderr.lower():
                        raise vm.Refused('Tmpfs volume absence not proven')
                if attempt('tmpfs volume', remove_volume, retry=True): result['cleanup']['volume']['status'] = 'removed'
            if ext4_acquired and absent and not any(row['phase'].startswith(('container ', 'tmpfs', 'ext4')) for row in failures):
                def remove_ext4():
                    result['ext4_cleanup'] = vm.ext4_guest('cleanup', owner, ext4_profile)
                    result['ext4_cleanup_repeat'] = vm.ext4_guest('cleanup', owner, ext4_profile)
                if attempt('ext4 fixture', remove_ext4, retry=True): result['cleanup']['ext4']['status'] = 'removed'
            if dependencies is not None and not any(row['phase'].startswith(('tmpfs', 'ext4')) for row in failures):
                removed_dependencies = attempt('test dependencies', lambda: cleanup_dependencies(dependencies))
                result['cleanup']['dependencies']['status'] = 'removed' if removed_dependencies else 'refused'
            if fixture is not None and not any(row['phase'].startswith(('tmpfs', 'ext4')) for row in failures):
                removed = attempt('host fixture', fixture.remove)
                result['cleanup']['status'] = 'removed' if removed else 'refused'
            elif not result.get('fixture') or not Path(result['fixture']).exists(): result['cleanup']['status'] = 'not-created'
        if result.get('fixture') and result['cleanup']['status'] == 'retained':
            result['cleanup']['retry'] = 'Resolve reported consumer/proof/ownership debt, then retry only exact fixture ' + result['fixture']
        close_handles()
        if fixture is not None and getattr(fixture.owned, 'close_failure', None) is not None:
            result['cleanup']['anchor_close'] = {'outcome': 'uncertain',
                'slots_consumed': fixture.owned.root is None and fixture.owned.parent is None,
                'failure': fixture.owned.close_failure}
        if dependencies in DEPENDENCY_CLOSE_DEBT and result['cleanup']['dependencies'] is not None:
            debt = DEPENDENCY_CLOSE_DEBT[dependencies]
            result['cleanup']['dependencies'].update(close_debt=debt, close_status='failed')
            if debt['namespace_status'] == 'removed': result['cleanup']['dependencies']['status'] = 'removed'
        result['status'] = 'failed' if failures else execution_status
        result['cleanup_errors'] = failures
        if durable:
            result['proof_report'] = str(durable)
            if not publish(durable, 'final cleanup report'):
                result['status'] = 'failed'
        if cancellation is not None and body_primary is None:
            attach_cleanup(cancellation, failures)
            raise cancellation
        if anchor_primary is not None and body_primary is None:
            attach_cleanup(anchor_primary, failures)
            raise anchor_primary
        if failures:
            if primary is not None: attach_cleanup(primary, failures)
            else:
                error = vm.Refused('Runtime cleanup/report failed; retained debt: ' + repr(failures))
                attach_cleanup(error, failures)
                raise error
    finally:
        active = sys.exc_info()[1] or primary
        count = len(failures)
        close_handles()
        if active is not None: attach_cleanup(active, failures[count:])



def finish_runtime(result, report, owner, fixture, dependencies=None, consumers=(), primary=None,
                   volume=None, ext4_profile=None, ext4_acquired=False, host_safe=True, proof_complete=True):
    active = primary
    try:
        return _finish_runtime(result, report, owner, fixture, dependencies, consumers, primary,
            volume, ext4_profile, ext4_acquired, host_safe, proof_complete)
    except BaseException as error:
        if active is None:
            active = error
            raise
        if error is not active: attach_cleanup(active, [{'phase': 'unexpected runtime finalization', 'failure': vm.failure_payload(error)}])
    finally:
        for handle in (fixture, DEPENDENCY_OWNERS.get(dependencies)):
            anchor = handle.owned if isinstance(handle, RuntimeFixture) else handle
            if anchor is not None and (anchor.root is not None or anchor.parent is not None):
                try:
                    if isinstance(handle, RuntimeFixture): close_runtime_anchor(anchor)
                    else: close_dependency_anchor(dependencies, anchor)
                except BaseException as error:
                    if active is None: raise
                    attach_cleanup(active, [{'phase': 'remaining runtime descriptor close', 'failure': vm.failure_payload(error)}])

def external_report(report, root):
    path = Path(os.path.abspath(report))
    vm.no_symlinks(path)
    if path == root or root in path.parents:
        raise vm.Refused('Runtime report must be outside disposable fixture: ' + str(root))
    return path




def close_copy_handles(handles, primary=None, *, phase='copy descriptor close'):
    first = primary
    failures = []
    for handle in handles:
        if handle is not None:
            try: os.close(handle)
            except BaseException as error:
                if first is None: first = error
                else: failures.append({'phase': phase, 'failure': vm.failure_payload(error)})
    if first is not None: attach_cleanup(first, failures)
    if primary is None and first is not None: raise first

def copy_into_fixture(source, destination, snapshot, fixture):
    """Clone reviewed source descriptors into the original held fixture root."""
    fixture.require_root()
    if snapshot['source'] != source or vm.mount_points() != snapshot['mounts']:
        raise vm.Refused('Copy snapshot or mounts changed')
    vm._reject_mount_crossings(source, snapshot['mounts'])
    name = destination.name
    fixture.mkdir(name)
    manifest = fixture.entries[name][1]

    def copy_entry(source_fd, parent_fd, leaf, relative):
        fixture.require_root()
        kind, signature = snapshot['records'][relative]
        if vm._input_signature(os.fstat(source_fd)) != signature: raise vm.Refused('Copy source identity changed')
        directory = kind == 'directory'
        receipt = None
        if directory:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            receipt = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            manifest[str(Path(*relative))] = [*entry_identity(receipt), None]
        flags = os.O_RDONLY | os.O_DIRECTORY if directory else os.O_WRONLY | os.O_CREAT | os.O_EXCL
        key = str(Path(*relative))
        first = None
        digest = hashlib.sha256()
        registered = False
        scope = DescriptorScope()
        try:
            target = scope.own(open_directory_receipt(parent_fd, leaf, receipt) if directory else os.open(leaf, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd))
            info = os.fstat(target) if directory else require_private_file(target)
            manifest[key] = [info.st_dev, info.st_ino, info.st_mode, None if directory else hashlib.sha256(b'').hexdigest()]
            registered = True
            if directory:
                if os.listdir(target): raise vm.Refused('New copy directory is not empty')
                scan(source_fd, target, relative)
            else:
                while True:
                    block = os.read(source_fd, 1024 * 1024)
                    if not block: break
                    offset = 0
                    while offset < len(block):
                        fixture.require_root()
                        require_private_file(target)
                        count = os.write(target, block[offset:])
                        if count <= 0: raise OSError('Short fixture copy write')
                        digest.update(block[offset:offset + count]); offset += count
                if vm._input_signature(os.fstat(source_fd)) != signature: raise vm.Refused('Copy source changed during read')
                require_private_file(target)
                os.fchmod(target, signature[2] & 0o777)
        except BaseException as error:
            first = error
            raise
        finally:
            receipt_error = None
            try:
                if registered:
                    if not directory: manifest[key][3] = digest.hexdigest()
                    manifest[key][2] = os.fstat(target).st_mode
            except BaseException as error:
                if first is None: receipt_error = error
                else: attach_cleanup(first, [{'phase': 'copy entry receipt', 'failure': vm.failure_payload(error)}])
            scope.close_all(first or receipt_error)
            if receipt_error is not None: raise receipt_error

    def scan(source_fd, target_fd, relative):
        if vm._visible_children(source_fd) != snapshot['children'][relative]: raise vm.Refused('Copy source namespace changed')
        for leaf in snapshot['children'][relative]:
            location = relative + (leaf,)
            scope = DescriptorScope()
            first = None
            try:
                child = scope.own(vm._open_input_child(source_fd, leaf, snapshot['records'][location]))
                copy_entry(child, target_fd, leaf, location)
            except BaseException as error:
                first = error
                raise
            finally: scope.close_all(first)
        if vm._visible_children(source_fd) != snapshot['children'][relative]: raise vm.Refused('Copy source changed during scan')

    scope = DescriptorScope()
    first = None
    try:
        source_fd = scope.own(vm._open_input(source))
        if vm._input_signature(os.fstat(source_fd)) != snapshot['records'][()][1]: raise vm.Refused('Copy root source changed')
        target_fd = scope.own(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fixture.owned.root))
        info = os.fstat(target_fd)
        if [info.st_dev, info.st_ino, info.st_mode] != manifest['.'][:3]: raise vm.Refused('Copy destination root changed')
        scan(source_fd, target_fd, ())
    except BaseException as error:
        first = error
        raise
    finally:
        scope.close_all(first)


def copy_preserving_modes(source, destination, snapshot, *, fixture=None):
    try:
        if fixture is not None: fixture.require_root()
        if fixture is None: vm.copy_input(source, destination, expected=snapshot)
        else: copy_into_fixture(source, destination, snapshot, fixture)
        # Capture successful copy ownership before any mode restoration.
        for location, (kind, signature) in snapshot['records'].items():
            if kind == 'directory':
                if fixture is None: destination.joinpath(*location).chmod(signature[2] & 0o777)
                else: fixture.chmod(destination.name, Path(*location), signature[2] & 0o777)
    except BaseException as error:
        if os.path.lexists(destination) and (fixture is None or destination.name not in fixture.entries):
            acquisition_debt(error, 'partial input acquisition', destination)
        raise


def test_accounting(xml, *, expected=None, ext4_profile=None, recreated=False):
    root = ET.fromstring(xml)
    cases = list(root.iter('testcase'))
    skips, failures, passed, case_errors = [], [], [], []
    for case in cases:
        module, _, classes = case.get('classname', '').partition('.')
        identity = module + '.py' + ('::' + classes.replace('.', '::') if classes else '') + '::' + case.get('name', '')
        skipped = case.find('skipped')
        if sum(case.find(name) is not None for name in ('skipped', 'failure', 'error')) > 1:
            case_errors.append('Contradictory testcase outcomes: ' + identity)
        if skipped is not None:
            skips.append({'id': identity, 'reason': skipped.get('message', '')})
        elif case.find('failure') is not None or case.find('error') is not None: failures.append(identity)
        else: passed.append(identity)
    def declared_skip(item):
        if item['reason'] == 'root SSH fixture; independent graph-loss recovery is qualified through HTTPS':
            return item['id'] == 'test_native_scope.py::test_scope_authenticated_network_publication_and_same_commit_recovery[ssh-accepted-readback-loss]'
        if ext4_profile and item['id'].startswith('test_native_ext4.py::'):
            name = item['id'].split('::', 1)[1]
            profiles = {
                'test_loose_index_worktree_save': 'allocation', 'test_existing_reflog_complete_atomic_overlap': 'allocation',
                'test_native_publication_physical_boundaries': 'allocation', 'test_control_atomic_overlap': 'allocation',
                'test_independent_recovery_copies': 'allocation', 'test_no_reuse_repack': 'allocation',
                'test_short_and_eio_boundaries': 'allocation', 'test_network_publication_ext4_frozen_physical_peak': 'allocation',
                'test_kernel_byte_enospc': 'bytes', 'test_network_graph_fetch_real_kernel_enospc_after_admission': 'bytes',
                'test_kernel_inode_enospc': 'inodes'}
            expected = profiles.get(name)
            return expected is not None and ((expected != ext4_profile and item['reason'] == 'different bounded ext4 profile') or
                (expected == ext4_profile and recreated and item['reason'] == 'recreation verifies retained filesystem; original matrix retained'))
        return False
    missing = [item for item in skips if not declared_skip(item)]
    identities = [case.get('classname', '').partition('.')[0] + '.py' +
        ('::' + case.get('classname', '').partition('.')[2].replace('.', '::') if '.' in case.get('classname', '') else '') +
        '::' + case.get('name', '') for case in cases]
    from collections import Counter
    duplicates = sorted(name for name, count in Counter(identities).items() if count != 1)
    suite_errors = list(case_errors)
    for suite in root.iter('testsuite'):
        direct = list(suite.findall('testcase'))
        for attribute, count in [('tests', len(direct)), ('failures', sum(case.find('failure') is not None for case in direct)),
                ('errors', sum(case.find('error') is not None for case in direct)), ('skipped', sum(case.find('skipped') is not None for case in direct))]:
            declared = suite.get(attribute)
            if declared is None and attribute != 'tests': continue
            if declared is None or not declared.isdigit() or int(declared) != count:
                suite_errors.append('Inconsistent testsuite ' + attribute)
    if root.tag == 'testsuites' and root.get('tests') is not None and root.get('tests') != str(len(cases)):
        suite_errors.append('Inconsistent testsuites tests')
    if expected is None:
        missing.append({'id': '*', 'reason': 'Reviewed required case manifest missing'})
        absent, extra = [], []
    else:
        if len(expected) != len(set(expected)): raise vm.Refused('Duplicate reviewed required case identity')
        absent = sorted(set(expected) - set(identities))
        extra = sorted(set(identities) - set(expected))
        missing.extend({'id': name, 'reason': 'Required case absent'} for name in absent)
    missing.extend({'id': name, 'reason': 'Duplicate case'} for name in duplicates)
    missing.extend({'id': name, 'reason': 'Unexpected case'} for name in extra)
    missing.extend({'id': '*', 'reason': error} for error in suite_errors)
    if not cases: missing.append({'id': '*', 'reason': 'No tests executed'})
    return {'collected': len(cases), 'passed': len(passed), 'failed': failures, 'skipped': skips,
            'declared_unexecuted': [item for item in skips if declared_skip(item)],
            'required_unexecuted': missing, 'passed_ids': passed, 'missing_ids': absent, 'extra_ids': extra, 'duplicate_ids': duplicates, 'suite_errors': suite_errors}


def resolve(arch):
    vm.ready(for_build=True)
    cache = CACHE / arch
    cache.mkdir(parents=True, exist_ok=True)
    vm.verify_source(cache)
    base = BASES[arch]
    native_arch = {'linux/arm64': 'aarch64', 'linux/amd64': 'amd64'}[vm.guest_platform()]
    resolve_base = BASES[native_arch]
    vm.docker('pull', resolve_base)
    keys = ''
    if arch != native_arch:
        # Extract public signing keys from the immutable foreign base without
        # executing foreign code or enabling emulation.
        vm.docker('pull', '--platform', 'linux/amd64' if arch == 'amd64' else 'linux/arm64', base)
        owner = uuid.uuid4().hex
        name = 'ha-ops-runtime-keys-' + owner
        container = vm.docker('create', '--name', name, '--label', 'ha-ops.dev.run=' + owner,
            '--network', 'none', '--read-only', '--entrypoint', '/bin/true', base).stdout.decode().strip()
        try: vm.docker('cp', container + ':/etc/apk/keys', str(cache / 'keys'))
        finally: vm.remove_owned(container, owner)
        keys = ' --keys-dir /out/keys'
    # apk resolves and verifies the repository metadata. Installation later
    # checks archive SHA256 and APK signatures, entirely offline.
    apk = 'apk --arch ' + {'aarch64': 'aarch64', 'amd64': 'x86_64'}[arch] + keys
    script = (apk + ' update >/dev/null; ' + apk + ' fetch --recursive --url ' + PACKAGES +
              ' > /out/urls.txt; ' + apk + ' fetch --recursive --output /out ' + PACKAGES)
    result = vm.docker('run', '--rm', '--entrypoint', '/bin/sh', '--mount',
        f'type=bind,source={cache},target=/out', resolve_base, '-ec', script)
    sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr)
    urls = [line.strip() for line in (cache / 'urls.txt').read_text().splitlines()
            if line.startswith('https://') and line.endswith('.apk')]
    if not urls or len(urls) != len(set(urls)):
        raise vm.Refused('Missing or duplicate APK closure URLs')
    lines = []
    for url in sorted(urls):
        archive = cache / url.rsplit('/', 1)[1]
        if not archive.is_file(): raise vm.Refused('Resolved archive absent: ' + archive.name)
        lines.append(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + url)
    (APP / ('runtime-apk-' + arch + '.lock')).write_text('\n'.join(lines) + '\n')
    print(json.dumps({'arch': arch, 'base': base, 'archives': len(lines), 'cache': str(cache)}))


def build():
    vm.ready(for_build=True)
    platform = vm.guest_platform()
    arch = {'linux/arm64': 'aarch64', 'linux/amd64': 'amd64'}[platform]
    # The exact installable context is snapshotted before Docker sees it.
    snapshot = vm.snapshot_input(APP)
    run = vm.RUNS / ('runtime-build-' + uuid.uuid4().hex)
    fixture = None
    first = None
    status = {'status': 'started', 'fixture': str(run)}
    build_started = build_finished = False
    try:
        run.mkdir(parents=True)
        fixture = RuntimeFixture(run)
        source = run / 'input'
        copy_preserving_modes(APP, source, snapshot, fixture=fixture)
        vm.verify_source(source)
        image = 'ha-ops-runtime:' + snapshot['digest'][:20] if 'digest' in snapshot else 'ha-ops-runtime:' + uuid.uuid4().hex[:20]
        build_started = True
        result = vm.docker('build', '--platform', platform, '--build-arg', 'BUILD_ARCH=' + arch,
            '--tag', image, str(source), timeout=1200, check=False)
        build_finished = True
        status.update(stdout=result.stdout.decode(errors='replace'), stderr=result.stderr.decode(errors='replace'), returncode=result.returncode)
        sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr)
        if result.returncode: raise vm.Refused('Original App image build failed; see complete build output')
        image_id = vm.docker('image', 'inspect', image, '--format', '{{.Id}}').stdout.decode().strip()
        vm.verify_image_arch(image_id, platform)
        print(json.dumps({'image': image, 'image_id': image_id, 'arch': arch, 'base': BASES[arch]}))
        app_manifest = {str(path.relative_to(source / 'app')): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (source / 'app').rglob('*') if path.is_file() and '__pycache__' not in path.parts}
        (vm.LOCAL / 'runtime-image.json').write_text(json.dumps({'image': image, 'image_id': image_id, 'arch': arch, 'base': BASES[arch],
            'app_manifest': app_manifest}, indent=2) + '\n')
        status['status'] = 'passed'
    except BaseException as error:
        first = error
        raise
    finally:
        finish_runtime(status, vm.LOCAL / (run.name + '.json'), run.name.removeprefix('runtime-build-'), fixture,
                       primary=first, host_safe=not build_started or build_finished)


def resolve_pip():
    vm.ready(for_build=True)
    cache = CACHE / 'pip'
    cache.mkdir(parents=True, exist_ok=True)
    vm.verify_source(cache)
    image = vm.image_name()
    packages = ['rfc8785==0.1.4', 'pytest==8.4.2', 'pytest-xdist==3.8.0', 'execnet==2.1.1',
                'pluggy==1.6.0', 'packaging==25.0', 'iniconfig==2.1.0', 'pygments==2.19.2']
    vm.docker('run', '--rm', '--mount', f'type=bind,source={cache},target=/out', image,
        'python3', '-m', 'pip', 'download', '--python-version', '3.14', '--no-deps', '--dest', '/out', *packages)
    production, test = [], []
    for arch in ('aarch64', 'x86_64'):
        filename = 'annotatedyaml-1.0.2-cp314-cp314-musllinux_1_2_' + arch + '.whl'
        url = 'https://wheels.home-assistant.io/musllinux-index/' + filename
        vm.docker('run', '--rm', '--mount', f'type=bind,source={cache},target=/out', image,
            'python3', '-m', 'pip', 'download', '--python-version', '3.14', '--platform', 'musllinux_1_2_' + arch,
            '--no-deps', '--dest', '/out', url)
        digest = hashlib.sha256((cache / filename).read_bytes()).hexdigest()
        production.append(f'annotatedyaml @ {url} ; platform_machine == "{arch}" --hash=sha256:{digest}')
    for archive in sorted(cache.iterdir()):
        name, version = archive.name.removesuffix('.tar.gz').split('-')[:2]
        if name == 'annotatedyaml': continue
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        row = f'{name}=={version} --hash=sha256:{digest}'
        (production if name in ('annotatedyaml', 'rfc8785') else test).append(row)
    if len(production) != 3 or len(test) != 7: raise vm.Refused('Unexpected wheel closure')
    (APP / 'runtime-pip.lock').write_text('\n'.join(production) + '\n')
    path = vm.HERE / 'runtime-toolchain.json'
    metadata = json.loads(path.read_text()) if path.exists() else {}
    metadata['pytest_wheels'] = test
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')


def runtime_image():
    data = json.loads((vm.LOCAL / 'runtime-image.json').read_text())
    image = data['image_id']
    if vm.docker('image', 'inspect', image, '--format', '{{.Id}}').stdout.decode().strip() != image:
        raise vm.Refused('Original App image identity changed')
    vm.verify_image_arch(image, vm.guest_platform())
    return data


def probe():
    vm.ready()
    data = runtime_image()
    script = 'import json,sys;sys.path.insert(0,"/app");import native_runtime;print(json.dumps(native_runtime.observe(),sort_keys=True))'
    result = vm.docker('run', '--rm', '--network', 'none', '--read-only', '--entrypoint', '/usr/bin/python3',
        data['image_id'], '-c', script)
    observed = json.loads(result.stdout)
    vm.write_report(vm.LOCAL / 'runtime-observed.json', observed)
    print(json.dumps(observed, indent=2, sort_keys=True))


def diagnose():
    vm.ready()
    data = runtime_image()
    script = '''import json,os,pathlib,subprocess,tempfile
def git(*args):
 r=subprocess.run(['/usr/bin/git',*args],capture_output=True,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'});r.check_returncode();return r.stdout
with tempfile.TemporaryDirectory() as t:
 p=pathlib.Path(t);git('init',str(p/'repo'));git('-C',str(p/'repo'),'-c','user.name=Fixture','-c','user.email=f@invalid','commit','--allow-empty','-qm','fixture')
 git('-C',str(p/'repo'),'worktree','add','--detach','--no-checkout','--lock','--reason','fixture',str(p/'tree'),'HEAD')
 print(json.dumps({'worktree_admin':[str(x.relative_to(p/'repo'/'.git'/'worktrees')) for x in (p/'repo'/'.git'/'worktrees').rglob('*')],'passwd':pathlib.Path('/etc/passwd').read_text()}))
'''
    result = vm.docker('run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp',
        '--entrypoint', '/usr/bin/python3', data['image_id'], '-c', script)
    print(result.stdout.decode())


def check_lock_failure(report):
    """Prove bundle, closure and APK failures offline before package install."""
    vm.ready()
    arch = {'linux/arm64': 'aarch64', 'linux/amd64': 'amd64'}[vm.guest_platform()]
    owner = uuid.uuid4().hex
    root = vm.RUNS / ('runtime-negative-' + owner)
    report = external_report(report, root)
    fixture = None
    first = None
    consumers, rows = [], []
    result_report = {'status': 'started', 'cases': rows, 'base': BASES[arch], 'fixture': str(root)}
    try:
        root.mkdir(parents=True)
        fixture = RuntimeFixture(root)
        script = root / 'runtime-install.sh'
        original = (APP / 'runtime-install.sh').read_text()
        for kind in ('bundle-checksum', 'checksum', 'missing', 'extra', 'duplicate', 'malformed'):
            content = b'reviewed-package-fixture'
            package_hash = hashlib.sha256(content).hexdigest()
            lock = root / ('runtime-apk-' + arch + '.lock')
            fixture.write(lock.name, ((('0' * 64 if kind == 'checksum' else package_hash) +
                '  https://dl-cdn.alpinelinux.org/alpine/v3.24/main/' + arch + '/fixture-1.0-r0.apk\n')).encode())
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                names = [] if kind == 'missing' else ['fixture-1.0-r0.apk']
                if kind == 'extra': names.append('extra-1.0-r0.apk')
                if kind == 'duplicate': names.append('fixture-1.0-r0.apk')
                for member in names:
                    info = tarfile.TarInfo(member); info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            bundle = b'invalid archive' if kind == 'malformed' else stream.getvalue()
            fixture.write('bundle.tar.gz', bundle)
            digest = '0' * 64 if kind == 'bundle-checksum' else hashlib.sha256(bundle).hexdigest()
            changed = re.sub(r'bundle_sha=[a-f0-9]{64}', 'bundle_sha=' + digest, original)
            # Offline transport and tripwires are disposable proof inputs only.
            prelude = "wget() { cp /fixture/bundle.tar.gz \"$3\"; }; apk() { echo UNEXPECTED_APK; exit 90; }; pip3() { echo UNEXPECTED_PIP; exit 91; };\n"
            fixture.write(script.name, (prelude + changed).encode())
            vm.verify_source(root)
            name = 'ha-ops-runtime-negative-' + owner + '-' + kind
            consumers.append(name)
            result = vm.docker('run', '--rm', '--name', name, '--label', 'ha-ops.dev.run=' + owner, '--read-only', '--network', 'none', '--tmpfs', '/tmp', '--entrypoint', '/bin/sh',
                '--mount', f'type=bind,source={root},target=/fixture,readonly',
                '--mount', f'type=bind,source={lock},target=/tmp/{lock.name},readonly',
                BASES[arch], '/fixture/runtime-install.sh', arch, timeout=120, check=False)
            logs = (result.stdout + result.stderr).decode(errors='replace')
            expected = {'bundle-checksum': 'FAILED', 'checksum': 'FAILED',
                'missing': 'APK closure mismatch', 'extra': 'APK closure mismatch',
                'duplicate': 'Invalid runtime bundle entries', 'malformed': 'tar:'}[kind]
            if result.returncode == 0 or expected not in logs or 'UNEXPECTED_' in logs:
                raise vm.Refused('Pinned artifact negative proof did not fail closed: ' + kind + '\n' + logs)
            rows.append({'case': kind, 'returncode': result.returncode, 'logs': logs, 'network': 'none'})
        result_report['status'] = 'passed'
    except BaseException as error:
        first = error
        raise
    finally:
        finish_runtime(result_report, report, owner, fixture, consumers=consumers, primary=first)
    print(json.dumps({'status': result_report['status'], 'cases': [row['case'] for row in rows]}))


def pytest_dependencies():
    vm.no_symlinks(CACHE)
    destination = Path(tempfile.mkdtemp(prefix='pytest-owned-', dir=CACHE))
    owned = None
    try:
        owned = vm.OwnedDirectory(destination)
        DEPENDENCY_OWNERS[destination] = owned
        info = os.fstat(owned.root)
        DEPENDENCY_IDENTITIES[destination] = [info.st_dev, info.st_ino]
        expected_files = {}
        DEPENDENCY_TREES[destination] = expected_files
        DEPENDENCY_DIRECTORIES[destination] = set()
        DEPENDENCY_DIRECTORY_IDENTITIES[destination] = {}
        DEPENDENCY_FILE_IDENTITIES[destination] = {}
        require_held_root(destination, owned)
        os.fchmod(owned.root, 0o755)
        require_held_root(destination, owned)
        locks = json.loads((vm.HERE / 'runtime-toolchain.json').read_text())['pytest_wheels']
        for line in locks:
            package, expected = line.split(' --hash=sha256:')
            name, version = package.split('==')
            matches = list((CACHE / 'pip').glob(name + '-' + version + '-*.whl'))
            for match in matches: vm.no_symlinks(match)
            if len(matches) != 1 or hashlib.sha256(matches[0].read_bytes()).hexdigest() != expected:
                raise vm.Refused('Pure Python pytest artifact absent or changed: ' + package)
            with zipfile.ZipFile(matches[0]) as wheel:
                for member in wheel.namelist():
                    path = Path(member)
                    info = wheel.getinfo(member)
                    if path.is_absolute() or '..' in path.parts or path.parts[0].split('-')[0] not in (
                            'pytest', '_pytest', 'iniconfig', 'packaging', 'pluggy', 'pygments', 'pytest.py', 'py.py',
                            'xdist', 'pytest_xdist', 'execnet') or stat.S_ISLNK(info.external_attr >> 16) or re.search(r'\.(so(?:\..*)?|pyd|dll|dylib|pth)$', member):
                        raise vm.Refused('Test wheel contains unreviewed import path: ' + member)
                    if info.is_dir(): continue
                    if member in expected_files: raise vm.Refused('Conflicting test wheel member: ' + member)
                    payload = wheel.read(member)
                    require_held_root(destination, owned)
                    scope = DescriptorScope()
                    first = None
                    written, registered = 0, False
                    try:
                        parent_fd = scope.own(os.dup(owned.root))
                        relative = Path('.')
                        for part in path.parts[:-1]:
                            relative /= part
                            key = str(relative)
                            records = DEPENDENCY_DIRECTORY_IDENTITIES[destination]
                            if key not in records:
                                os.mkdir(part, 0o755, dir_fd=parent_fd)
                                records[key] = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                                DEPENDENCY_DIRECTORIES[destination].add(key)
                            following = scope.own(open_directory_receipt(parent_fd, part, records[key]))
                            scope.close(parent_fd)
                            parent_fd = following
                            require_held_root(destination, owned)
                        handle = scope.own(os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent_fd))
                        # The exclusive empty file and its FD are owned before parent release.
                        DEPENDENCY_FILE_IDENTITIES[destination][member] = entry_identity(require_private_file(handle))
                        expected_files[member] = hashlib.sha256(b'').hexdigest()
                        registered = True
                        scope.close(parent_fd)
                        while written < len(payload):
                            require_held_root(destination, owned)
                            require_private_file(handle)
                            count = os.write(handle, payload[written:])
                            if count <= 0: raise OSError('Short test dependency write')
                            written += count
                    except BaseException as error:
                        first = error
                        raise
                    finally:
                        if registered: expected_files[member] = hashlib.sha256(payload[:written]).hexdigest()
                        scope.close_all(first, phase='dependency file close')
        DEPENDENCY_TREES[destination] = expected_files
        require_held_root(destination, owned)
        validate_dependencies(destination)
        return destination
    except BaseException as error:
        if owned is None:
            acquisition_debt(error, 'dependency anchor acquisition', destination)
            raise
        try: cleanup_dependencies(destination)
        except BaseException as secondary:
            attach_cleanup(error, [{'phase': 'partial dependency cleanup', 'failure': vm.failure_payload(secondary), 'path': str(destination)}])
        raise


def validate_dependencies(destination):
    expected = DEPENDENCY_TREES.get(destination)
    if expected is None: raise vm.Refused('Unknown test dependency extraction; preserved for inspection')
    vm.no_symlinks(destination)
    owned = DEPENDENCY_OWNERS.get(destination)
    if owned is not None and owned.root is not None: require_held_root(destination, owned)
    actual = {}
    expected_directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != '.'}
    expected_directories.update(DEPENDENCY_DIRECTORIES.get(destination, ()))
    for path in destination.rglob('*'):
        info = path.lstat(); name = str(path.relative_to(destination))
        if stat.S_ISDIR(info.st_mode) and name in expected_directories:
            original = DEPENDENCY_DIRECTORY_IDENTITIES.get(destination, {}).get(name)
            if original is not None and entry_identity(info) != entry_identity(original): raise vm.Refused('Dependency directory identity changed')
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise vm.Refused('Foreign test dependency entry: ' + name)
        original = DEPENDENCY_FILE_IDENTITIES.get(destination, {}).get(name)
        if original is not None and entry_identity(info) != original: raise vm.Refused('Dependency file identity changed')
        actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected: raise vm.Refused('Changed or stale test dependency namespace; preserved for inspection')
    return hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()


def required_group(paths=None, *, group=None):
    path = vm.HERE / 'runtime-required-cases.json'
    raw = path.read_bytes()
    lock = json.loads((vm.HERE / 'runtime-toolchain.json').read_text())['required_matrix_sha256']
    if hashlib.sha256(raw).hexdigest() != lock: raise vm.Refused('Reviewed required-case manifest hash changed')
    document = json.loads(raw)
    if document.get('schema') != 1: raise vm.Refused('Unknown required-case manifest schema')
    groups = document['groups']
    if group is None:
        matches = [name for name, item in groups.items() if item['selectors'] == list(paths or [])]
        if len(matches) != 1: raise vm.Refused('Select one predeclared complete or focused runtime group')
        group = matches[0]
    if group not in groups or (paths and list(paths) != groups[group]['selectors']):
        raise vm.Refused('Runtime selectors do not match the reviewed group')
    result = groups[group]
    if len(result['nodeids']) != len(set(result['nodeids'])) or not result['nodeids']:
        raise vm.Refused('Empty or duplicate required-case manifest')
    return group, result, lock


def verify_container(entry, identity, owner, image, mounts, network, volume):
    config, host = entry['Config'], entry['HostConfig']
    if entry['Id'] != identity or config.get('Image') != image or config.get('Labels', {}).get('ha-ops.dev.run') != owner:
        raise vm.Refused('Runtime container identity changed')
    if host.get('NetworkMode') != network or not host.get('ReadonlyRootfs') or host.get('Privileged') or host.get('Devices') or host.get('PortBindings'):
        raise vm.Refused('Runtime container isolation failed')
    actual = {row['Destination']: (row['Source'], row['RW']) for row in entry['Mounts'] if row['Type'] == 'bind'}
    if actual != {target: (str(source), writable) for target, (source, writable) in mounts.items()}:
        raise vm.Refused('Runtime container exact mounts failed')
    volumes = [row for row in entry['Mounts'] if row['Type'] == 'volume']
    if len(volumes) != 1 or volumes[0].get('Name') != volume or volumes[0]['Destination'] != '/workspace/output' or not volumes[0]['RW']:
        raise vm.Refused('Runtime exact tmpfs volume failed')
    if any(item.split('=', 1)[0].lower() in ('http_proxy', 'https_proxy', 'all_proxy') for item in config.get('Env', [])):
        raise vm.Refused('Ambient proxy in isolated runtime')



def capture_output(target, completed):
    target['execution_completed'] = True
    target.update(stdout=completed.stdout.decode(errors='replace'), stderr=completed.stderr.decode(errors='replace'),
                  returncode=completed.returncode)


def capture_partial_output(target, error):
    if target.get('execution_completed'): return
    for name in ('stdout', 'stderr'):
        value = getattr(error, name, None)
        if value is None and name == 'stdout': value = getattr(error, 'output', None)
        if value is not None: target[name] = value.decode(errors='replace') if isinstance(value, bytes) else str(value)
    code = getattr(error, 'returncode', None)
    if code is not None: target['returncode'] = code


def capture_artifacts(target, container, *, recreated=False, ext4=False):
    """Read output while its consumer and backing volume still exist."""
    files = [('junit', 'results-recreated.xml' if recreated else 'results.xml')]
    if ext4: files.append(('ext4_raw', 'ext4-proof.json'))
    complete = bool(target.get('execution_completed'))
    for key, filename in files:
        if key in target or key == 'junit' and target.get('junit_absent') and target.get('execution_completed'): continue
        completed = vm.docker('exec', container, '/bin/cat', '/workspace/output/' + filename, check=False)
        if completed.returncode == 0:
            target[key] = completed.stdout.decode(errors='replace')
        elif target.get('execution_completed') and b'no such file' in completed.stderr.lower():
            if key == 'junit':
                target['junit_absent'] = {'path': filename, 'stderr': completed.stderr.decode(errors='replace')}
            else: target[key] = None  # Completed execution and proven absence have no sole bytes to preserve.
        else:
            target.setdefault('artifact_errors', []).append({'path': filename, 'returncode': completed.returncode,
                'stderr': completed.stderr.decode(errors='replace')})
            complete = False
    return complete

def run_tests(paths, report, *, ext4_profile=None, recreate=False, group=None, workers=0):
    group, required, matrix_hash = required_group(paths, group=group)
    paths = required['selectors']
    if workers not in (0, 2, 4) or (workers and group not in ('native-git', 'native-save', 'native-scope', 'core')):
        raise vm.Refused('Parallel workers are restricted to the independent core test groups')
    vm.ready()
    data = runtime_image()
    owner = uuid.uuid4().hex
    root = vm.RUNS / ('runtime-' + owner)
    report = external_report(report, root)
    source, output = root / 'input', root / 'output'
    receiver_code = root / 'receiver.py'
    clients = []
    fixture = None
    host_fixture = None
    ext4_acquired = False
    dependencies = None
    first = None
    volume = 'ha-ops-runtime-' + owner
    volume_created = False
    active_attempt = None
    active_container = None
    result = {'status': 'started', 'fixture': str(root), 'image': data, 'containers': [], 'checks': [],
              'required_group': group, 'required_manifest_sha256': matrix_hash, 'expected_nodeids': required['nodeids'], 'workers': workers}
    try:
        root.mkdir(mode=0o700, parents=True)
        host_fixture = RuntimeFixture(root)
        dependencies = pytest_dependencies()
        snapshot = vm.snapshot_input(APP / 'tests')
        copy_preserving_modes(APP / 'tests', source, snapshot, fixture=host_fixture)
        host_fixture.write('receiver.py', (vm.HERE / 'runtime_receiver.py').read_bytes())
        host_fixture.mkdir('output', mode=0o777, dynamic=True)
        host_fixture.chmod('output', Path('.'), 0o777)
        for path in (source, output, dependencies): vm.verify_source(path)
        volume_created = True
        vm.docker('volume', 'create', '--driver', 'local', '--label', 'ha-ops.dev.run=' + owner,
            '--opt', 'type=tmpfs', '--opt', 'device=tmpfs', '--opt', 'o=size=1073741824,mode=1777', volume)
        volume_info = json.loads(vm.docker('volume', 'inspect', volume).stdout)[0]
        if (volume_info.get('Driver'), volume_info.get('Labels', {}).get('ha-ops.dev.run'), volume_info.get('Options')) != (
                'local', owner, {'type': 'tmpfs', 'device': 'tmpfs', 'o': 'size=1073741824,mode=1777'}):
            raise vm.Refused('Owned bounded tmpfs identity failed')
        result['tmpfs_volume'] = volume_info
        client_mounts = {'/tests': (source, False), '/testdeps': (dependencies, False)}
        ext4_env = []
        if ext4_profile:
            if os.getuid() == 0: raise vm.Refused('Ext4 App proof requires the exact nonroot user')
            vm.ext4_guest('preflight', owner, ext4_profile, owner=(65534, 65534))
            ext4_acquired = True
            fixture = vm.ext4_guest('setup', owner, ext4_profile, owner=(65534, 65534))
            client_mounts['/workspace/ext4'] = (Path(fixture['mount_path']), True)
            token = uuid.uuid4().hex
            ext4_env = ['--user', '65534:65534', '--cap-drop', 'ALL',
                        '--security-opt', 'no-new-privileges']
            for name, value in {'ROOT': '/workspace/ext4', 'PROFILE': ext4_profile, 'RUN': owner,
                'DEVICE': fixture['mount']['device'], 'REPORT': '/workspace/output/ext4-proof.json',
                'PRIOR_REPORT': '/workspace/output/ext4-prior.json', 'TOKEN': token, 'ATTEMPT': '0'}.items():
                ext4_env += ['--env', 'HA_OPS_EXT4_' + name + '=' + value]
        args = ['create', '--name', 'ha-ops-runtime-' + owner, '--label', 'ha-ops.dev.run=' + owner,
                '--network', 'none', '--read-only', '--tmpfs', '/tmp:exec', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--env', 'PYTHONPATH=/app:/tests:/testdeps', '--env', 'PATH=/usr/bin:/bin',
                '--env', 'HA_OPS_RUNTIME_RECEIVER_SOCKET=127.0.0.1:39117', '--entrypoint', '/usr/bin/python3',
                '--mount', f'type=volume,source={volume},target=/workspace/output']
        args += ext4_env
        for target, (source_path, writable) in client_mounts.items():
            args += ['--mount', f'type=bind,source={source_path},target={target}' + ('' if writable else ',readonly')]
        client_args = list(args)
        clients.append('ha-ops-runtime-' + owner)
        client = vm.docker(*args, data['image_id'], '-c', 'import time;time.sleep(1800)').stdout.decode().strip()
        if not client: raise vm.Refused('Runtime create returned no immutable container identity')
        clients[-1] = client
        entry = json.loads(vm.docker('inspect', client).stdout)[0]
        verify_container(entry, client, owner, data['image_id'], client_mounts, 'none', volume)
        vm.docker('start', client)
        receiver_mounts = {'/tests': (source, False), '/receiver.py': (receiver_code, False)}
        if fixture: receiver_mounts['/workspace/ext4'] = (Path(fixture['mount_path']), True)
        dev_image = vm.docker('image', 'inspect', vm.image_name(), '--format', '{{.Id}}').stdout.decode().strip()
        args = ['create', '--name', 'ha-ops-receiver-' + owner, '--label', 'ha-ops.dev.run=' + owner,
                '--network', 'container:' + client, '--read-only', '--tmpfs', '/tmp:exec', '--entrypoint', '/usr/local/bin/python3',
                '--mount', f'type=volume,source={volume},target=/workspace/output']
        for target, (source_path, writable) in receiver_mounts.items():
            args += ['--mount', f'type=bind,source={source_path},target={target}' + ('' if writable else ',readonly')]
        
        if fixture: args += ['--user', '65534:65534', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges']
        clients.append('ha-ops-receiver-' + owner)
        receiver = vm.docker(*args, dev_image, '/receiver.py', '39117').stdout.decode().strip()
        if not receiver: raise vm.Refused('Receiver create returned no immutable container identity')
        clients[-1] = receiver
        peer = json.loads(vm.docker('inspect', receiver).stdout)[0]
        verify_container(peer, receiver, owner, dev_image, receiver_mounts, 'container:' + client, volume)
        vm.docker('start', receiver)
        deadline = time.monotonic() + 15
        ping = 'from network_publication_helpers import receiver_request;assert receiver_request(action="ping")=="ready"'
        while vm.docker('exec', client, '/usr/bin/python3', '-c', ping, check=False).returncode:
            if time.monotonic() >= deadline: raise vm.Refused('Receiver loopback control not ready')
            time.sleep(.05)
        # Verify original modules byte-for-byte, not imports from the test bind.
        code = 'import pathlib,hashlib,json;print(json.dumps({str(p.relative_to("/app")):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path("/app").rglob("*") if p.is_file() and p.name!="config.yaml"}))'
        manifest = json.loads(vm.docker('exec', client, '/usr/bin/python3', '-c', code).stdout)
        if manifest != data['app_manifest']: raise vm.Refused('Original /app content does not match built source manifest')
        result['test_dependency_manifest_sha256'] = validate_dependencies(dependencies)
        result['containers'] = [entry, peer]
        collection = vm.docker('exec', client, '/usr/bin/python3', '-m', 'pytest', '-p', 'no:cacheprovider',
            '--collect-only', '-q', *['/tests/' + path for path in paths], timeout=120, check=False)
        result['collection'] = {'stdout': collection.stdout.decode(errors='replace'), 'stderr': collection.stderr.decode(errors='replace'), 'returncode': collection.returncode}
        collected = [match.group(1) for line in collection.stdout.decode().splitlines()
                     if (match := re.fullmatch(r'(?:/tests/)?(test_[A-Za-z0-9_]+\.py::.+)', line))]
        if collection.returncode or len(collected) != len(set(collected)) or set(collected) != set(required['nodeids']):
            raise vm.Refused('Runtime collection differs from the predeclared required identity set: ' + collection.stdout.decode()[-2000:])
        result['collected_nodeids'] = collected
        command = ['/usr/bin/python3', '-m', 'pytest', '-p', 'no:cacheprovider', '-r', 'a',
                   '--durations=20', '--basetemp=/workspace/output/pytest', '--junitxml=/workspace/output/results.xml',
                   *(['-n', str(workers)] if workers else []), *['/tests/' + path for path in paths]]
        active_attempt, active_container = result, client
        result['execution_started'] = True
        completed = vm.docker('exec', client, *command, timeout=1200, check=False)
        capture_output(result, completed)
        capture_artifacts(result, client, ext4=fixture is not None)
        result['status'] = 'passed' if completed.returncode == 0 else 'failed'
        if 'junit' not in result:
            result['status'] = 'failed'
            result['accounting_error'] = 'Required test report missing'
        else:
            result['accounting'] = test_accounting(result['junit'], expected=required['nodeids'], ext4_profile=ext4_profile)
            if result['accounting']['required_unexecuted']: result['status'] = 'failed'
        sys.stdout.buffer.write(completed.stdout); sys.stderr.buffer.write(completed.stderr)
        if fixture and completed.returncode == 0:
            proof = json.loads(result['ext4_raw'])
            vm.validate_ext4_proof(proof, run_id=owner, profile=ext4_profile, attempt=0, token=token, fixture=fixture)
            result['ext4_proofs'] = [proof]
        if recreate and completed.returncode == 0 and result['status'] == 'passed':
            # Receiver retains the exact original network-none namespace; the
            # original client is removed before a second original App starts.
            checkpoint = dict(result, status='checkpoint', execution_status=result['status'])
            vm.write_report(report, checkpoint)
            vm.remove_owned(client, owner)
            clients.remove(client)
            result['recreation_fence'] = {'identity': client, 'status': 'removed'}
            args = list(client_args)
            index = args.index('--name') + 1
            args[index] += '-recreated'
            args[args.index('--network') + 1] = 'container:' + receiver
            if fixture:
                vm.docker('exec', receiver, '/bin/cp', '/workspace/output/ext4-proof.json', '/workspace/output/ext4-prior.json')
                token2 = uuid.uuid4().hex
                args = [item.replace('HA_OPS_EXT4_ATTEMPT=0', 'HA_OPS_EXT4_ATTEMPT=1').replace(
                    'HA_OPS_EXT4_TOKEN=' + token, 'HA_OPS_EXT4_TOKEN=' + token2) for item in args]
            clients.append('ha-ops-runtime-' + owner + '-recreated')
            second = vm.docker(*args, data['image_id'], '-c', 'import time;time.sleep(1800)').stdout.decode().strip()
            if not second: raise vm.Refused('Recreated client returned no immutable container identity')
            clients[-1] = second
            second_entry = json.loads(vm.docker('inspect', second).stdout)[0]
            verify_container(second_entry, second, owner, data['image_id'], client_mounts, 'container:' + receiver, volume)
            result['containers'].append(second_entry)
            vm.docker('start', second)
            second_command = [item.replace('--basetemp=/workspace/output/pytest', '--basetemp=/workspace/output/pytest-recreated').replace('--junitxml=/workspace/output/results.xml', '--junitxml=/workspace/output/results-recreated.xml') for item in command]
            result['recreated'] = {'execution_started': True}
            active_attempt, active_container = result['recreated'], second
            repeated = vm.docker('exec', second, *second_command, timeout=1200, check=False)
            capture_output(active_attempt, repeated)
            capture_artifacts(active_attempt, second, recreated=True, ext4=fixture is not None)
            if 'junit' in active_attempt:
                active_attempt['accounting'] = test_accounting(active_attempt['junit'], expected=required['nodeids'], ext4_profile=ext4_profile, recreated=True)
                if active_attempt['accounting']['required_unexecuted']: result['status'] = 'failed'
            else: result['status'] = 'failed'
            sys.stdout.buffer.write(repeated.stdout); sys.stderr.buffer.write(repeated.stderr)
            if repeated.returncode: result['status'] = 'failed'
            if fixture and repeated.returncode == 0:
                second_proof = json.loads(active_attempt['ext4_raw'])
                vm.validate_ext4_proof(second_proof, run_id=owner, profile=ext4_profile, attempt=1,
                    token=token2, fixture=fixture, previous=proof)
                result['ext4_proofs'].append(second_proof)
    except BaseException as error:
        first = error
        if active_attempt is not None: capture_partial_output(active_attempt, error)
        raise
    finally:
        proof_complete = True
        recovery_primary = None
        if active_attempt is not None:
            try:
                proof_complete = capture_artifacts(active_attempt, active_container, recreated=active_attempt is not result, ext4=fixture is not None)
            except BaseException as error:
                proof_complete = False
                row = {'phase': 'runtime artifact recovery', 'failure': vm.failure_payload(error)}
                result.setdefault('artifact_recovery_errors', []).append(row)
                if first is not None: attach_cleanup(first, [row])
                else: first = recovery_primary = error
        finish_runtime(result, report, owner, host_fixture, dependencies, clients, first,
                       volume=volume if volume_created else None, ext4_profile=ext4_profile,
                       ext4_acquired=ext4_acquired, proof_complete=proof_complete)
        if recovery_primary is not None: raise recovery_primary
    return 0 if result['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    resolve_parser = commands.add_parser('resolve')
    resolve_parser.add_argument('--arch', choices=BASES, required=True)
    commands.add_parser('build')
    commands.add_parser('resolve-pip')
    commands.add_parser('probe')
    commands.add_parser('diagnose')
    negative = commands.add_parser('check-lock-failure')
    negative.add_argument('--report', required=True, type=Path)
    run = commands.add_parser('run')
    run.add_argument('--report', required=True, type=Path)
    run.add_argument('--ext4-profile', choices=vm.EXT4_SIZES)
    run.add_argument('--recreate', action='store_true')
    run.add_argument('--group')
    run.add_argument('--workers', type=int, choices=(0, 2, 4), default=0)
    run.add_argument('tests', nargs='*')
    args = parser.parse_args()
    if args.action == 'resolve': resolve(args.arch)
    elif args.action == 'build': build()
    elif args.action == 'resolve-pip': resolve_pip()
    elif args.action == 'probe': probe()
    elif args.action == 'diagnose': diagnose()
    elif args.action == 'check-lock-failure': check_lock_failure(args.report)
    elif args.action == 'run': return run_tests(args.tests, args.report, ext4_profile=args.ext4_profile, recreate=args.recreate, group=args.group, workers=args.workers)


if __name__ == '__main__': sys.exit(main())
