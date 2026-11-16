#!/usr/bin/env python3
"""Explained selective pre-push. Unknown influence blocks; --full is explicit."""
import argparse
from collections import Counter, defaultdict
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import test_plan as shared

ISOLATED = {'test_test_runner_guard.py', 'test_test_plan.py', 'test_selective_pre_push.py'}
MIXED = {'test_server.py', 'test_client_contract.py'}
MIXED_AREAS = frozenset({'reset', 'logs', 'backend-common', 'backend-other'})
COMMON = re.compile(r'(state|command|ownership|transport|dispatch|restart|checkpoint|contract|auth|lease)')
LOG = re.compile(r'(log_window|log_scroll|log_wrap|scroll|wrapping|render_log|logs_panel)')


def category(node):
    """Small reviewed semantic grammar; concrete membership is always collected."""
    source, name = node['source'], node['id'].split('::', 1)[-1].lower()
    filename = Path(source).name
    if filename in MIXED:
        if 'reset' in name:
            return 'reset'
        if LOG.search(name):
            return 'logs'
        if COMMON.search(name):
            return 'backend-common'
        return 'backend-other'
    stem = Path(source).stem.removeprefix('test_')
    if stem.startswith('native_'):
        return 'native-' + stem[7:]
    if stem == 'preview_decisions':
        return 'ui-common'
    if 'migration' in stem:
        return 'migration'
    if 'reset' in stem:
        return 'reset'
    if 'log' in stem:
        return 'logs'
    if filename in ISOLATED or stem in ('pre_push_hook',):
        return 'runner'
    if any(word in stem for word in ('release', 'publish')):
        return 'release'
    if any(word in stem for word in ('client', 'frontend', 'browser')):
        return 'ui-common'
    if any(word in stem for word in ('virtual', 'dev_environment')):
        return 'virtualization'
    return 'backend-common'


def is_ancestor(root, old, new):
    import subprocess
    result = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', old, new], capture_output=True)
    if result.returncode not in (0, 1):
        raise shared.Refused('Cannot verify pushed tag ancestry')
    return result.returncode == 0


def change_basis(root, records, full=False):
    """Union every unpublished commit and independent dirty layer, for every ref."""
    paths, refs, branches, tags = set(), [], [], []
    for line in records.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise shared.Refused('Malformed pre-push record; expected four fields')
        local, sha, remote, base = fields
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base):
            raise shared.Refused('Invalid pre-push object ID')
        if set(sha) == {'0'}:
            refs.append({'local': local, 'remote': remote, 'deleted': True})
            continue
        if not local.startswith(('refs/heads/', 'refs/tags/')) or not remote.startswith(('refs/heads/', 'refs/tags/')):
            raise shared.Refused('Unsupported pushed ref: ' + local)
        actual = shared.git(root, 'rev-parse', '--verify', local).decode().strip()
        if actual != sha:
            raise shared.Refused('Pushed local ref moved: ' + local)
        commit = shared.git(root, 'rev-parse', '--verify', sha + '^{commit}').decode().strip()
        entry = {'local': local, 'remote': remote, 'sha': sha, 'commit': commit, 'base': base}
        refs.append(entry)
        (tags if local.startswith('refs/tags/') else branches).append(entry)
    for entry in branches:
        if entry['commit'] != shared.git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip():
            raise shared.Refused('Pushed branch is not checked-out HEAD; check out that exact commit before testing: ' + entry['local'])
        if set(entry['base']) == {'0'} and full:
            continue
        if set(entry['base']) == {'0'}:
            raise shared.Refused('New branch has no advertised comparison base; use explicit --full or push against an established branch')
        base = shared.git(root, 'rev-parse', '--verify', entry['base'] + '^{commit}').decode().strip()
        entry['verified_base'] = base
        # An existing remote-tracking ref is an additional stale-base check.
        # The hook's advertised SHA itself is the authoritative transport base.
        ancestor = shared.git(root, 'merge-base', base, entry['commit']).decode().strip()
        entry['merge_base'] = ancestor
        outputs = [shared.git(root, 'diff', '--name-only', '--no-renames', '-z', ancestor, entry['commit'], '--')]
        for commit in shared.git(root, 'rev-list', ancestor + '..' + entry['commit']).decode().splitlines():
            outputs.append(shared.git(root, 'diff-tree', '--root', '--no-commit-id', '--name-only', '--no-renames', '-r', '-m', '-z', commit))
        paths.update(os.fsdecode(p) for output in outputs for p in output.split(b'\0') if p)
    for tag in tags:
        covered = any(is_ancestor(root, tag['commit'], branch['commit']) for branch in branches)
        head = shared.git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
        if not covered and tag['commit'] != head:
            raise shared.Refused('Pushed tag snapshot differs from checked-out HEAD without a covered pushed branch')
        if not full and not any(is_ancestor(root, tag['commit'], branch['commit']) for branch in branches):
            raise shared.Refused('Tag-only/unmatched tag has no verified branch comparison base; use explicit --full')
    for arguments in [('diff', '--cached', '--name-only', '--no-renames', '-z', '--'),
                      ('diff', '--name-only', '--no-renames', '-z', '--'),
                      ('ls-files', '--others', '--exclude-standard', '-z')]:
        paths.update(os.fsdecode(p) for p in shared.git(root, *arguments).split(b'\0') if p)
    if not refs:
        raise shared.Refused('No pre-push records; provide actual hook stdin')
    return sorted(p for p in paths if not shared.generated_path(p)), refs


