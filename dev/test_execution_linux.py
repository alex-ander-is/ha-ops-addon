"""Execution-only owned Linux routes. This module grants no receipt authority."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import test_plan as shared
import test_receipts as receipts
import test_receipt_linux as linux

__test__ = False
PREFIX = 'ha-ops/tests/test_native_ext4.py::'
RECREATION = PREFIX + 'test_container_recreation'
RECIPES = {
    'allocation': [PREFIX + 'test_' + name for name in (
        'loose_index_worktree_save', 'existing_reflog_complete_atomic_overlap',
        'native_publication_physical_boundaries', 'control_atomic_overlap',
        'independent_recovery_copies', 'no_reuse_repack', 'short_and_eio_boundaries',
        'network_publication_ext4_frozen_physical_peak')],
    'bytes': [PREFIX + 'test_kernel_byte_enospc', PREFIX + 'test_network_graph_fetch_real_kernel_enospc_after_admission'],
    'inodes': [PREFIX + 'test_kernel_inode_enospc'],
}
COMPACT = 'ha-ops/tests/test_native_compact.py::test_real_inode_exhaustion_in_wrapper_ext4'
SAVE = frozenset('ha-ops/tests/test_native_save.py::TestFailures::test_child_or_io_failure_retains_primary_and_preserves_live_canonical[ENOSPC-' + name + ']' for name in ('hash-object', 'read-tree', 'update-index', 'write-tree'))
HOST = {
    'ha-ops/tests/test_native_migration_review.py::test_later_request_failure_preserves_trusted_other_context_continuity[enospc-decide]',
    'ha-ops/tests/test_native_migration_review.py::test_later_request_failure_preserves_trusted_other_context_continuity[enospc-review]',
    'ha-ops/tests/test_native_operations.py::test_system_io_failures_preserve_previous_manifest[ENOSPC-fsync]',
    'ha-ops/tests/test_native_operations.py::test_system_io_failures_preserve_previous_manifest[ENOSPC-replace]',
    'ha-ops/tests/test_test_runner_guard.py::test_owned_reap_exhaustion_stays_red_with_bounded_errors',
}
# Exact collected parameter spellings, including pytest's escaped newlines.
for source in (
    'from os import *\ndef measure(): return statvfs(".")\n',
    'import os as system\nreflect=getattr\nquery=reflect(system,"statvfs")\ndef measure(): return query(".")\n',
    'import os,operator\ndef measure(): return operator.attrgetter("statvfs")(os)(".")\n',
    'import os\ndef measure(): return __builtins__["getattr"](os,"statvfs")(".")\n',
    'import os\ndef measure(): return vars(os)["statvfs"](".")\n',
    'import os\nquery=getattr(os,"statvfs")\ndef measure(): return query(".")\n',
):
    HOST.add('ha-ops/tests/test_test_plan.py::test_unresolved_resource_indirection_overrides_broad_host_family[' + source.replace('\n', '\\n') + ']')
DEFERRED = frozenset({
    'ha-ops/tests/test_native_network_publication.py::test_original_app_container_recreation_retains_pending_same_commit',
    'ha-ops/tests/test_native_scope.py::test_retained_legacy_scope_authenticated_network_publication_and_same_commit_recovery[ssh-accepted-readback-loss]',
})
EXT4_ENV = ('ROOT', 'PROFILE', 'RUN', 'DEVICE', 'REPORT', 'PRIOR_REPORT', 'TOKEN', 'ATTEMPT')
# Linux limits each exec argument to 128 KiB. Keep the complete JSON argument
# below 60 KiB, including its repeated selected/attempt accounting namespace.
MAX_RECIPE_BYTES = 60 * 1024
# Bound serial work as well as transport size within the child's 240s budget.
MAX_LINUX_NODES = 32


def route(node, unsafe, families):
    nodeid, filename = node['id'], Path(node['source']).name
    reviewed_source = node['source'] == 'ha-ops/tests/' + filename
    if nodeid in HOST:
        return 'host'
    if nodeid == COMPACT:
        return 'inodes'
    if reviewed_source and filename == 'test_native_apply_ext4.py':
        return 'allocation'
    if reviewed_source and filename == 'test_native_ext4.py':
        if nodeid == RECREATION:
            return 'allocation'
        for profile, ids in RECIPES.items():
            if nodeid in ids:
                return profile
        raise shared.Refused('Unmapped ext4 node: ' + nodeid)
    if reviewed_source and filename in families or nodeid in SAVE:
        return 'linux'
    if unsafe:
        raise shared.Refused('Unreviewed exclusive isolated route: ' + nodeid)
    return 'host'


def jobs(nodes, inventory):
    """Bind full ordered support recipes and exact selected ownership up front."""
    available = {node['id'] for node in inventory}
    grouped = {}
    for node, profile in nodes:
        grouped.setdefault(profile, []).append(node['id'])
    result = []
    for profile, selected in grouped.items():
        if profile == 'linux':
            batch = []
            for nodeid in selected:
                candidate = batch + [nodeid]
                recipe = {'profile': profile, 'selected': candidate, 'attempts': [candidate]}
                if (len(candidate) > MAX_LINUX_NODES or
                        len(json.dumps(recipe, separators=(',', ':')).encode()) > MAX_RECIPE_BYTES):
                    if not batch:
                        raise shared.Refused('Linux selector exceeds bounded recipe transport')
                    result.append({'profile': profile, 'selected': batch, 'attempts': [batch]})
                    batch = [nodeid]
                    if len(json.dumps({'profile': profile, 'selected': batch, 'attempts': [batch]}, separators=(',', ':')).encode()) > MAX_RECIPE_BYTES:
                        raise shared.Refused('Linux selector exceeds bounded recipe transport')
                else:
                    batch = candidate
            if batch:
                result.append({'profile': profile, 'selected': batch, 'attempts': [batch]})
            continue
        mandatory = RECIPES[profile] + [RECREATION]
        if not set(mandatory) <= available:
            raise shared.Refused('Missing mandatory ext4 support: ' + profile)
        first = RECIPES[profile] + [node for node in selected if node not in mandatory] + [RECREATION]
        result.append({'profile': profile, 'selected': selected,
                       'attempts': [first] + ([[RECREATION]] if profile == 'allocation' else [])})
    return result


def containers_success(outer, count):
    if (outer.get('status') != 'passed' or outer.get('failure') is not None or
            outer.get('cleanup_errors') != [] or outer.get('capture_errors') != []):
        raise shared.Refused('Incomplete execution-only outer evidence')
    rows = outer.get('containers')
    if type(rows) is not list or len(rows) != count:
        raise shared.Refused('Missing exact execution-only containers')
    for row in rows:
        if (not all(receipts.zero(row.get(key)) for key in ('wait_exit_code', 'docker_wait_returncode', 'supervisor_exit_code')) or
                row.get('watchdog_reason') != 'completed' or row.get('wait_failure') is not None or
                row.get('state', {}).get('OOMKilled') is not False or
                not receipts.zero(row.get('state', {}).get('ExitCode')) or row.get('state', {}).get('Status') != 'exited'):
            raise shared.Refused('Failed execution-only container')


def controller_selectors(expected):
    """Match the protected controller's bounded, credential-redacted summary."""
    params = ['--rootdir', '/workspace/input', '-p', 'test_plan_plugin', '-p',
              'no:cacheprovider', '-n', '1', '--dist', 'worksteal', *expected]
    redact = linux.wrapper().guard.redact
    return [redact(value)[:256] for value in params[:64]
            if '.py' in value and not value.startswith('-')]


