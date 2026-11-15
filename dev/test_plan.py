#!/usr/bin/env python3
"""Generate a conservative pytest inventory, explain impact, and execute host jobs.

Inventory and plans are evidence of selection only. They never waive the full
pre-push gate or qualify required Linux, volume, runtime, browser or PID1 proofs.
"""
import argparse
import ast
import fcntl
from collections import Counter, defaultdict
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import sysconfig
import tempfile
import time

__test__ = False
HERE = Path(__file__).resolve().parent
SCHEMA = 1
MAX_FILE = 65536
MAX_FILES = 8
EXCLUSIVE_API_SYMBOLS = {'statvfs', 'fstatvfs', 'disk_usage', 'fallocate', 'posix_fallocate', 'mount'}
RESOURCE_INDIRECTION = {'__import__', 'import_module', 'spec_from_file_location',
                        'module_from_spec', 'exec_module', 'run_module', 'run_path',
                        'exec', 'eval', 'compile', 'getattr', 'setattr', 'delattr',
                        'hasattr', 'vars', 'globals', 'locals', 'dir', '__getattribute__', '__getattr__',
                        '__setattr__', '__delattr__', '__dict__', '__builtins__', '__globals__',
                        '__code__', '__closure__', 'attrgetter', 'methodcaller', 'getattr_static',
                        'getmembers', 'load_module', 'find_spec', 'spec_from_loader'}
REQUIRED_PROOFS = ['linux', 'exclusive-volume-ext4', 'original-app-runtime', 'browser', 'pid1']


