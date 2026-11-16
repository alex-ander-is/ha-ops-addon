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
import test_receipts as receipts
import test_receipt_linux as qualified_linux

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


# Exact reviewed shared inputs. These compose backend dispatch, generated UI and
# source-contract checks across domains; all *generated* categories are the safe
# boundary. This is deliberately not an app/*.py or packaging-directory rule.
SHARED_INPUTS = frozenset({
    '.gitignore', 'requirements-dev.txt', 'ha-ops/Dockerfile', 'ha-ops/build.yaml',
    'ha-ops/runtime-apk-aarch64.lock', 'ha-ops/runtime-apk-amd64.lock',
    'ha-ops/runtime-install.sh', 'ha-ops/runtime-pip.lock', 'ha-ops/runtime-profile.json',
    'ha-ops/frontend/package.json', 'ha-ops/frontend/package-lock.json',
    'ha-ops/frontend/src/ha-ops.js', 'ha-ops/frontend/dist/ha-ops.js',
    'ha-ops/app/static/ha-ops.js', 'ha-ops/app/server.py', 'ha-ops/dev_harness.py',
    *('ha-ops/app/' + name + '.py' for name in (
        'app_context', 'backups', 'dev_harness', 'direct_access', 'direct_auth',
        'git_auth', 'i18n', 'jobs', 'state', 'supervisor', 'sync', 'web')),
})
# Native module names are reviewed too: a new module needs a semantic decision
# even when a newly collected test happens to give it a matching category.
NATIVE_INPUTS = frozenset('ha-ops/app/native_' + name + '.py' for name in (
    'apply', 'status', 'scope', 'migration_transition', 'storage', 'operations',
    'migration', 'inventory', 'publication', 'services', 'preview', 'save', 'git',
    'metadata', 'disk', 'recovery', 'migration_git_candidate', 'runtime',
    'migration_review', 'migration_review_http'))
DOCUMENTATION = frozenset({'AGENTS.md', 'TESTING.md', 'README.md', 'Backlog.md',
                           'TODO.md', 'UI_COMPONENT_AUDIT.md', 'ha-ops/tests/browser/README.md'})
# Browser operations also exercise mixed HTTP/state and shared UI contracts.
# Required primary families survive deletion; broad adjacent families below are
# intersected with current membership so optional families are not invented.
UI_AREAS = MIXED_AREAS | {'ui-common', 'migration', 'release', 'native-apply',
                         'native-apply_integration', 'native-migration_review_http'}
BROWSER_FAMILIES = {
    'reset-git-state.mjs': {'reset'}, 'reset_git_state_fixture.py': {'reset'},
    'preview-decisions-source.cjs': {'ui-common'},
    'migration-review.mjs': {'migration', 'native-migration_review', 'native-migration_review_http'},
    'migration-review-lifecycle.mjs': {'migration', 'native-migration_review', 'native-migration_review_http'},
    'migration-review-interception.mjs': {'migration', 'native-migration_review', 'native-migration_review_http'},
    'migration-review-interception-test.mjs': {'migration', 'native-migration_review', 'native-migration_review_http'},
    'migration_review_fixture.py': {'migration', 'native-migration_review', 'native-migration_review_http'},
    'storage-compact.mjs': {'native-storage', 'native-compact'},
    'storage-header-source.cjs': {'native-storage', 'native-compact'},
    'storage_compact_fixture.py': {'native-storage', 'native-compact'},
    'direct-access.mjs': {'backend-common'}, 'direct_fixture.py': {'backend-common'},
    'backup-continuation.mjs': {'backend-common'},
    'folder-scope.mjs': {'native-scope', 'native-apply'},
    'folder_scope_fixture.py': {'native-scope', 'native-apply'},
}
SHARED_BROWSER = frozenset('ha-ops/tests/browser/' + name for name in
                           ('run.mjs', 'native_apply_fixture.py', 'linux_bridge.py'))