def areas_for_path(path, nodes):
    """Only qualified module boundaries are narrow; mixed source hunks block."""
    categories = {category(node) for node in nodes}
    canonical_path = (Path(path).as_posix() == path and '..' not in Path(path).parts and '\\' not in path)
    python_name = re.fullmatch(r'[a-z_][a-z0-9_]*\.py', Path(path).name) is not None
    collected_python = (canonical_path and python_name and path.startswith('ha-ops/tests/') and
                        any(node['source'] == path for node in nodes))
    declared_root_module = re.fullmatch(r'ha-ops/tests/test_[a-z0-9_]+\.py', path) is not None
    if collected_python or declared_root_module:
        if Path(path).name in MIXED:
            # Any changed mixed module can have lost a family entirely. Current
            # survivors cannot prove its former membership; retain every declared
            # family for edits, partial/full deletion and both rename endpoints.
            return set(MIXED_AREAS), None
        surviving = {category(node) for node in nodes if node['source'] == path}
        return surviving or {category({'source': path, 'id': path + '::deleted'})}, None
    if path in ('AGENTS.md', 'TESTING.md', 'README.md', 'Backlog.md') or path.startswith('ha-ops/docs/') and path.endswith('.md'):
        return set(), None
    if path in ('release', 'publish-release', 'release-policy.py', 'ha-ops/config.yaml', 'ha-ops/CHANGELOG.md'):
        return {'release'}, None
    if path.startswith('dev/virtualization/'):
        return {'virtualization', 'runner'}, None
    if path.startswith(('dev/', '.githooks/')):
        return {'runner'}, None
    if path == 'ha-ops/app/reset_git.py':
        return {'reset', 'backend-common'}, None
    if path.startswith('ha-ops/app/native_') and path.endswith('.py'):
        area = 'native-' + Path(path).stem[7:]
        return {c for c in categories if c.startswith('native-')} | {area, 'backend-common'}, None
    if path.startswith('ha-ops/frontend/src/'):
        if Path(path).name == 'migration-review.js':
            return {'migration', 'ui-common'}, None
        return set(), 'Unqualified mixed frontend boundary: ' + path
    if path in ('ha-ops/app/server.py', 'ha-ops/frontend/dist/ha-ops.js'):
        return set(), 'Unqualified mixed application boundary: ' + path
    return set(), 'Unknown influence: ' + path + '; add a reviewed semantic rule or explicitly request --full'