class Refused(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + '\n'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    Path(path).write_text(canonical(value), encoding='utf-8')


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    if result.returncode:
        raise Refused('Git query failed: ' + ' '.join(args[:2]))
    return result.stdout


def identity():
    return {'python': sys.version.split()[0], 'platform': sys.platform, 'machine': platform.machine(),
            'packages': dict(sorted((distribution.metadata['Name'], distribution.version)
                                    for distribution in importlib.metadata.distributions())),
            **{name: importlib.metadata.version(name) for name in ('pytest', 'pytest-xdist')}}


def generated_path(name):
    return (any(part in ('.git', '.codex-audit', '__pycache__', 'node_modules', '.local') for part in Path(name).parts) or
            Path(name).suffix in ('.pyc', '.pyo'))


def fingerprints(root):
    """Git enumerates the entire relevant tracked/untracked namespace, not mtimes."""
    paths = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')
    result = {}
    for raw in paths:
        if not raw:
            continue
        name = os.fsdecode(raw)
        if generated_path(name):
            continue
        path = root / name
        if path.is_symlink():
            raise Refused('Symlink input requires explicit dependency qualification: ' + name)
        elif path.is_file():
            result[name] = digest(path.read_bytes())
    return result


def tool_fingerprints():
    return {name: digest((HERE / name).read_bytes()) for name in
            ('test_plan.py', 'test_plan_plugin.py', 'test_plan_rules.json', 'test_runner.py')}


def rules_data(path=None):
    value = json.loads(Path(path or HERE / 'test_plan_rules.json').read_text())
    if value.get('schema') != SCHEMA:
        raise Refused('Unsupported rules schema')
    cap = value.get('profiles', {}).get('host', {}).get('cap')
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 10:
        raise Refused('Host cap exceeds qualified profile or is invalid')
    if value.get('required_proofs') != REQUIRED_PROOFS:
        raise Refused('Semantic rules cannot remove required platform proofs')
    if not isinstance(value.get('rules'), list):
        raise Refused('Invalid semantic rules')
    for rule in value['rules']:
        if not all(isinstance(rule.get(field), str) and rule[field] for field in ('name', 'pattern', 'profile')):
            raise Refused('Invalid semantic rule')
        re.search(rule['pattern'], '')
    return value


def guarded(root, arguments, directory, *, collect=False, deadline=1800):
    """Use the unchanged canonical supervisor and retain its complete manifest."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = directory / 'pytest.json'
    evidence = directory / 'runner'
    env = os.environ.copy()
    # Options belong only to this child; never mutate the parent environment.
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_PLUGINS', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONPATH'] = str(HERE) + os.pathsep + env.get('PYTHONPATH', '')
    env['TEST_PLAN_OUTPUT'] = str(output)
    env['TEST_PLAN_ROOT'] = str(root)
    command = [sys.executable, str(HERE / 'test_runner.py'), '--deadline', str(deadline),
               '--artifacts', str(evidence), '--', '--rootdir', str(root), '-p', 'test_plan_plugin',
               '-p', 'no:cacheprovider', *arguments]
    result = subprocess.run(command, cwd=root, env=env)
    manifests = list(evidence.glob('run-*/manifest.json'))
    if len(manifests) != 1 or not output.is_file():
        raise Refused('Missing/ambiguous protected evidence: ' + str(directory))
    manifest = json.loads(manifests[0].read_text())
    record = json.loads(output.read_text())
    if (result.returncode or manifest.get('raw_returncode') != 0 or
            manifest.get('exit_code') != 0 or manifest.get('status') != 'passed' or
            manifest.get('reason') != 'completed' or manifest.get('errors') or
            manifest.get('omitted_error_count') or record.get('exitstatus') != 0):
        raise Refused('Protected invocation failed: ' + str(directory))
    if record.get('schema') != SCHEMA or record.get('errors') or record.get('deselected'):
        raise Refused('Collection errors/deselection in owned evidence')
    return record, {'argv': command, 'manifest': str(manifests[0]), 'records': str(output)}


def source_graph(root, names):
    """Conservative local AST edges; unsupported influences remain explicit."""
    python = {name for name in names if name.endswith('.py')}
    modules = defaultdict(set)
    for name in python:
        p = Path(name)
        pieces = list(p.with_suffix('').parts)
        if pieces[-1] == '__init__':
            pieces.pop()
        for start in range(len(pieces)):
            modules['.'.join(pieces[start:])].add(name)
    edges, unknown, resources, resource_unknown = {}, {}, {}, {}
    try:
        stdlib = set(sys.stdlib_module_names)
    except AttributeError:
        stdlib = set()
    stdlib.update(sys.builtin_module_names)
    stdlib_path = Path(sysconfig.get_path('stdlib'))
    stdlib.update(p.stem for p in stdlib_path.glob('*.py'))
    stdlib.update(p.name for p in stdlib_path.iterdir() if p.is_dir())
    stdlib.update(p.name.split('.')[0] for p in (stdlib_path / 'lib-dynload').glob('*') if p.suffix in ('.so', '.pyd'))
    external = set()
    for distribution in importlib.metadata.distributions():
        external.update((distribution.read_text('top_level.txt') or '').split())
    for name in sorted(python):
        deps, problems, symbols, uncertain = set(), set(), set(), set()
        path = Path(name)
        for parent in path.parents:
            init = (parent / '__init__.py').as_posix()
            if init in python and init != name:
                deps.add(init)
        try:
            tree = ast.parse((root / name).read_bytes(), filename=name)
        except (SyntaxError, ValueError, OSError):
            edges[name], unknown[name], resources[name], resource_unknown[name] = [], ['AST unavailable'], [], ['AST unavailable']
            continue
        for node in ast.walk(tree):
            # Symbol references survive callable assignment aliases and local
            # helper indirection. This deliberately over-approximates the whole
            # dependency module; a host family is never a negative resource proof.
            if ((isinstance(node, ast.Name) and node.id in RESOURCE_INDIRECTION) or
                    (isinstance(node, ast.Attribute) and node.attr in RESOURCE_INDIRECTION)):
                uncertain.add('dynamic loader or reflective dispatch')
            if isinstance(node, ast.ImportFrom) and any(alias.name in RESOURCE_INDIRECTION for alias in node.names):
                uncertain.add('dynamic loader or reflective dispatch')
            if isinstance(node, ast.Attribute) and node.attr in EXCLUSIVE_API_SYMBOLS:
                symbols.add(node.attr)
            if isinstance(node, ast.ImportFrom):
                symbols.update(alias.name for alias in node.names if alias.name in EXCLUSIVE_API_SYMBOLS)
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = list(path.parent.parts)
                    if node.level > len(parts):
                        problems.add('unresolved relative import')
                        uncertain.add('unresolved import provenance')
                        continue
                    prefix = parts[:len(parts) - node.level + 1]
                    base = '.'.join(prefix + ([node.module] if node.module else []))
                else:
                    base = node.module or ''
                imports = [base] + [base + '.' + alias.name for alias in node.names if alias.name != '*']
                if any(alias.name == '*' for alias in node.names):
                    problems.add('wildcard import: ' + base)
                    uncertain.add('wildcard import')
            elif isinstance(node, ast.Call):
                function = node.func
                call = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ''
                # Any call may read files, launch Git/processes, make HTTP
                # requests, inspect environment, invoke an aliased dynamic API
                # or delegate to code outside the import graph. No negative
                # runtime-dependency proof is inferred from a lexical name.
                problems.add('unresolved runtime call influence')
            for module in imports:
                candidates = modules.get(module, set())
                if candidates:
                    deps.update(candidates)
                    if len(candidates) > 1:
                        problems.add('ambiguous local import: ' + module)
                        uncertain.add('ambiguous import provenance')
                elif module and module.split('.')[0] not in stdlib | external | {'__future__'}:
                    # from package import symbol may refer to an attribute; its
                    # parent import is sufficient only if it resolves locally.
                    parent = module.rsplit('.', 1)[0] if '.' in module else ''
                    if not modules.get(parent):
                        problems.add('unresolved import: ' + module)
                        uncertain.add('unresolved import provenance')
        edges[name] = sorted(deps - {name})
        problems.update('unresolved resource provenance: ' + reason for reason in uncertain)
        unknown[name] = sorted(problems)
        resource_unknown[name] = sorted(uncertain)
        resources[name] = sorted(symbols)
    return edges, unknown, resources, resource_unknown


def closure(seeds, edges):
    result, pending = set(), list(seeds)
    while pending:
        name = pending.pop()
        if name in result:
            continue
        result.add(name)
        pending.extend(edges.get(name, ()))
    return sorted(result)


def inventory(root, tests, artifacts, rules=None):
    root = Path(root).resolve()
    normalized = []
    for target in tests:
        try:
            normalized.append((root / target).resolve().relative_to(root).as_posix())
        except ValueError as error:
            raise Refused('Collection scope escapes the repository') from error
    tests = normalized
    before = fingerprints(root)
    tool = tool_fingerprints()
    record, evidence = guarded(root, ['--collect-only', '-n', '0', *tests], Path(artifacts))
    ids = record['collected']
    if len(ids) != len(set(ids)) or not ids:
        raise Refused('Empty or duplicate full collection')
    items = record['items']
    if Counter(item['id'] for item in items) != Counter(ids):
        raise Refused('Inventory metadata does not match full collection')
    edges, unknown, resources, resource_unknown = source_graph(root, before)
    rules_source = None
    if rules is not None:
        try:
            rules_source = (root / rules).resolve().relative_to(root).as_posix()
        except ValueError as error:
            raise Refused('Rules must be fingerprinted inside the repository') from error
    semantic = rules_data(root / rules_source if rules_source else None)
    nodes = []
    for item in sorted(items, key=lambda value: value['id']):
        seeds = [item['source']] if item['source'] else []
        seeds.extend(plugin['source'] for plugin in record['plugins'] if plugin['source'])
        seeds.extend(f['source'] for f in item['fixtures'] if f['source'])
        # conftest.py loading is implicit; include every ancestor provider.
        for parent in Path(item['source'] or '').parents:
            conftest = (parent / 'conftest.py').as_posix()
            if conftest in before:
                seeds.append(conftest)
        deps = closure(seeds, edges)
        issues = set(item['unknown'])
        for source in deps:
            issues.update(source + ': ' + issue for issue in unknown.get(source, ()))
        matches = [rule for rule in semantic['rules'] if re.search(rule['pattern'], item['id'])]
        profiles = {rule['profile'] for rule in matches}
        resource_sources = sorted(source for source in deps if resources.get(source))
        uncertain_sources = sorted(source for source in deps if resource_unknown.get(source))
        if uncertain_sources:
            profiles.add('unqualified')
        if resource_sources or any(re.search(semantic.get('exclusive_api_pattern', r'(?!)'), call) for call in item.get('calls', [])):
            profiles.add('exclusive-volume-ext4')
        profile = next(iter(profiles)) if len(profiles) == 1 else 'unqualified' if profiles else 'unknown'
        area = Path(item['source']).stem.removeprefix('test_') if item['source'] else 'unclassified'
        nodes.append({**item, 'area': area, 'dependencies': deps, 'unknown': sorted(issues),
                      'profile': profile, 'resource_sources': resource_sources, 'resource_unknown_sources': uncertain_sources, 'resource_rules': [rule['name'] for rule in matches]})
    if before != fingerprints(root) or tool != tool_fingerprints():
        raise Refused('Inputs changed during inventory')
    return {'schema': SCHEMA, 'identity': identity(), 'inputs': before, 'tools': tool,
            'rules': semantic, 'rules_source': rules_source, 'tests': list(tests),
            'source_analysis': {name: {'imports': edges[name], 'unknown': unknown[name], 'resource_symbols': resources[name], 'resource_unknown': resource_unknown[name]} for name in sorted(edges)},
            'nodes': nodes}, evidence


def validate_map(root, data):
    source = data.get('rules_source')
    if source is not None and (not isinstance(source, str) or Path(source).is_absolute() or '..' in Path(source).parts or source not in data.get('inputs', {})):
        raise Refused('Invalid map rules source')
    if data.get('schema') != SCHEMA or data.get('identity') != identity():
        return ['map schema/runtime identity is stale']
    if data.get('inputs') != fingerprints(root) or data.get('tools') != tool_fingerprints() or data.get('rules') != rules_data(root / data['rules_source'] if data.get('rules_source') else None):
        return ['map source/config/test/tool fingerprints are stale']
    ids = [node['id'] for node in data['nodes']]
    for node in data['nodes']:
        source = node.get('source')
        if (not isinstance(source, str) or Path(source).is_absolute() or '..' in Path(source).parts or
                source not in data['inputs'] or not node['id'].startswith(source + '::')):
            raise Refused('Invalid map source/selector')
    if not ids or len(ids) != len(set(ids)):
        raise Refused('Invalid map node IDs')
    return []


def changed_paths(root, base=None):
    reasons = []
    try:
        reference = base or git(root, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}').decode().strip()
        verified = git(root, 'rev-parse', '--verify', reference + '^{commit}').decode().strip()
        ancestor = git(root, 'merge-base', 'HEAD', verified).decode().strip()
        if not ancestor:
            raise Refused('No merge base')
        # Union independent layers; a dirty restoration must never cancel an
        # index change or unpublished commit. Inspect each unpublished commit
        # too, including changes later reverted by another unpublished commit.
        outputs = [git(root, 'diff', '--name-only', '--no-renames', '-z', ancestor, 'HEAD', '--'),
                   git(root, 'diff', '--cached', '--name-only', '--no-renames', '-z', '--'),
                   git(root, 'diff', '--name-only', '--no-renames', '-z', '--'),
                   git(root, 'ls-files', '--others', '--exclude-standard', '-z')]
        for commit in git(root, 'rev-list', ancestor + '..HEAD').decode().splitlines():
            outputs.append(git(root, 'diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-m', '-z', commit))
        paths = {os.fsdecode(p) for output in outputs for p in output.split(b'\0') if p}
        return sorted(path for path in paths if not generated_path(path)), {'requested': base, 'reference': reference, 'commit': verified, 'merge_base': ancestor}, reasons
    except Refused as error:
        return [], None, ['unverified change base: ' + str(error)]


def make_plan(root, data, base=None):
    root = Path(root).resolve()
    reasons = validate_map(root, data)
    changed, base_info, base_errors = changed_paths(root, base)
    reasons.extend(base_errors)
    known = set(data['inputs']) | {dep for node in data['nodes'] for dep in node['dependencies']}
    for path in changed:
        if path not in known:
            reasons.append('unresolved changed path: ' + path)
        if Path(path).suffix not in ('.py', '.md'):
            reasons.append('non-Python semantic influence is unresolved: ' + path)
        if (Path(path).name in ('pytest.ini', '.pytest.ini', 'pyproject.toml', 'setup.cfg', 'tox.ini', 'conftest.py') or
                path.startswith(('dev/', '.githooks/')) or 'requirements' in path):
            reasons.append('shared test/configuration boundary: ' + path)
    dynamic = sorted({issue for node in data['nodes'] for issue in node['unknown']})
    if changed and dynamic:
        reasons.append('unresolved dependency influence: ' + '; '.join(dynamic))
    decisions = []
    for node in data['nodes']:
        affected = sorted(set(node['dependencies']) & set(changed))
        selected = bool(reasons or affected)
        decisions.append({'id': node['id'], 'selected': selected, 'profile': node['profile'],
                          'reason': 'full fallback' if reasons else 'changed dependency: ' + ', '.join(affected) if affected else 'no changed known dependency',
                          'dependencies': affected})
    return {'schema': SCHEMA, 'map_digest': digest(canonical(data).encode()),
            'identity': identity(), 'inputs': fingerprints(root), 'tools': tool_fingerprints(),
            'requested_base': base, 'base': base_info, 'changed': changed, 'full_reasons': sorted(set(reasons)),
            'decisions': decisions, 'required_proofs': data['rules']['required_proofs'],
            'qualification': 'selection-only; full pre-push gate unchanged'}


def selector_batches(ids):
    if len(ids) != len(set(ids)):
        raise Refused('Duplicate selected IDs')
    batches, files, current, size = [], [], [], 0
    for nodeid in ids:
        if not nodeid or '\n' in nodeid or '\r' in nodeid or '\0' in nodeid or nodeid.startswith(('@', '-')):
            raise Refused('Unrepresentable selector')
        encoded = len((nodeid + '\n').encode('utf-8'))
        if encoded > MAX_FILE:
            raise Refused('Selector exceeds 64 KiB')
        if size + encoded > MAX_FILE:
            files.append(current)
            current, size = [], 0
            if len(files) == MAX_FILES:
                batches.append(files)
                files = []
        current.append(nodeid)
        size += encoded
    if current:
        files.append(current)
    if files:
        batches.append(files)
    return batches


def available_cpus():
    try:
        count = len(os.sched_getaffinity(0))
        if count > 0:
            return count, 'sched_getaffinity'
    except (AttributeError, OSError):
        pass
    return max(1, os.cpu_count() or 1), 'os.cpu_count fallback'


def verify_execution(record, expected, workers):
    expected = list(expected)
    collections = record['worker_collections'] if workers > 0 else [record['collected']]
    if not collections or any(Counter(ids) != Counter(expected) for ids in collections):
        raise Refused('Runtime collection does not match exact expected IDs')
    reports = defaultdict(list)
    expected_set = set(expected)
    for report in record['reports']:
        if report['id'] not in expected_set:
            raise Refused('Unexpected runtime ID')
        reports[report['id']].append(report)
    if set(reports) != set(expected):
        raise Refused('Missing runtime outcomes')
    for nodeid, phases in reports.items():
        if Counter(report['when'] for report in phases) != Counter(('setup', 'call', 'teardown')):
            raise Refused('Missing/duplicate runtime phase: ' + nodeid)
        if any(report['outcome'] != 'passed' or report['xfail'] for report in phases):
            raise Refused('Failed/skipped/xfail runtime outcome: ' + nodeid)


def run_plan(root, data, plan, artifacts, *, deadline=1800):
    root, artifacts = Path(root).resolve(), Path(artifacts).resolve()
    if validate_map(root, data):
        raise Refused('Stale map cannot execute; recollect first')
    if (plan.get('schema') != SCHEMA or plan.get('map_digest') != digest(canonical(data).encode()) or
            plan.get('inputs') != fingerprints(root) or plan.get('tools') != tool_fingerprints() or plan.get('identity') != identity()):
        raise Refused('Stale/incompatible plan')
    by_id = {node['id']: node for node in data['nodes']}
    recomputed = make_plan(root, data, plan.get('requested_base'))
    if canonical(recomputed) != canonical(plan):
        raise Refused('Saved plan decisions/change basis do not match current authoritative plan')
    decisions = plan['decisions']
    if plan.get('required_proofs') != data['rules']['required_proofs']:
        raise Refused('Plan dropped required platform proofs')
    if Counter(row['id'] for row in decisions) != Counter(by_id.keys()):
        raise Refused('Plan does not account for exact inventory')
    chosen = [row['id'] for row in decisions if row['selected']]
    blocked = [{'id': nodeid, 'profile': by_id[nodeid]['profile'], 'reason': 'profile route is not qualified/supported'} for nodeid in chosen if by_id[nodeid]['profile'] != 'host']
    ordinary = [nodeid for nodeid in chosen if by_id[nodeid]['profile'] == 'host']
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
    cpu, cpu_method = available_cpus()
    cap = data['rules']['profiles']['host']['cap']
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise Refused('Invalid qualified profile cap')
    result = {'schema': SCHEMA, 'available_cpu': cpu, 'cpu_method': cpu_method,
              'profile_cap': cap, 'jobs': [], 'blocked': blocked,
              'required_proofs': [{'profile': name, 'status': 'incomplete'} for name in plan['required_proofs']],
              'status': 'incomplete', 'receipt_reuse': False}
    common = Path(os.fsdecode(git(root, 'rev-parse', '--git-common-dir')).strip())
    common = common if common.is_absolute() else root / common
    lock_fd = os.open(common / 'test-plan-host.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused('Another scheduler owns this repository resource route') from error
        fresh, _ = inventory(root, data['tests'], artifacts / 'preflight-collection',
                             root / data['rules_source'] if data.get('rules_source') else None)
        if canonical(fresh) != canonical(data):
            raise Refused('Current full collection/environment differs; recollect map and plan')
        for index, batch in enumerate(selector_batches(ordinary)):
            if (validate_map(root, data) or fingerprints(root) != plan['inputs'] or
                    canonical(make_plan(root, data, plan.get('requested_base'))) != canonical(plan)):
                raise Refused('Inputs changed before dispatch')
            job = artifacts / ('job-' + str(index))
            job.mkdir(mode=0o700)
            selectors, ids = [], []
            for number, entries in enumerate(batch):
                path = job / ('selectors-' + str(number) + '.txt')
                path.write_text('\n'.join(entries) + '\n', encoding='utf-8')
                selectors.append('@' + str(path))
                ids.extend(entries)
            workers = min(cpu, cap, len(ids))
            began = time.monotonic()
            record, evidence = guarded(root, ['-n', str(workers), '--dist', 'worksteal', *selectors], job / 'execution', deadline=deadline)
            verify_execution(record, ids, workers)
            if validate_map(root, data):
                raise Refused('Inputs changed during execution')
            result['jobs'].append({'profile': 'host', 'workers': workers, 'expected': ids,
                                   'completed': sorted({row['id'] for row in record['reports']}),
                                   'elapsed': time.monotonic() - began, **evidence})
        result['host_status'] = 'passed'
        result['status'] = 'passed' if not blocked and not result['required_proofs'] else 'incomplete'
    except (Refused, OSError, ValueError) as error:
        result['error'] = str(error)
        result['host_status'] = 'failed'
    finally:
        os.close(lock_fd)
    write_json(artifacts / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=HERE.parent)
    commands = parser.add_subparsers(dest='action', required=True)
    collect = commands.add_parser('collect')
    collect.add_argument('--output', type=Path, required=True)
    collect.add_argument('--artifacts', type=Path, required=True)
    collect.add_argument('--rules', type=Path, help='Qualified semantic rules inside the repository')
    collect.add_argument('tests', nargs='*', default=['ha-ops/tests'])
    plan_parser = commands.add_parser('plan')
    plan_parser.add_argument('--map', type=Path, required=True)
    plan_parser.add_argument('--base')
    plan_parser.add_argument('--output', type=Path, required=True)
    run = commands.add_parser('run')
    run.add_argument('--map', type=Path, required=True)
    run.add_argument('--plan', type=Path, required=True)
    run.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'collect':
            data, evidence = inventory(args.root, args.tests, args.artifacts, args.rules)
            write_json(args.output, data)
            write_json(args.artifacts / 'inventory-evidence.json', evidence)
            print('Collected ' + str(len(data['nodes'])) + ' exact IDs; selection evidence only')
        elif args.action == 'plan':
            data = json.loads(args.map.read_text())
            plan = make_plan(args.root, data, args.base)
            write_json(args.output, plan)
            print('Selected ' + str(sum(row['selected'] for row in plan['decisions'])) + '; full pre-push gate unchanged')
        else:
            result = run_plan(args.root, json.loads(args.map.read_text()), json.loads(args.plan.read_text()), args.artifacts)
            print(result['status'] + ': ' + str(args.artifacts / 'result.json'))
            return 0 if result['status'] == 'passed' else 1
        return 0
    except (Refused, OSError, ValueError, KeyError) as error:
        print('Test planner refused: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
