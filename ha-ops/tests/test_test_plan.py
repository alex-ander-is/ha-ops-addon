"""Portable generated-map contracts; all child jobs use disposable Git projects."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
import dev.test_plan as planner


def command(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          env={**os.environ, 'GIT_AUTHOR_NAME': 'Fixture',
                               'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                               'GIT_COMMITTER_NAME': 'Fixture',
                               'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'})


def project(tmp_path, files):
    root = tmp_path / 'project'
    root.mkdir()
    command(root, 'init', '-b', 'main')
    rules = planner.rules_data()
    rules['rules'] = [{'name': 'qualified-disposable-fixtures', 'pattern': '^tests/', 'profile': 'host'}]
    put(root, {'fixture-rules.json': json.dumps(rules), **files})
    command(root, 'add', '.')
    command(root, 'commit', '-m', 'Fixture baseline')
    return root


def put(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def collect(root, tmp_path, label='inventory'):
    return planner.inventory(root, ['tests'], tmp_path / label, root / 'fixture-rules.json')[0]


def test_real_inventory_fixture_closure_param_ids_and_stable_bytes(tmp_path, monkeypatch):
    root = project(tmp_path, {
        'pkg/__init__.py': 'VALUE = 1\n',
        'pkg/helper.py': 'from . import VALUE\n',
        'conftest.py': 'import pytest\nfrom pkg import helper\n@pytest.fixture\ndef outer(): return helper\n',
        'tests/conftest.py': 'import pytest\n@pytest.fixture\ndef inner(outer): return outer\n',
        'tests/test_alpha.py': 'import pytest\n@pytest.mark.parametrize("value", ["a b", "é,[x]::z"])\ndef test_value(inner,value): assert inner\n',
        'tests/nested/test_unclassified.py': 'def test_fresh(): pass\n',
    })
    # Owned collection clears inherited parent selectors without global leakage.
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k nonexistent')
    first = collect(root, tmp_path, 'first')
    second = collect(root, tmp_path, 'second')
    assert planner.canonical(first) == planner.canonical(second)
    assert len(first['nodes']) == 3
    assert os.environ['PYTEST_ADDOPTS'] == '-k nonexistent'
    node = next(n for n in first['nodes'] if 'a b' in n['id'])
    assert {'pkg/helper.py', 'pkg/__init__.py', 'conftest.py', 'tests/conftest.py', 'tests/test_alpha.py'} <= set(node['dependencies'])
    assert {'outer', 'inner'} <= {f['name'] for f in node['fixtures']}
    assert any('nested/test_unclassified' in n['id'] for n in first['nodes'])


@pytest.mark.parametrize('configuration', ['[pytest]\naddopts = -k absent\n', '[pytest]\naddopts = -m absent\n', '[pytest]\naddopts = --deselect=tests/test_one.py::test_one\n'])
def test_config_filters_cannot_make_partial_inventory_green(tmp_path, configuration):
    root = project(tmp_path, {'pytest.ini': configuration, 'tests/test_one.py': 'def test_one(): pass\n'})
    with pytest.raises(planner.Refused):
        collect(root, tmp_path)


def test_plugin_deselection_and_collection_error_are_red(tmp_path):
    root = project(tmp_path, {'tests/test_one.py': 'def test_one(): pass\n',
        'conftest.py': 'def pytest_collection_modifyitems(config, items):\n gone=items[:1]\n items[:1]=[]\n config.hook.pytest_deselected(items=gone)\n'})
    with pytest.raises(planner.Refused):
        collect(root, tmp_path, 'filtered')
    (root / 'conftest.py').unlink()
    (root / 'tests/test_one.py').write_text('def invalid(:\n')
    with pytest.raises(planner.Refused):
        collect(root, tmp_path, 'broken')


def test_git_unpublished_staged_dirty_rename_delete_and_untracked(tmp_path):
    root = project(tmp_path, {'source space.py': 'A=1\n', 'delete.py': 'A=1\n', 'dirty.py': 'A=1\n', 'staged.py': 'A=1\n'})
    base = command(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    command(root, 'branch', 'upstream', base)
    command(root, 'branch', '--set-upstream-to=upstream')
    for number in (1, 2):
        put(root, {'commit' + str(number) + '.py': 'A=1\n'})
        command(root, 'add', '.')
        command(root, 'commit', '-m', 'Unpublished ' + str(number))
    command(root, 'mv', 'source space.py', 'renamed space.py')
    (root / 'delete.py').unlink()
    put(root, {'dirty.py': 'A=2\n', 'staged.py': 'A=2\n', 'new space.py': 'A=1\n'})
    command(root, 'add', 'staged.py')
    changed, base_info, errors = planner.changed_paths(root)
    assert not errors and base_info['merge_base'] == base
    assert set(changed) == {'commit1.py', 'commit2.py', 'source space.py', 'renamed space.py', 'delete.py', 'dirty.py', 'staged.py', 'new space.py'}
    assert planner.changed_paths(root, 'absent-ref')[2]


def test_explained_closure_unknown_and_stale_new_inputs(tmp_path):
    root = project(tmp_path, {'helper.py': 'VALUE=1\n',
                             'tests/test_one.py': 'import helper\ndef test_one(): assert helper.VALUE\n',
                             'tests/test_two.py': 'def test_two(): pass\n'})
    base = command(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    # Map describes current sources; change base is older than the map.
    put(root, {'helper.py': 'VALUE=2\n'})
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, base)
    assert not plan['full_reasons']
    assert {row['id'] for row in plan['decisions'] if row['selected']} == {'tests/test_one.py::test_one'}
    assert all(row['reason'] for row in plan['decisions'])
    assert len(plan['decisions']) == len(data['nodes'])
    # Same size/mtime content changes are still stale.
    source = root / 'helper.py'
    stamp = source.stat().st_mtime_ns
    source.write_text('VALUE=3\n')
    os.utime(source, ns=(stamp, stamp))
    assert planner.make_plan(root, data, base)['full_reasons']
    put(root, {'tests/test_new.py': 'def test_new(): pass\n'})
    assert planner.validate_map(root, data)
    with pytest.raises(planner.Refused, match='Stale map'):
        planner.run_plan(root, data, plan, tmp_path / 'cannot-run')
    put(root, {'tests/test_two.py': 'import importlib\ndef test_two(): importlib.import_module("helper")\n'})
    data = collect(root, tmp_path, 'dynamic')
    plan = planner.make_plan(root, data, base)
    assert any('unresolved dependency influence' in reason for reason in plan['full_reasons'])
    assert all(row['selected'] for row in plan['decisions'])


def test_shadow_fixture_chain_and_wildcard_are_conservative(tmp_path):
    root = project(tmp_path, {'pkg/__init__.py': 'VALUE=1\n',
        'conftest.py': 'import pytest\n@pytest.fixture\ndef value(): return 1\n',
        'tests/conftest.py': 'import pytest\n@pytest.fixture\ndef value(value): return value+1\n',
        'tests/test_one.py': 'from pkg import *\ndef test_one(value): assert value==2\n'})
    data = collect(root, tmp_path)
    node = data['nodes'][0]
    assert {'conftest.py', 'tests/conftest.py', 'pkg/__init__.py'} <= set(node['dependencies'])
    assert any('wildcard' in problem for problem in node['unknown'])


def test_selector_files_are_disjoint_bounded_utf8_and_lossless():
    ids = ['tests/test_long.py::test_x[' + str(n) + ' é a,b::[]' + 'z' * 1000 + ']' for n in range(800)]
    batches = planner.selector_batches(ids)
    assert len(batches) > 1
    assert [value for batch in batches for file in batch for value in file] == ids
    assert all(len(batch) <= 8 for batch in batches)
    assert all(len(('\n'.join(file) + '\n').encode()) <= 65536 for batch in batches for file in batch)
    for bad in ('bad\nselector', 'x' * 65536, '@nested', '-kfilter'):
        with pytest.raises(planner.Refused):
            planner.selector_batches([bad])
    with pytest.raises(planner.Refused):
        planner.selector_batches(['same', 'same'])


def records(ids):
    return {'collected': ids, 'worker_collections': [ids, ids],
            'reports': [{'id': nodeid, 'when': phase, 'outcome': 'passed', 'duration': 0, 'xfail': False}
                        for nodeid in ids for phase in ('setup', 'call', 'teardown')]}


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'skipped', 'foreign', 'partial-collection', 'teardown'])
def test_exact_runtime_accounting_refuses_false_success(mutation):
    record = records(['one', 'two'])
    if mutation == 'missing': record['reports'].pop()
    elif mutation == 'duplicate': record['reports'].append(record['reports'][-1])
    elif mutation == 'skipped': record['reports'][-1]['outcome'] = 'skipped'
    elif mutation == 'foreign': record['reports'][-1]['id'] = 'foreign'
    elif mutation == 'partial-collection': record['worker_collections'][-1].pop()
    elif mutation == 'teardown': record['reports'][-1]['outcome'] = 'failed'
    with pytest.raises(planner.Refused):
        planner.verify_execution(record, ['one', 'two'], 2)


def test_actual_worksteal_overlap_env_isolation_and_explicit_incomplete(tmp_path, monkeypatch):
    # Two initial items per worker guarantee worksteal has concurrent work.
    timeline = tmp_path / 'timeline.jsonl'
    root = project(tmp_path, {'tests/test_jobs.py': '''import json,os,time
import pytest
@pytest.mark.parametrize('number',range(6))
def test_job(number):
 path=os.environ['FIXTURE_TIMELINE']
 with open(path,'a') as f: f.write(json.dumps(['start',number,time.monotonic(),os.getpid()])+'\\n')
 time.sleep(.15)
 with open(path,'a') as f: f.write(json.dumps(['end',number,time.monotonic(),os.getpid()])+'\\n')
'''})
    monkeypatch.setenv('FIXTURE_TIMELINE', str(timeline))
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k absent')
    data = collect(root, tmp_path)
    base = command(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    # Missing base conservatively selects all current nodes.
    plan = planner.make_plan(root, data, 'absent')
    original_cpus = planner.available_cpus
    planner.available_cpus = lambda: (2, 'controlled qualified fixture')
    try:
        result = planner.run_plan(root, data, plan, tmp_path / 'jobs')
    finally:
        planner.available_cpus = original_cpus
    assert result['host_status'] == 'passed' and result['status'] == 'incomplete'
    assert result['required_proofs'] and not result['receipt_reuse']
    assert len(result['jobs']) == 1 and result['jobs'][0]['workers'] == 2
    assert len(result['jobs'][0]['completed']) == 6
    assert '--dist' in result['jobs'][0]['argv'] and 'worksteal' in result['jobs'][0]['argv']
    assert os.environ['PYTEST_ADDOPTS'] == '-k absent'
    events = [json.loads(line) for line in timeline.read_text().splitlines()]
    active, peak = set(), 0
    for kind, number, stamp, process in sorted(events, key=lambda event: event[2]):
        if kind == 'start': active.add(number)
        else: active.remove(number)
        peak = max(peak, len(active))
    assert peak == 2 and not active
    assert len({row[3] for row in events}) == 2


@pytest.mark.parametrize('body', ['assert False', 'pytest.skip("requires unavailable platform")', 'pass\n@pytest.fixture(autouse=True)\ndef teardown():\n yield\n assert False'])
def test_actual_failed_skip_and_teardown_jobs_cannot_pass(tmp_path, body):
    root = project(tmp_path, {'tests/test_jobs.py': 'import pytest\ndef test_job():\n ' + body + '\n'})
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, 'absent')
    result = planner.run_plan(root, data, plan, tmp_path / 'jobs')
    assert result['host_status'] == 'failed' and result['status'] == 'incomplete'
    assert result['error']


def test_actual_timeout_keeps_runner_evidence_red(tmp_path):
    root = project(tmp_path, {'tests/test_jobs.py': 'import time\ndef test_job(): time.sleep(20)\n'})
    # Collect with normal deadline, then use the existing runner's short
    # deadline only for the explicitly isolated hang fixture.
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, 'absent')
    result = planner.run_plan(root, data, plan, tmp_path / 'jobs', deadline=.4)
    assert result['host_status'] == 'failed'
    manifests = list((tmp_path / 'jobs').glob('job-*/execution/runner/run-*/manifest.json'))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())['reason'] == 'timeout'


def test_exclusive_and_unknown_profiles_are_blocked_without_dispatch(tmp_path):
    root = project(tmp_path, {'tests/test_capacity.py': 'import os\ndef test_admission(): os.statvfs(".")\n'})
    data = collect(root, tmp_path)
    assert data['nodes'][0]['profile'] == 'unqualified'
    plan = planner.make_plan(root, data, 'absent')
    result = planner.run_plan(root, data, plan, tmp_path / 'jobs')
    assert result['status'] == 'incomplete' and result['blocked']
    assert not result['jobs']


def test_actual_worker_early_zero_exit_is_incomplete(tmp_path):
    root = project(tmp_path, {'tests/test_jobs.py': 'import os\ndef test_job(): os._exit(0)\n'})
    data = collect(root, tmp_path)
    result = planner.run_plan(root, data, planner.make_plan(root, data, 'absent'), tmp_path / 'jobs')
    assert result['host_status'] == 'failed' and result['status'] == 'incomplete'


def test_saved_plan_cannot_drop_required_proofs(tmp_path):
    root = project(tmp_path, {'tests/test_jobs.py': 'def test_job(): pass\n'})
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, 'absent')
    plan['required_proofs'] = []
    with pytest.raises(planner.Refused, match='Saved plan|real proof|required platform proofs'):
        planner.run_plan(root, data, plan, tmp_path / 'jobs')


def test_non_python_semantic_change_expands_full(tmp_path):
    root = project(tmp_path, {'tests/test_jobs.py': 'def test_job(): pass\n', 'client.js': 'export const value = 1;\n'})
    base = command(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    put(root, {'client.js': 'export const value = 2;\n'})
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, base)
    assert any('non-Python semantic influence' in value for value in plan['full_reasons'])
    assert all(row['selected'] for row in plan['decisions'])


@pytest.mark.parametrize('test_source,extra', [
    ('from importlib import import_module as load\ndef test_one(): assert load("helper").VALUE==1\n', {}),
    ('import pytest\nfrom importlib import import_module as load\n@pytest.fixture\ndef value(): return load("helper").VALUE\ndef test_one(value): assert value==1\n', {}),
    ('from pathlib import Path\ndef test_one(): assert Path("helper.py").read_text()=="VALUE=1\\n"\n', {}),
    ('def test_one(): assert VALUE==1\n', {'behavior.py': 'def pytest_runtest_setup(item): item.module.VALUE=1\n', 'conftest.py': 'pytest_plugins=["behavior"]\n'}),
])
def test_dynamic_runtime_and_plugin_influences_cannot_be_excluded(tmp_path, test_source, extra):
    root = project(tmp_path, {'helper.py': 'VALUE=1\n', 'tests/test_one.py': test_source, **extra})
    base = command(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    changed = 'behavior.py' if extra else 'helper.py'
    put(root, {changed: 'def pytest_runtest_setup(item): item.module.VALUE=2\n' if extra else 'VALUE=2\n'})
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, base)
    assert all(row['selected'] for row in plan['decisions'])
    assert plan['full_reasons'] or changed in data['nodes'][0]['dependencies']
    if extra:
        assert 'behavior.py' in data['nodes'][0]['dependencies']


def test_silent_plugin_removal_cannot_shrink_inventory(tmp_path):
    root = project(tmp_path, {'tests/test_one.py': 'def test_one(): pass\ndef test_two(): pass\n',
                             'conftest.py': 'def pytest_collection_modifyitems(items): items.pop()\n'})
    with pytest.raises(planner.Refused):
        collect(root, tmp_path)


def test_git_layers_and_unpublished_reverts_never_cancel(tmp_path):
    root = project(tmp_path, {'helper.py': 'VALUE=1\n'})
    base = command(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    put(root, {'helper.py': 'VALUE=2\n'})
    command(root, 'add', 'helper.py')
    put(root, {'helper.py': 'VALUE=1\n'})
    assert 'helper.py' in planner.changed_paths(root, base)[0]
    command(root, 'commit', '-m', 'Unpublished')
    assert 'helper.py' in planner.changed_paths(root, base)[0]
    command(root, 'add', 'helper.py')
    command(root, 'commit', '-m', 'Unpublished restoration')
    assert 'helper.py' in planner.changed_paths(root, base)[0]


@pytest.mark.parametrize('filename', ['README.md', 'fixture.png'])
def test_doc_and_image_input_edits_stale_saved_map_and_plan(tmp_path, filename):
    root = project(tmp_path, {filename: 'old\n', 'tests/test_one.py': 'def test_one(): pass\n'})
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, command(root, 'rev-parse', 'HEAD').stdout.decode().strip())
    assert filename in data['inputs']
    put(root, {filename: 'new\n'})
    assert planner.validate_map(root, data)
    with pytest.raises(planner.Refused, match='Stale map'):
        planner.run_plan(root, data, plan, tmp_path / 'run')


def test_saved_plan_cannot_turn_off_required_selection(tmp_path):
    root = project(tmp_path, {'tests/test_one.py': 'def test_one(): assert False\n'})
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, 'absent')
    plan['decisions'][0]['selected'] = False
    with pytest.raises(planner.Refused, match='Saved plan'):
        planner.run_plan(root, data, plan, tmp_path / 'run')


def test_collection_environment_growth_is_refused_before_selected_dispatch(tmp_path, monkeypatch):
    root = project(tmp_path, {'tests/test_one.py': 'import os,pytest\n@pytest.mark.parametrize("value",os.getenv("FAILURE_VARIANTS","one").split(","))\ndef test_one(value): assert value=="one"\n'})
    monkeypatch.setenv('FAILURE_VARIANTS', 'one')
    data = collect(root, tmp_path)
    plan = planner.make_plan(root, data, 'absent')
    monkeypatch.setenv('FAILURE_VARIANTS', 'one,two')
    result = planner.run_plan(root, data, plan, tmp_path / 'run')
    assert result['host_status'] == 'failed' and not result['jobs']
    assert 'full collection/environment differs' in result['error']


def test_collection_creates_no_bytecache_and_generated_cache_is_not_change(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONDONTWRITEBYTECODE', raising=False)
    root = project(tmp_path, {'tests/test_one.py': 'def test_one(): pass\n'})
    command(root, 'config', 'core.excludesFile', '/dev/null')
    base = command(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    collect(root, tmp_path)
    assert not list(root.rglob('*.pyc'))
    assert not planner.changed_paths(root, base)[0]
    put(root, {'tests/__pycache__/test_one.cpython-312.pyc': 'generated'})
    assert not planner.changed_paths(root, base)[0]


def test_unnamed_resource_api_and_conflicting_profiles_are_blocked(tmp_path):
    root = project(tmp_path, {'tests/test_one.py': 'from os import statvfs as query\ndef test_root_statistics(): query(".")\n'})
    data = collect(root, tmp_path)
    assert data['nodes'][0]['profile'] == 'unqualified'
    assert 'os.statvfs' in data['nodes'][0]['calls']
    result = planner.run_plan(root, data, planner.make_plan(root, data, 'absent'), tmp_path / 'run')
    assert result['blocked'] and not result['jobs']
    # Removing the explicit fixture qualification must never infer host safety.
    default_data = planner.inventory(root, ['tests'], tmp_path / 'default')[0]
    assert default_data['nodes'][0]['profile'] == 'exclusive-volume-ext4'


def test_unmatched_resource_profile_is_unknown(tmp_path):
    root = project(tmp_path, {'tests/test_one.py': 'def test_one(): pass\n'})
    data = planner.inventory(root, ['tests'], tmp_path / 'default')[0]
    assert data['nodes'][0]['profile'] == 'unknown'


def test_rules_are_root_relative_and_foreign_saved_rules_are_refused(tmp_path):
    root = project(tmp_path, {'tests/test_one.py': 'def test_one(): pass\n'})
    data = planner.inventory(root, ['tests'], tmp_path / 'relative', Path('fixture-rules.json'))[0]
    assert data['rules_source'] == 'fixture-rules.json'
    assert data['nodes'][0]['profile'] == 'host'
    data['rules_source'] = '../foreign.json'
    with pytest.raises(planner.Refused, match='Invalid map rules source'):
        planner.validate_map(root, data)


@pytest.mark.parametrize('variant', ['assignment', 'helper', 'transitive-fixture', 'plugin'])
def test_resource_symbols_survive_aliases_helpers_fixtures_and_plugins(tmp_path, variant):
    files = {'tests/test_one.py': 'import helper\ndef test_statistics(): assert helper.measure()\n',
             'helper.py': 'import os\nquery=os.statvfs\ndef measure(): return query(".")\n'}
    if variant == 'assignment':
        files = {'tests/test_one.py': 'import os\nquery=os.statvfs\ndef test_statistics(): query(".")\n'}
    elif variant == 'transitive-fixture':
        files.update({'provider.py': 'from helper import measure\ndef value(): return measure()\n',
                      'conftest.py': 'import pytest\nimport provider\n@pytest.fixture\ndef sample(): return provider.value()\n',
                      'tests/test_one.py': 'def test_statistics(sample): assert sample\n'})
    elif variant == 'plugin':
        files.update({'behavior.py': 'from helper import measure\ndef pytest_runtest_setup(item): measure()\n',
                      'conftest.py': 'pytest_plugins=["behavior"]\n',
                      'tests/test_one.py': 'def test_statistics(): pass\n'})
    root = project(tmp_path, files)
    data = collect(root, tmp_path)
    node = data['nodes'][0]
    assert node['profile'] == 'unqualified'
    assert node['resource_sources']
    assert any('statvfs' in data['source_analysis'][source]['resource_symbols'] for source in node['resource_sources'])
    result = planner.run_plan(root, data, planner.make_plan(root, data, 'absent'), tmp_path / 'run')
    assert result['blocked'] and not result['jobs']


def test_unknown_provenance_scales_by_source_categories_not_call_test_product(tmp_path):
    # Generate independently controlled source-call cardinality and test count;
    # every call remains conservatively unknown without duplicating every name
    # through every test closure. No function is executed by collection.
    root = project(tmp_path, {
        'helper.py': '\n'.join('def function_' + str(i) + '(): return runtime_' + str(i) + '()' for i in range(400)) + '\n',
        'tests/test_many.py': 'import helper\n' + '\n'.join('def test_' + str(i) + '(): pass' for i in range(80)) + '\n',
    })
    data = collect(root, tmp_path)
    assert len(data['nodes']) == 80
    assert data['source_analysis']['helper.py']['unknown'] == ['unresolved runtime call influence']
    assert all(sum('helper.py: unresolved runtime call influence' == issue for issue in node['unknown']) == 1 for node in data['nodes'])
    assert len(planner.canonical(data).encode()) < 200_000
    assert all(node['unknown'] for node in data['nodes'])
    put(root, {'helper.py': (root / 'helper.py').read_text() + 'def extra(): return another_runtime()\n'})
    newer = collect(root, tmp_path, 'more-calls')
    assert len(planner.canonical(newer).encode()) - len(planner.canonical(data).encode()) < 1000


@pytest.mark.parametrize('helper_source', [
    'from os import *\ndef measure(): return statvfs(".")\n',
    'import os\nquery=getattr(os,"statvfs")\ndef measure(): return query(".")\n',
    'import os as system\nreflect=getattr\nquery=reflect(system,"statvfs")\ndef measure(): return query(".")\n',
    'import importlib\ndef measure(): return importlib.import_module("hidden").measure()\n',
    'from importlib import import_module as load\ndef measure(): return load("hidden").measure()\n',
    'import importlib,os\ndef measure(): return importlib.import_module(os.environ["TARGET_MODULE"]).measure()\n',
    'import os\ndef measure(): return vars(os)["statvfs"](".")\n',
    'def measure(): return globals()["query"](".")\n',
    'import os,operator\ndef measure(): return operator.attrgetter("statvfs")(os)(".")\n',
    'import os\ndef measure(): return __builtins__["getattr"](os,"statvfs")(".")\n',
])
def test_unresolved_resource_indirection_overrides_broad_host_family(tmp_path, helper_source):
    root = project(tmp_path, {'helper.py': helper_source,
                             'hidden.py': 'import os\ndef measure(): return os.statvfs(".")\n',
                             'tests/test_one.py': 'from helper import measure\ndef test_statistics(): assert measure()\n'})
    data = collect(root, tmp_path)
    node = data['nodes'][0]
    assert node['profile'] == 'unqualified'
    assert 'helper.py' in node['resource_unknown_sources']
    assert data['source_analysis']['helper.py']['resource_unknown']
    result = planner.run_plan(root, data, planner.make_plan(root, data, 'absent'), tmp_path / 'run')
    assert result['blocked'] and not result['jobs']


@pytest.mark.parametrize('route', ['direct', 'fixture', 'plugin'])
def test_reflective_unknown_propagates_through_all_execution_seeds(tmp_path, route):
    files = {'helper.py': 'import os\nquery=getattr(os,"statvfs")\ndef measure(): return query(".")\n'}
    if route == 'direct':
        files['tests/test_one.py'] = 'import os\ndef test_statistics(): getattr(os,"statvfs")(".")\n'
    elif route == 'fixture':
        files.update({'provider.py': 'from helper import measure\ndef sample(): return measure()\n',
                      'conftest.py': 'import pytest\nimport provider\n@pytest.fixture\ndef sample(): return provider.sample()\n',
                      'tests/test_one.py': 'def test_statistics(sample): assert sample\n'})
    else:
        files.update({'behavior.py': 'from helper import measure\ndef pytest_runtest_setup(item): measure()\n',
                      'conftest.py': 'pytest_plugins=["behavior"]\n',
                      'tests/test_one.py': 'def test_statistics(): pass\n'})
    root = project(tmp_path, files)
    data = collect(root, tmp_path)
    assert data['nodes'][0]['profile'] == 'unqualified'
    assert data['nodes'][0]['resource_unknown_sources']
    result = planner.run_plan(root, data, planner.make_plan(root, data, 'absent'), tmp_path / 'run')
    assert result['blocked'] and not result['jobs']