def make_plan(root, data, records, full=False):
    errors, refs, changed = [], [], []
    try:
        changed, refs = change_basis(root, records, full)
    except shared.Refused as error:
        errors.append(str(error))
    ids = [node['id'] for node in data['nodes']]
    if not ids or len(ids) != len(set(ids)):
        raise shared.Refused('Empty/duplicate generated inventory')
    selected_areas, reasons = set(), defaultdict(list)
    for path in changed:
        areas, error = areas_for_path(path, data['nodes'])
        if error and not full:
            errors.append(error)
        if path.startswith('ha-ops/tests/') and not areas:
            # Removed tests need a surviving category, never a guessed green.
            if not path.endswith('.md') and not full:
                errors.append('No surviving collected category for changed test: ' + path)
        for area in areas:
            selected_areas.add(area)
            reasons[area].append(path)
    decisions = []
    for node in data['nodes']:
        area = category(node)
        selected = full or area in selected_areas
        decisions.append({'id': node['id'], 'category': area, 'selected': selected,
                          'reason': 'explicit full mode' if full else ('changed: ' + ', '.join(reasons[area]) if selected else 'unaffected category')})
    # A requested category with no collected proof is not a successful empty gate.
    for area in selected_areas - {row['category'] for row in decisions}:
        errors.append('Required category has no collected tests: ' + area)
    return {'schema': 1, 'mode': 'full' if full else 'selective', 'changed': changed,
            'refs': refs, 'errors': errors, 'decisions': decisions,
            'inputs': data['inputs'], 'tools': data['tools'],
            'inventory_digest': shared.digest(shared.canonical(data).encode()),
            'category_counts': dict(Counter(row['category'] for row in decisions if row['selected']))}


def verify_outcomes(record, expected, workers):
    """Exact machine accounting, with host skips explicitly unproven."""
    if record.get('errors') or record.get('deselected') or record.get('exitstatus') != 0:
        raise shared.Refused('Failed/error/deselected machine record')
    reports = defaultdict(list)
    for row in record['reports']:
        reports[row['id']].append(row)
    if set(reports) != set(expected):
        raise shared.Refused('Missing/unexpected runtime outcome')
    collections = record['worker_collections'] if workers else [record['collected']]
    if not collections or any(Counter(ids) != Counter(expected) for ids in collections):
        raise shared.Refused('Runtime collection differs from selection')
    skipped = []
    for nodeid, phases in reports.items():
        if any(p['xfail'] or p['outcome'] not in ('passed', 'skipped') for p in phases):
            raise shared.Refused('Failed/xfail outcome: ' + nodeid)
        names = Counter(p['when'] for p in phases)
        if any(p['outcome'] == 'skipped' for p in phases):
            if names not in (Counter(('setup', 'teardown')), Counter(('setup', 'call', 'teardown'))):
                raise shared.Refused('Invalid skipped runtime phases: ' + nodeid)
            skipped.append(nodeid)
        elif names != Counter(('setup', 'call', 'teardown')):
            raise shared.Refused('Missing/duplicate runtime phase: ' + nodeid)
    return sorted(skipped)


# Coarse, reviewed resource boundary: entire capacity/publication fixture families
# require the existing qualified exclusive-volume route. Source profile and direct
# test/fixture API evidence add refusals; these lexical facts are not a general
# proof of absence through arbitrary helpers. Unsupported resource cases must add
# a reviewed family or use the existing qualified route, never ordinary host xdist.
EXCLUSIVE_FAMILIES = {
    'test_native_apply.py', 'test_native_apply_integration.py', 'test_native_apply_ext4.py',
    'test_native_disk.py', 'test_native_ext4.py', 'test_native_network_publication.py',
    'test_native_publication.py', 'test_native_storage.py', 'test_native_compact.py',
    'test_native_scope.py', 'test_native_recovery.py',
}
RESOURCE_CASE = re.compile(r'(statvfs|enospc|exhaustion|physical_delta|initial_capacity|exclusive_volume|real_allocations)')