def launch(root, data, recipe, directory):
    directory.mkdir(mode=0o700)
    module, stage = linux.wrapper(), linux.make_stage(root)
    command = ['python3', 'dev/test_execution_linux.py', '--child', json.dumps(recipe, separators=(',', ':'))]
    try:
        for name in data['inputs']:
            source = root / name
            if not source.exists():
                continue
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
        if recipe['profile'] == 'linux':
            code, evidence = module.generic_run(stage, command, receipt_profile=linux.PROFILE)
        else:
            code, evidence = module.ext4_run(stage, command, profile=recipe['profile'],
                recreate=len(recipe['attempts']) == 2, report=directory / 'ext4.json', return_evidence=True)
        if not receipts.zero(code):
            raise shared.Refused('Execution-only wrapper failed')
        outer = receipts.strict_json(receipts.regular_bytes(evidence / 'manifest.json'))
        containers_success(outer, len(recipe['attempts']))
        if recipe['profile'] == 'linux':
            linux.validate_outer(outer, command)
        else:
            if (outer.get('profile') != recipe['profile'] or outer.get('command') != command or
                    type(outer.get('source_snapshot')) is not dict):
                raise shared.Refused('Missing ext4 source/command binding')
            links = outer.get('machine_records')
            expected_links = [{'profile': recipe['profile'], 'attempt': attempt,
                'container': row['id'], 'file': 'test-plan-' + recipe['profile'] + '-' + str(attempt) + '.json'}
                for attempt, row in enumerate(outer['containers'])]
            if links != expected_links:
                raise shared.Refused('Missing exact ext4 machine/container binding')
            report = receipts.strict_json(receipts.regular_bytes(directory / 'ext4.json'))
            if (report.get('status') != 'passed' or report.get('cleanup_errors') != [] or
                    report.get('failure') is not None or report.get('image_id') != report.get('image_id_after_cleanup') or
                    report.get('cleanup', {}).get('status') != 'absent' or report.get('cleanup_repeat', {}).get('status') != 'absent'):
                raise shared.Refused('Incomplete execution-only ext4 cleanup')
        manifests = list((evidence / 'inner').glob('run-*/manifest.json'))
        if len(manifests) != len(recipe['attempts']):
            raise shared.Refused('Missing exact execution-only inner controllers')
        controllers = [receipts.strict_json(receipts.regular_bytes(path)) for path in manifests]
        for controller in controllers:
            receipts.controller_success(controller)
        records = []
        for attempt, expected in enumerate(recipe['attempts']):
            name = 'test-plan.json' if recipe['profile'] == 'linux' else 'test-plan-' + recipe['profile'] + '-' + str(attempt) + '.json'
            record = receipts.strict_json(receipts.regular_bytes(evidence / name))
            # Controller command binds each exact attempt; do not rely on glob order.
            matching = [c for c in controllers if c.get('selectors') == controller_selectors(expected)]
            if len(matching) != 1:
                raise shared.Refused('Missing exact attempt controller binding')
            records.append(record)
        return records, {'route': 'execution-only-linux', 'profile': recipe['profile'],
                         'outer_manifest': str(evidence / 'manifest.json'), 'recipe': recipe}
    except (module.Refused, subprocess.SubprocessError) as error:
        raise shared.Refused('Execution-only Linux route failed: ' + module.guard.redact(str(error))) from error
    finally:
        shutil.rmtree(stage)