TOPOLOGY_INPUTS = frozenset({
    'ha-ops/tests/native_topology_helpers.py', 'ha-ops/tests/integration/Dockerfile',
    'ha-ops/tests/integration/container_probe.py', 'ha-ops/tests/integration/fixture_cleanup.py',
    'ha-ops/tests/integration/run_native_topology.py', '.github/workflows/native-topology.yml',
})


def areas_for_path(path, nodes):
    """Reviewed semantic authority first; AST reverse edges only add coverage."""
    categories = {category(node) for node in nodes}
    canonical = (bool(path) and not Path(path).is_absolute() and
                 Path(path).as_posix() == path and '..' not in Path(path).parts and
                 path != '.' and '\\' not in path)
    if not canonical:
        return set(), 'Unknown influence: ' + path + '; noncanonical repository path'

    def reviewed(areas):
        # dependencies is the existing transitive AST closure. It cannot prove
        # absence of dynamic/data/process influence and never admits a new path.
        reverse = {category(node) for node in nodes if path in node.get('dependencies', [])}
        return set(areas) | reverse, None

    python_name = re.fullmatch(r'[a-z_][a-z0-9_]*\.py', Path(path).name) is not None
    collected_python = (python_name and path.startswith('ha-ops/tests/') and
                        any(node['source'] == path for node in nodes))
    declared_root_module = re.fullmatch(r'ha-ops/tests/test_[a-z0-9_]+\.py', path) is not None
    if collected_python or declared_root_module:
        if Path(path).name in MIXED:
            return reviewed(MIXED_AREAS)
        surviving = {category(node) for node in nodes if node['source'] == path}
        return reviewed(surviving or {category({'source': path, 'id': path + '::deleted'})})
    if path in DOCUMENTATION or path.startswith('ha-ops/docs/') and path.endswith('.md'):
        return reviewed(set())
    if path in SHARED_INPUTS or path in SHARED_BROWSER:
        # Shared browser runner/bridge/harness serves every operation and its
        # backend/source-contract consumers, including newly collected families.
        return reviewed(categories)
    if path in TOPOLOGY_INPUTS or path == 'ha-ops/tests/network_publication_helpers.py':
        required = {'native-topology'} if path in TOPOLOGY_INPUTS else {'native-network_publication', 'native-scope'}
        return reviewed(required | {c for c in categories if c.startswith('native-')} |
                        (categories & {'virtualization', 'release', 'runner'}))
    if path == '.github/workflows/local-virtualization.yml':
        return reviewed({'virtualization', 'runner'} | (categories & {'native-topology'}))
    browser = 'ha-ops/tests/browser/'
    if path.startswith(browser) and path[len(browser):] in BROWSER_FAMILIES:
        return reviewed(BROWSER_FAMILIES[path[len(browser):]] | MIXED_AREAS |
                        {'ui-common'} | (categories & UI_AREAS))
    if path == 'ha-ops/app/static/ha-ops.css':
        return reviewed({'ui-common'} | (categories & UI_AREAS))
    if path in ('release', 'publish-release', 'release-policy.py', 'ha-ops/config.yaml', 'ha-ops/CHANGELOG.md'):
        return reviewed({'release'})
    if path.startswith('dev/virtualization/'):
        return reviewed({'virtualization', 'runner'})
    if path.startswith(('dev/', '.githooks/')):
        return reviewed({'runner'})
    if path == 'ha-ops/app/reset_git.py':
        # Existing qualified feature boundary: shared server AST imports would
        # otherwise spuriously turn a Reset-only change into Logs/native work.
        return {'reset', 'backend-common'}, None
    if path in NATIVE_INPUTS:
        area = 'native-' + Path(path).stem[7:]
        return reviewed({c for c in categories if c.startswith('native-')} | {area, 'backend-common'})
    if path == 'ha-ops/frontend/src/migration-review.js':
        return reviewed({'migration', 'ui-common'})
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