def unsafe_host_node(node):
    return (node.get('profile') == 'exclusive-volume-ext4' or
            Path(node['source']).name in EXCLUSIVE_FAMILIES or
            bool(RESOURCE_CASE.search(node['id'].lower())) or
            any(call.rsplit('.', 1)[-1] in shared.EXCLUSIVE_API_SYMBOLS for call in node.get('calls', [])))


def execute(root, data, plan, records, artifacts, *, launch=None):
    launch = launch or shared.guarded
    if plan['errors']:
        raise shared.Refused('; '.join(plan['errors']))
    def fresh():
        if shared.validate_map(root, data) or make_plan(root, data, records, plan['mode'] == 'full') != plan:
            raise shared.Refused('Selection inputs changed; recollect')
    fresh()
    selected = [row['id'] for row in plan['decisions'] if row['selected']]
    unsafe = [node['id'] for node in data['nodes'] if node['id'] in set(selected) and unsafe_host_node(node)]
    if unsafe:
        raise shared.Refused('Exclusive resource proof requires qualified isolated Linux/ext4 route: ' + ', '.join(unsafe))
    ordinary = [node for node in selected if Path(node.split('::')[0]).name not in ISOLATED]
    isolated = [node for node in selected if node not in ordinary]
    result = {'status': 'passed', 'jobs': [], 'not_proven': [], 'platform_proofs': 'separate existing qualification obligations; host skips do not prove them'}
    cpu, _ = shared.available_cpus()
    cap = min(10, data['rules']['profiles']['host']['cap'], cpu)
    for phase, ids in [('ordinary', ordinary), ('isolated', isolated)]:
        for index, batch in enumerate(shared.selector_batches(ids)):
            fresh()
            job = artifacts / (phase + '-' + str(index))
            job.mkdir(mode=0o700)
            arguments, expected = [], []
            for number, entries in enumerate(batch):
                selector = job / ('selectors-' + str(number) + '.txt')
                selector.write_text('\n'.join(entries) + '\n')
                arguments.append('@' + str(selector))
                expected.extend(entries)
            workers = min(2 if phase == 'isolated' else cap, len(expected))
            record, evidence = launch(root, ['-n', str(workers), '--dist', 'worksteal', *arguments], job / 'execution')
            result['not_proven'].extend(verify_outcomes(record, expected, workers))
            fresh()
            result['jobs'].append({'phase': phase, 'workers': workers, 'expected': expected, **evidence})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--full', action='store_true', default=os.environ.get('HA_OPS_PRE_PUSH_MODE') == 'full')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    artifacts = args.artifacts or Path(tempfile.mkdtemp(prefix='ha-ops-selective-prepush-'))
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    print('Selective pre-push evidence: ' + str(artifacts), flush=True)
    try:
        records = args.records.read_text()
        common = Path(os.fsdecode(shared.git(root, 'rev-parse', '--git-common-dir')).strip())
        common = common if common.is_absolute() else root / common
        with (common / 'test-plan-host.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data, _ = shared.inventory(root, ['ha-ops/tests'], artifacts / 'collection')
            shared.write_json(artifacts / 'inventory.json', data)
            plan = make_plan(root, data, records, args.full)
            shared.write_json(artifacts / 'plan.json', plan)
            print(json.dumps({'mode': plan['mode'], 'categories': plan['category_counts'], 'errors': plan['errors']}), flush=True)
            if plan['errors']:
                raise shared.Refused('; '.join(plan['errors']))
            if not args.plan_only:
                result = execute(root, data, plan, records, artifacts)
                shared.write_json(artifacts / 'result.json', result)
        return 0
    except (shared.Refused, OSError, ValueError) as error:
        shared.write_json(artifacts / 'result.json', {'status': 'blocked', 'error': str(error)})
        print('Push blocked: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