def child(recipe):
    if sys.platform != 'linux':
        raise shared.Refused('Execution-only route requires Linux')
    env = linux.prepare_child_environment()
    profile = recipe['profile']
    attempt = 0
    if profile != 'linux':
        for suffix in EXT4_ENV:
            key = 'HA_OPS_EXT4_' + suffix
            if not os.environ.get(key):
                raise shared.Refused('Missing ext4 child field: ' + key)
            env[key] = os.environ[key]
        attempt_text = env['HA_OPS_EXT4_ATTEMPT']
        if (env['HA_OPS_EXT4_PROFILE'] != profile or attempt_text not in ('0', '1') or
                env['HA_OPS_EXT4_ROOT'] != '/workspace/ext4' or
                not re.fullmatch('[0-9a-f]{32}', env['HA_OPS_EXT4_TOKEN']) or
                not re.fullmatch('[0-9a-f]{32}', env['HA_OPS_EXT4_RUN'])):
            raise shared.Refused('Altered ext4 child binding')
        attempt = int(attempt_text)
        env['TEST_PLAN_OUTPUT'] = '/workspace/output/test-plan-' + profile + '-' + str(attempt) + '.json'
    if attempt >= len(recipe['attempts']):
        raise shared.Refused('Unexpected ext4 attempt')
    command = ['/usr/local/bin/python3', 'dev/test_runner.py', '--deadline', '240',
               '--artifacts', '/workspace/output/test-diagnostics', '--', '--rootdir', '/workspace/input',
               '-p', 'test_plan_plugin', '-p', 'no:cacheprovider', '-n', '1', '--dist', 'worksteal',
               *recipe['attempts'][attempt]]
    os.execve(command[0], command, env)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--child':
        raise SystemExit('Only the owned execution child entrypoint is supported')
    child(json.loads(sys.argv[2]))