def verification_plan(data, area):
    if area != 'reset':
        raise shared.Refused('Only reviewed Reset category verification is supported')
    decisions = [{'id': node['id'], 'category': category(node), 'selected': category(node) == area,
                  'reason': 'explicit local category verification'} for node in data['nodes']]
    count = sum(row['selected'] for row in decisions)
    if not count:
        raise shared.Refused('Empty verification category')
    return {'schema': 1, 'mode': 'category-verification', 'verified_category': area,
            'changed': [], 'refs': [], 'errors': [], 'decisions': decisions,
            'inputs': data['inputs'], 'tools': data['tools'],
            'inventory_digest': shared.digest(shared.canonical(data).encode()), 'category_counts': {area: count}}

def verify_outcomes(record, expected, workers):
    """Exact machine accounting, with host skips explicitly unproven."""
    if record.get('errors') or record.get('deselected') or not receipts.zero(record.get('exitstatus')):
        raise shared.Refused('Failed/error/deselected machine record')
    reports = defaultdict(list)
    for row in record['reports']:
        reports[row['id']].append(row)
    if set(reports) != set(expected):
        raise shared.Refused('Missing/unexpected runtime outcome')
    collections = record['worker_collections'] if workers else [record['collected']]
    if len(collections) != (workers or 1) or any(Counter(ids) != Counter(expected) for ids in collections):
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
        current_plan = (verification_plan(data, plan['verified_category']) if plan['mode'] == 'category-verification' else
                        make_plan(root, data, records, plan['mode'] == 'full'))
        if shared.validate_map(root, data) or current_plan != plan:
            raise shared.Refused('Selection inputs changed; recollect')
    fresh()
    selected = [row['id'] for row in plan['decisions'] if row['selected']]
    unsafe = [node['id'] for node in data['nodes'] if node['id'] in set(selected) and unsafe_host_node(node)]
    if unsafe:
        raise shared.Refused('Exclusive resource proof requires qualified isolated Linux/ext4 route: ' + ', '.join(unsafe))
    groups = defaultdict(list)
    for node in data['nodes']:
        if node['id'] in set(selected):
            groups[category(node)].append(node)
    result = {'status': 'passed', 'jobs': [], 'not_proven': [], 'categories': {},
              'platform_proofs': 'separate existing qualification obligations; host skips do not prove them'}
    cpu, _ = shared.available_cpus()
    cap = min(10, data['rules']['profiles']['host']['cap'], cpu)
    store = receipts.Store(root)
    candidates, retained_jobs, initial = {}, {}, {}

    def bound(area, nodes, profile):
        return receipts.binding(root, data, area, nodes, profile, category)

    # Ordinary work always precedes the bounded isolated controller phase.
    for area, nodes in sorted(groups.items(), key=lambda pair: (any(Path(n['source']).name in ISOLATED for n in pair[1]), pair[0])):
        isolated = any(Path(node['source']).name in ISOLATED for node in nodes)
        phase = 'isolated' if isolated else 'ordinary'
        batches = list(shared.selector_batches([node['id'] for node in nodes]))
        try:
            qualified_linux.contract(root, data, area, nodes)
            linux_route = True
        except (shared.Refused, OSError, ValueError, KeyError):
            linux_route = False
        profile = {'route': 'qualified-reset-linux-v1' if linux_route else 'canonical-host', 'phase': phase, 'dist': 'worksteal',
                   'workers': [min(2 if isolated else min(cap, 8) if linux_route else cap, sum(len(ids) for ids in batch)) for batch in batches]}
        current = None
        try:
            current = bound(area, nodes, profile)
            initial[area] = (nodes, profile, current)
            reusable, reason = store.lookup(area, current)
        except shared.Refused as error:
            reusable, reason = False, str(error)
        fresh()
        if linux_route and current is None:
            raise shared.Refused('Qualified Linux input binding is unavailable: ' + reason)
        if reusable:
            if bound(area, nodes, profile) != current:
                raise shared.Refused('Receipt inputs changed during lookup')
            result['categories'][area] = {'status': 'reused', 'reason': reason,
                                          'binding': shared.digest(shared.canonical(current).encode())}
            continue
        result['categories'][area] = {'status': 'executed', 'reason': reason}
        category_jobs, complete = [], current is not None
        for index, batch in enumerate(batches):
            fresh()
            if current is not None and bound(area, nodes, profile) != current:
                raise shared.Refused('Receipt inputs changed before dispatch')
            job = artifacts / (area + '-' + phase + '-' + str(index))
            job.mkdir(mode=0o700)
            arguments, expected = [], []
            for number, entries in enumerate(batch):
                selector = job / ('selectors-' + str(number) + '.txt')
                selector.write_text('\n'.join(entries) + '\n')
                arguments.append('@' + str(selector))
                expected.extend(entries)
            workers = profile['workers'][index]
            launch_args = ['-n', str(workers), '--dist', 'worksteal', *arguments]
            if linux_route:
                record, evidence = qualified_linux.launch(root, data, nodes, launch_args, job / 'execution', current['inputs'])
            else:
                record, evidence = launch(root, launch_args, job / 'execution')
            skipped = verify_outcomes(record, expected, workers)
            result['not_proven'].extend(skipped)
            fresh()
            if current is not None and bound(area, nodes, profile) != current:
                raise shared.Refused('Receipt inputs changed during execution')
            if skipped:
                complete = False
                result['categories'][area]['proof'] = 'not-proven: skipped nodes'
            elif complete:
                try:
                    captured = evidence.get('qualified_job') if linux_route else receipts.capture(root, record, evidence, expected, workers, arguments, job / 'execution')
                    receipts.validate_job(captured)
                    key = shared.digest(shared.canonical(captured).encode())
                    retained_jobs[key] = captured
                    category_jobs.append(key)
                except (shared.Refused, KeyError, OSError, TypeError, ValueError) as error:
                    complete = False
                    result['categories'][area]['proof'] = 'not-proven: ' + str(error)
            result['jobs'].append({'category': area, 'phase': phase, 'workers': workers, 'expected': expected, **evidence})
        if complete:
            candidates[area] = {'binding': current, 'jobs': category_jobs}
            result['categories'][area]['proof'] = 'complete success candidate'
    # No publication until every sibling job and final authoritative freshness
    # check succeeds. Old independent success is preserved after any later failure.
    fresh()
    for area, (nodes, profile, current) in initial.items():
        if bound(area, nodes, profile) != current:
            raise shared.Refused('Receipt inputs changed before publication')
    store.publish(candidates, retained_jobs)

    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--records', type=Path)
    parser.add_argument('--verify-category', choices=['reset'])
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--full', action='store_true', default=os.environ.get('HA_OPS_PRE_PUSH_MODE') == 'full')
    args = parser.parse_args(argv)
    if args.verify_category and (args.records or args.full):
        parser.error('--verify-category is exclusive with --records/--full')
    if not args.verify_category and not args.records:
        parser.error('--records is required for push verification')
    root = args.root.resolve()
    artifacts = args.artifacts or Path(tempfile.mkdtemp(prefix='ha-ops-selective-prepush-'))
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    print('Selective pre-push evidence: ' + str(artifacts), flush=True)
    try:
        records = args.records.read_text() if args.records else ''
        common = Path(os.fsdecode(shared.git(root, 'rev-parse', '--git-common-dir')).strip())
        common = common if common.is_absolute() else root / common
        with (common / 'test-plan-host.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data, _ = shared.inventory(root, ['ha-ops/tests'], artifacts / 'collection')
            shared.write_json(artifacts / 'inventory.json', data)
            plan = verification_plan(data, args.verify_category) if args.verify_category else make_plan(root, data, records, args.full)
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
