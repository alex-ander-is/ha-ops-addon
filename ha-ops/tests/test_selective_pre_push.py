"""Portable selective gate contracts; no live system or full-suite execution."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'dev'))
import selective_pre_push as gate


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-b', 'main')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    (tmp_path / 'initial.md').write_text('initial')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-m', 'Initial')
    return tmp_path


def commit(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Change')
    return git(root, 'rev-parse', 'HEAD')


def records(root, base):
    return 'refs/heads/main {} refs/heads/main {}\n'.format(git(root, 'rev-parse', 'HEAD'), base)


def data(nodes):
    return {'nodes': [{'id': source + '::' + name, 'source': source, 'unknown': []} for source, name in nodes],
            'inputs': {}, 'tools': {}, 'rules': {'profiles': {'host': {'cap': 10}}}}


def test_union_every_commit_revert_dirty_layers_rename_delete_untracked(repo):
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'first.py', 'one')
    commit(repo, 'first.py', 'two')
    commit(repo, 'first.py', 'one')
    git(repo, 'mv', 'first.py', 'second.py')
    git(repo, 'commit', '-m', 'Rename')
    (repo / 'initial.md').unlink()
    (repo / 'second.py').write_text('index')
    git(repo, 'add', 'second.py')
    (repo / 'second.py').write_text('one')
    (repo / 'new.py').write_text('new')
    changed, _ = gate.change_basis(repo, records(repo, base))
    assert set(changed) == {'first.py', 'second.py', 'initial.md', 'new.py'}


def test_multiref_annotated_ancestor_tag(repo):
    base = git(repo, 'rev-parse', 'HEAD')
    tagged = commit(repo, 'tagged.py', 'tag')
    git(repo, 'tag', '-a', 'v1', '-m', 'Tag')
    head = commit(repo, 'later.py', 'later')
    git(repo, 'branch', 'other', head)
    raw = records(repo, base) + 'refs/tags/v1 {} refs/tags/v1 {}\n'.format(git(repo, 'rev-parse', 'v1'), '0' * 40)
    raw += 'refs/heads/other {} refs/heads/other {}\n'.format(head, base)
    changed, refs = gate.change_basis(repo, raw)
    assert {'tagged.py', 'later.py'} <= set(changed)
    assert len(refs) == 3


def test_unknown_base_ref_and_malformed_block_even_full(repo):
    for raw in ['bad', 'refs/heads/main {} refs/heads/main {}\n'.format('f' * 40, '0' * 40)]:
        with pytest.raises(gate.shared.Refused):
            gate.change_basis(repo, raw, full=True)
    with pytest.raises(gate.shared.Refused, match='New branch'):
        gate.change_basis(repo, records(repo, '0' * 40))


def test_feature_before_mixed_defaults_and_ui_excludes_native(repo):
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'ha-ops/app/reset_git.py', '# reset')
    inventory = data([('ha-ops/tests/test_server.py', name) for name in
                      ['test_reset_recovery', 'test_log_scroll', 'test_command_dispatch', 'test_other']] +
                     [('ha-ops/tests/test_native_disk.py', 'test_disk')])
    plan = gate.make_plan(repo, inventory, records(repo, base))
    chosen = {row['id'].split('::')[-1] for row in plan['decisions'] if row['selected']}
    assert chosen == {'test_reset_recovery', 'test_command_dispatch'}
    assert plan['errors'] == []
    assert gate.category({'source': 'ha-ops/tests/test_server.py', 'id': 'x::test_reset_log_window'}) == 'reset'
    assert gate.areas_for_path('ha-ops/frontend/src/migration-review.js', inventory['nodes'])[0] == {'migration', 'ui-common'}


def test_unknown_blocks_no_full_fallback_explicit_full_exact(repo):
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'unknown.config', 'value')
    inventory = data([('ha-ops/tests/test_server.py', 'test_reset'), ('ha-ops/tests/test_native_disk.py', 'test_disk')])
    plan = gate.make_plan(repo, inventory, records(repo, base))
    assert plan['errors'] and not any(row['selected'] for row in plan['decisions'])
    full = gate.make_plan(repo, inventory, records(repo, base), full=True)
    assert not full['errors'] and all(row['selected'] for row in full['decisions'])
    assert gate.areas_for_path('ha-ops/app/server.py', inventory['nodes']) == ({'reset', 'native-disk'}, None)


def machine(ids, workers=2):
    return {'errors': [], 'exitstatus': 0, 'deselected': [], 'schema': 1, 'worker_collections': [ids] * workers,
            'reports': [{'id': node, 'when': phase, 'outcome': 'passed', 'xfail': False}
                        for node in ids for phase in ('setup', 'call', 'teardown')]}


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'unexpected', 'failure', 'xfail', 'collection', 'wrong_collection'])
def test_exact_machine_accounting_rejects_all_bad_outcomes(change):
    record = machine(['node'])
    if change == 'missing': record['reports'].pop()
    if change == 'duplicate': record['reports'].append(record['reports'][0])
    if change == 'unexpected': record['reports'][0]['id'] = 'other'
    if change == 'failure': record['reports'][-1]['outcome'] = 'failed'
    if change == 'xfail': record['reports'][1]['xfail'] = True
    if change == 'collection': record['errors'] = ['failure']
    if change == 'wrong_collection': record['worker_collections'][0] = []
    with pytest.raises(gate.shared.Refused):
        gate.verify_outcomes(record, ['node'], 2)


def test_ordinary_finishes_before_all_nested_timing_families(repo, monkeypatch, tmp_path_factory):
    inventory = data([('ha-ops/tests/test_server.py', 'test_command'), *
                      [('ha-ops/tests/' + name, 'test_case') for name in sorted(gate.ISOLATED)]])
    plan = {'errors': [], 'mode': 'full', 'decisions': [{'id': node['id'], 'selected': True} for node in inventory['nodes']]}
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *args: [])
    monkeypatch.setattr(gate, 'make_plan', lambda *args: plan)
    calls = []
    def launch(root, args, directory):
        ids = [line for arg in args if arg.startswith('@') for line in Path(arg[1:]).read_text().splitlines()]
        calls.append((ids, int(args[1])))
        return machine(ids, int(args[1])), {}
    result = gate.execute(repo, inventory, plan, '', tmp_path_factory.mktemp('phase-evidence'), launch=launch)
    assert result['status'] == 'passed'
    assert len(calls) == 2
    assert 'test_server.py' in calls[0][0][0]
    assert {Path(node.split('::')[0]).name for node in calls[1][0]} == gate.ISOLATED
    assert calls[1][1] <= 2


def test_real_controlled_inventory_and_protected_child(repo, tmp_path_factory):
    commit(repo, 'ha-ops/tests/test_small.py', 'def test_one():\n    assert True\n')
    evidence = tmp_path_factory.mktemp('selective-evidence')
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], evidence / 'collection')
    node = inventory['nodes'][0]['id']
    record, _ = gate.shared.guarded(repo, ['-n', '0', node], evidence / 'child')
    assert gate.verify_outcomes(record, [node], 0) == []


def test_documentation_versus_data_and_virtualization_boundaries():
    nodes = data([('ha-ops/tests/test_local_virtualization.py', 'test_wrapper')])['nodes']
    assert gate.areas_for_path('AGENTS.md', nodes) == (set(), None)
    assert gate.areas_for_path('fixtures/data.md', nodes)[1]
    assert gate.areas_for_path('dev/virtualization/ha_ops_dev.py', nodes)[0] == {'virtualization', 'runner'}


def test_pushed_non_head_branch_refuses_even_explicit_full(repo):
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'branch', 'other')
    commit(repo, 'later.py', 'changed')
    raw = 'refs/heads/other {} refs/heads/other {}\n'.format(base, base)
    for full in (False, True):
        with pytest.raises(gate.shared.Refused, match='not checked-out HEAD'):
            gate.change_basis(repo, raw, full)


def test_tag_only_full_cannot_test_different_head(repo):
    commit(repo, 'old.py', 'old')
    git(repo, 'tag', '-a', 'v1', '-m', 'Tag')
    commit(repo, 'new.py', 'new')
    raw = 'refs/tags/v1 {} refs/tags/v1 {}\n'.format(git(repo, 'rev-parse', 'v1'), '0' * 40)
    with pytest.raises(gate.shared.Refused, match='tag snapshot'):
        gate.change_basis(repo, raw, full=True)


def test_missing_native_category_and_deleted_test_survival(repo, monkeypatch):
    monkeypatch.setattr(gate, 'change_basis', lambda *a: (['ha-ops/app/native_disk.py'], []))
    inventory = data([('ha-ops/tests/test_server.py', 'test_command')])
    assert any('native-disk' in error for error in gate.make_plan(repo, inventory, '')['errors'])
    monkeypatch.setattr(gate, 'change_basis', lambda *a: (['ha-ops/tests/test_reset_removed.py'], []))
    inventory = data([('ha-ops/tests/test_reset_git.py', 'test_reset')])
    plan = gate.make_plan(repo, inventory, '')
    assert not plan['errors'] and plan['decisions'][0]['selected']


def test_resource_class_api_and_profile_refuse_host_without_blanket_native():
    def node(source, name='test_case', **extra):
        return {'source': source, 'id': source + '::' + name, **extra}
    assert gate.unsafe_host_node(node('ha-ops/tests/test_native_disk.py'))
    assert gate.unsafe_host_node(node('ha-ops/tests/test_other.py', calls=['os.statvfs']))
    assert gate.unsafe_host_node(node('ha-ops/tests/test_other.py', profile='exclusive-volume-ext4'))
    assert not gate.unsafe_host_node(node('ha-ops/tests/test_native_git.py', profile='unqualified'))
    assert gate.category(node('ha-ops/tests/test_preview_decisions.py')) == 'ui-common'
    assert gate.category(node('ha-ops/tests/test_client_contract.py', 'test_reset')) == 'reset'


def test_exclusive_case_blocks_before_any_dispatch(repo, monkeypatch, tmp_path):
    inventory = data([('ha-ops/tests/test_future.py', 'test_linux_real_allocations_with_capped_initial_capacity_warning')])
    plan = {'errors': [], 'mode': 'full', 'decisions': [{'id': node['id'], 'selected': True} for node in inventory['nodes']]}
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *a: [])
    monkeypatch.setattr(gate, 'make_plan', lambda *a: plan)
    def forbidden(*a):
        pytest.fail('Exclusive case entered host launch')
    with pytest.raises(gate.shared.Refused, match='Unreviewed exclusive isolated'):
        gate.execute(repo, inventory, plan, '', tmp_path, launch=forbidden)


@pytest.mark.parametrize('filename,companion', [
    ('test_server.py', 'test_client_contract.py'),
    ('test_client_contract.py', 'test_server.py'),
])
@pytest.mark.parametrize('operation', ['delete', 'partial-delete', 'rename'])
def test_changed_mixed_module_retains_declared_families(repo, filename, companion, operation):
    source = 'ha-ops/tests/' + filename
    commit(repo, source, 'def test_reset_old(): pass\ndef test_log_scroll_old(): pass\ndef test_command_old(): pass\ndef test_misc_old(): pass\n')
    base = git(repo, 'rev-parse', 'HEAD')
    if operation == 'delete':
        git(repo, 'rm', source)
    elif operation == 'rename':
        git(repo, 'mv', source, 'ha-ops/tests/test_renamed_mixed.py')
    else:
        (repo / source).write_text('def test_misc_old(): pass\n')
        git(repo, 'add', source)
    git(repo, 'commit', '-m', 'Change mixed test module')
    inventory = data([('ha-ops/tests/' + companion, name) for name in
                      ['test_reset_survives', 'test_log_scroll_survives', 'test_command_survives', 'test_misc_survives']])
    if operation == 'partial-delete':
        inventory['nodes'].extend(data([(source, 'test_misc_old')])['nodes'])
    # An unrelated Native family must remain excluded from this category union.
    inventory['nodes'].extend(data([('ha-ops/tests/test_native_git.py', 'test_git')])['nodes'])
    plan = gate.make_plan(repo, inventory, records(repo, base))
    assert not plan['errors']
    selected = [row for row in plan['decisions'] if row['selected']]
    assert {row['category'] for row in selected} == {'reset', 'logs', 'backend-common', 'backend-other'}
    assert all(source in row['reason'] for row in selected)
    assert source in plan['changed']
    if operation == 'rename':
        assert 'ha-ops/tests/test_renamed_mixed.py' in plan['changed']


@pytest.mark.parametrize('filename', ['test_server.py', 'test_client_contract.py'])
@pytest.mark.parametrize('operation', ['delete', 'partial-delete', 'rename'])
@pytest.mark.parametrize('missing', ['reset', 'logs', 'backend-common', 'backend-other'])
def test_changed_mixed_module_missing_family_blocks(repo, monkeypatch, filename, operation, missing):
    source = 'ha-ops/tests/' + filename
    # Partial deletion keeps only currently surviving families at the old path;
    # rename retains the old endpoint as well as the new collected namespace.
    changed = [source] + (['ha-ops/tests/test_renamed_mixed.py'] if operation == 'rename' else [])
    monkeypatch.setattr(gate, 'change_basis', lambda *a: (changed, []))
    companion = 'test_client_contract.py' if filename == 'test_server.py' else 'test_server.py'
    names = {'reset': 'test_reset', 'logs': 'test_log_scroll',
             'backend-common': 'test_command', 'backend-other': 'test_misc'}
    rows = [('ha-ops/tests/' + companion, name) for area, name in names.items() if area != missing]
    if operation == 'partial-delete':
        rows += [(source, name) for area, name in names.items() if area != missing]
    plan = gate.make_plan(repo, data(rows), '')
    assert 'Required category has no collected tests: ' + missing in plan['errors']


def test_deleted_single_family_native_keeps_narrow_membership(repo, monkeypatch):
    source = 'ha-ops/tests/test_native_git.py'
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([source], []))
    inventory = data([('ha-ops/tests/test_native_git.py', 'test_surviving_git'),
                      ('ha-ops/tests/test_native_disk.py', 'test_disk'),
                      ('ha-ops/tests/test_reset_git.py', 'test_reset')])
    plan = gate.make_plan(repo, inventory, '')
    assert not plan['errors']
    assert [row['category'] for row in plan['decisions'] if row['selected']] == ['native-git']


@pytest.mark.parametrize('relative', [
    'test_reset_fixture.json', 'test_native_disk.yaml', 'test_cases.txt',
    'test_reset_fixture.yml', 'test_reset_fixture.py.json', 'test_reset_fixture.pyi',
    'test_reset_fixture.PY', 'test_Reset_fixture.py', 'Test_reset_fixture.py',
    'test_reset_fixture.py.JSON', 'data/test_reset_fixture.py',
    'nested/test_native_disk.py', 'data/test_reset_fixture.json',
    'test_reset_fixture', './test_reset_fixture.py', 'data/../test_reset_fixture.py',
    'data//test_reset_fixture.py',
])
@pytest.mark.parametrize('deleted', [False, True])
def test_unknown_test_data_never_inherits_python_source_grammar(repo, monkeypatch, relative, deleted):
    source = 'ha-ops/tests/' + relative
    if not deleted and '..' not in Path(relative).parts and '//' not in relative:
        target = repo / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('fixture data')
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([source], []))
    inventory = data([('ha-ops/tests/test_server.py', 'test_reset'),
                      ('ha-ops/tests/test_native_disk.py', 'test_disk'),
                      ('ha-ops/tests/test_plain.py', 'test_case')])
    plan = gate.make_plan(repo, inventory, '')
    assert any('Unknown influence: ' + source in error for error in plan['errors'])
    assert not any(row['selected'] for row in plan['decisions'])


@pytest.mark.parametrize('source', ['ha-ops/tests/test_reset_future.py',
                                   'ha-ops/tests/nested/test_reset_future.py'])
def test_actual_collected_python_sources_keep_metadata_selection(repo, monkeypatch, source):
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([source], []))
    inventory = data([(source, 'test_reset'), ('ha-ops/tests/test_native_git.py', 'test_git')])
    plan = gate.make_plan(repo, inventory, '')
    assert not plan['errors']
    assert [row['id'] for row in plan['decisions'] if row['selected']] == [source + '::test_reset']


def test_declared_root_deleted_python_module_keeps_surviving_family(repo, monkeypatch):
    source = 'ha-ops/tests/test_reset_removed.py'
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([source], []))
    inventory = data([('ha-ops/tests/test_reset_other.py', 'test_reset'),
                      ('ha-ops/tests/test_native_git.py', 'test_git')])
    plan = gate.make_plan(repo, inventory, '')
    assert not plan['errors']
    assert [row['category'] for row in plan['decisions'] if row['selected']] == ['reset']


# Receipt contracts exercise the same scheduler/guard/plugin as the hook.
def test_canonical_receipt_mechanism_with_explicit_test_only_authority(repo, tmp_path_factory, monkeypatch):
    # This is a mechanism unit contract, not evidence of production eligibility.
    monkeypatch.setattr(gate.receipts, 'positive_qualification',
                        lambda *args: {'scope': 'test-only-literal-fixture'})
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'ha-ops/tests/test_small.py', 'def test_one():\n    assert True\n')
    evidence = tmp_path_factory.mktemp('receipt-real')
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], evidence / 'collection')
    raw = records(repo, base)
    plan = gate.make_plan(repo, inventory, raw)
    first = gate.execute(repo, inventory, plan, raw, evidence)
    assert first['categories']['backend-common']['status'] == 'executed'
    assert first['categories']['backend-common']['proof'] == 'complete success candidate'
    def forbidden(*args):
        pytest.fail('Unchanged category launched instead of reusing success')
    second = gate.execute(repo, inventory, plan, raw, evidence, launch=forbidden)
    assert second['categories']['backend-common']['status'] == 'reused'
    assert second['jobs'] == []


def receipt_binding(repo, monkeypatch):
    source = 'ha-ops/tests/test_reset_one.py'
    other = 'ha-ops/tests/test_logs_two.py'
    for path in (source, other, 'ha-ops/tests/helper.py', 'ha-ops/tests/conftest.py', 'config.json'):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# input')
    inventory = data([(source, 'test_reset'), (other, 'test_logs')])
    nodes = [inventory['nodes'][0]]
    monkeypatch.setattr(gate.receipts, 'positive_qualification', lambda *args: {'scope': 'test-only-binding-unit'})
    monkeypatch.setattr(gate.receipts, 'runtime', lambda root: {'env': os.environ.get('RECEIPT_CUSTOM_TEST', '')})
    profile = {'route': 'canonical-host', 'workers': [1]}
    def current():
        return gate.receipts.binding(repo, inventory, 'reset', nodes, profile, gate.category)
    return current, inventory, profile


@pytest.mark.parametrize('mutation', ['own', 'helper', 'conftest', 'config', 'mode', 'delete', 'new', 'env', 'profile', 'membership'])
def test_receipt_binding_invalidates_influential_inputs(repo, monkeypatch, mutation):
    current, inventory, profile = receipt_binding(repo, monkeypatch)
    before = current()
    paths = {'own': 'ha-ops/tests/test_reset_one.py', 'helper': 'ha-ops/tests/helper.py',
             'conftest': 'ha-ops/tests/conftest.py', 'config': 'config.json'}
    if mutation in paths:
        (repo / paths[mutation]).write_text('changed')
    elif mutation == 'mode':
        (repo / 'config.json').chmod(0o755)
    elif mutation == 'delete':
        (repo / 'config.json').unlink()
    elif mutation == 'new':
        (repo / 'new-data.json').write_text('new')
    elif mutation == 'env':
        monkeypatch.setenv('RECEIPT_CUSTOM_TEST', 'changed')
    elif mutation == 'profile':
        profile['workers'] = [2]
    else:
        inventory['nodes'][0]['id'] += '[new]'
    assert current() != before


def test_unrelated_collected_category_edit_preserves_receipt_binding(repo, monkeypatch):
    current, _, _ = receipt_binding(repo, monkeypatch)
    before = current()
    (repo / 'ha-ops/tests/test_logs_two.py').write_text('unrelated change')
    assert current() == before


def test_receipt_rejects_symlink_inputs(repo, monkeypatch):
    current, _, _ = receipt_binding(repo, monkeypatch)
    (repo / 'config.json').unlink()
    (repo / 'config.json').symlink_to(repo / 'initial.md')
    with pytest.raises(gate.shared.Refused, match='Nonregular'):
        current()


def valid_controller():
    return {'status': 'passed', 'reason': 'completed', 'raw_returncode': 0,
            'exit_code': 0, 'errors': [], 'omitted_error_count': 0,
            'process_group_cleanup': 'owned cleanup attempted', 'signal': None}


@pytest.mark.parametrize('key,value', [('raw_returncode', False), ('raw_returncode', None),
    ('raw_returncode', 1), ('exit_code', False), ('errors', None), ('omitted_error_count', False),
    ('omitted_error_count', 1), ('reason', 'timeout'), ('status', 'running'),
    ('process_group_cleanup', 'incomplete or unknown'), ('signal', 9)])
def test_strict_receipt_controller_rejects_invalid_finalization(key, value):
    manifest = valid_controller()
    manifest[key] = value
    with pytest.raises(gate.shared.Refused):
        gate.receipts.controller_success(manifest)
    manifest.pop(key)
    with pytest.raises(gate.shared.Refused):
        gate.receipts.controller_success(manifest)


@pytest.mark.parametrize('mutation', ['schema-bool', 'raw-bool', 'missing-errors', 'short-workers',
    'extra-workers', 'duplicate-collection', 'duplicate-phase', 'missing-phase', 'skip', 'xfail', 'xfail-missing'])
def test_strict_receipt_machine_rejects_partial_false_success(mutation):
    record = machine(['node'])
    if mutation == 'schema-bool': record['schema'] = True
    elif mutation == 'raw-bool': record['exitstatus'] = False
    elif mutation == 'missing-errors': record.pop('errors')
    elif mutation == 'short-workers': record['worker_collections'].pop()
    elif mutation == 'extra-workers': record['worker_collections'].append(['node'])
    elif mutation == 'duplicate-collection': record['worker_collections'][0].append('node')
    elif mutation == 'duplicate-phase': record['reports'].append(dict(record['reports'][0]))
    elif mutation == 'missing-phase': record['reports'].pop()
    elif mutation == 'skip': record['reports'][1]['outcome'] = 'skipped'
    elif mutation == 'xfail': record['reports'][1]['xfail'] = True
    else: record['reports'][1].pop('xfail')
    with pytest.raises(gate.shared.Refused):
        gate.receipts.machine_success(record, ['node'], 2)


@pytest.mark.parametrize('raw', ['{"schema":1,"schema":1}', '{"v":NaN}', '{bad'])
def test_receipt_json_rejects_ambiguous_evidence(raw):
    with pytest.raises(ValueError):
        gate.receipts.strict_json(raw)


def test_receipt_cache_is_bounded_shared_atomic_and_preserves_foreign_file(repo):
    store = gate.receipts.Store(repo)
    sentinel = store.path / 'foreign.txt'
    sentinel.write_text('keep')
    for number in range(5):
        key = str(number)
        store.publish({'reset': {'binding': {}, 'jobs': [key]}, 'logs': {'binding': {}, 'jobs': [key]}}, {key: {'n': number}})
        assert len(store.value['jobs']) == 1
    assert sentinel.read_text() == 'keep'
    assert len(gate.receipts.Store(repo).value['jobs']) == 1
    assert not list(store.path.glob('publish-*'))


@pytest.mark.parametrize('raw', ['[]', '{"schema":true}', '{"schema":1,"categories":{},"jobs":{}}', '{bad'])
def test_missing_malformed_receipt_is_safe_miss(repo, raw):
    store = gate.receipts.Store(repo)
    store.index.write_text(raw)
    assert gate.receipts.Store(repo).lookup('reset', {})[0] is False


def test_failure_after_prior_job_never_publishes_candidate(repo, monkeypatch, tmp_path_factory):
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'ha-ops/tests/test_reset_first.py', 'def test_reset():\n    assert True\n')
    commit(repo, 'ha-ops/tests/test_logs_second.py', 'def test_logs():\n    assert True\n')
    evidence = tmp_path_factory.mktemp('receipt-failed-sibling')
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], evidence / 'collection')
    raw = records(repo, base)
    plan = gate.make_plan(repo, inventory, raw)
    launched = []
    def launch(root, args, directory):
        launched.append(directory)
        if len(launched) == 2:
            raise gate.shared.Refused('Later sibling failed')
        return gate.shared.guarded(root, args, directory)
    with pytest.raises(gate.shared.Refused, match='Later sibling'):
        gate.execute(repo, inventory, plan, raw, evidence, launch=launch)
    assert len(launched) == 2
    assert gate.receipts.Store(repo).value['categories'] == {}



def retained_job(repo, expected=None, workers=1):
    expected = expected or ['ha-ops/tests/test_reset_one.py::test_reset']
    selector = str(repo / 'selector.txt')
    directory = repo / 'evidence'
    argv = [sys.executable, str(gate.shared.HERE / 'test_runner.py'), '--deadline', '1800',
            '--artifacts', str(directory / 'runner'), '--', '--rootdir', str(repo),
            '-p', 'test_plan_plugin', '-p', 'no:cacheprovider', '-n', str(workers),
            '--dist', 'worksteal', '@' + selector]
    machine_raw = gate.shared.canonical(machine(expected, workers))
    controller_raw = gate.shared.canonical(valid_controller())
    return {'machine': machine_raw, 'controller': controller_raw,
            'machine_hash': gate.shared.digest(machine_raw.encode()),
            'controller_hash': gate.shared.digest(controller_raw.encode()),
            'outer_returncode': 0, 'expected': expected, 'workers': workers,
            'argv': argv, 'root': str(repo), 'runner_artifacts': str(directory / 'runner'),
            'selectors': [{'path': selector, 'ids': expected}]}


@pytest.mark.parametrize('mutation', ['machine-tamper', 'controller-tamper', 'outer-false',
    'outer-missing', 'argv', 'selector', 'workers-bool', 'worker-profile', 'foreign-root', 'missing-job'])
def test_receipt_lookup_revalidates_original_evidence_and_route(repo, monkeypatch, mutation):
    current, _, profile = receipt_binding(repo, monkeypatch)
    # Match the full canonical profile contract used by lookup.
    bound = current()
    job = retained_job(repo)
    gate.receipts.validate_job(job)
    if mutation == 'machine-tamper': job['machine'] += ' '
    elif mutation == 'controller-tamper': job['controller'] += ' '
    elif mutation == 'outer-false': job['outer_returncode'] = False
    elif mutation == 'outer-missing': job.pop('outer_returncode')
    elif mutation == 'argv': job['argv'][1] = 'other-runner.py'
    elif mutation == 'selector': job['selectors'][0]['ids'] = []
    elif mutation == 'workers-bool': job['workers'] = True
    elif mutation == 'worker-profile': bound['profile']['workers'] = [2]
    elif mutation == 'foreign-root':
        job['root'] = '/foreign'
        job['argv'][8] = '/foreign'
    key = gate.shared.digest(gate.shared.canonical(job).encode())
    store = gate.receipts.Store(repo)
    store.publish({'reset': {'binding': bound, 'jobs': [key]}}, {} if mutation == 'missing-job' else {key: job})
    assert store.lookup('reset', bound)[0] is False


def test_matching_receipt_never_bypasses_unknown_selection_or_unsafe_route(repo, monkeypatch, tmp_path_factory):
    inventory = data([('ha-ops/tests/test_reset_one.py', 'test_reset')])
    plan = {'errors': ['Unknown influence: new.config'], 'mode': 'full',
            'decisions': [{'id': inventory['nodes'][0]['id'], 'selected': True}]}
    def forbidden(*args):
        pytest.fail('Unsafe/unknown plan reached receipt lookup or launch')
    monkeypatch.setattr(gate.receipts, 'Store', forbidden)
    with pytest.raises(gate.shared.Refused, match='Unknown influence'):
        gate.execute(repo, inventory, plan, '', tmp_path_factory.mktemp('unknown-receipt'))
    plan['errors'] = []
    inventory['nodes'][0]['profile'] = 'exclusive-volume-ext4'
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *a: [])
    monkeypatch.setattr(gate, 'make_plan', lambda *a: plan)
    with pytest.raises(gate.shared.Refused, match='Unreviewed exclusive isolated'):
        gate.execute(repo, inventory, plan, '', tmp_path_factory.mktemp('unsafe-receipt'))


def test_midrun_input_mutation_preserves_prior_success(repo, tmp_path_factory, monkeypatch):
    # Explicit unit authority only; production eligibility remains fail-closed.
    monkeypatch.setattr(gate.receipts, 'positive_qualification',
                        lambda *args: {'scope': 'test-only-literal-fixture'})
    base = git(repo, 'rev-parse', 'HEAD')
    source = repo / 'ha-ops/tests/test_reset_one.py'
    commit(repo, str(source.relative_to(repo)), 'def test_reset():\n    assert True\n')
    evidence = tmp_path_factory.mktemp('receipt-mutating-input')
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], evidence / 'collection')
    raw = records(repo, base)
    plan = gate.make_plan(repo, inventory, raw)
    gate.execute(repo, inventory, plan, raw, evidence)
    store = gate.receipts.Store(repo)
    previous = store.index.read_bytes()
    source.write_text(source.read_text() + '# changed before next run\n')
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], evidence / 'collection-two')
    plan = gate.make_plan(repo, inventory, raw)
    def mutate(root, args, directory):
        result = gate.shared.guarded(root, args, directory)
        source.write_text(source.read_text() + '# changed during final job\n')
        return result
    # Distinct artifact root, same original private receipt cache.
    second = evidence / 'second'
    second.mkdir()
    with pytest.raises(gate.shared.Refused, match='changed'):
        gate.execute(repo, inventory, plan, raw, second, launch=mutate)
    assert store.index.read_bytes() == previous


def test_symlink_cache_index_cannot_read_or_modify_foreign_file(repo):
    store = gate.receipts.Store(repo)
    sentinel = repo / 'foreign.json'
    sentinel.write_text('{"schema":1,"categories":{},"jobs":{}}')
    store.index.symlink_to(sentinel)
    assert gate.receipts.Store(repo).lookup('reset', {})[0] is False
    assert sentinel.read_text() == '{"schema":1,"categories":{},"jobs":{}}'


def test_runtime_digest_changes_for_environment_and_tool_without_raw_secret(repo, monkeypatch):
    before = gate.receipts.runtime(repo)
    monkeypatch.setenv('RECEIPT_CUSTOM_TEST', 'fixture-secret-no-output')
    after = gate.receipts.runtime(repo)
    assert before['environment'] != after['environment']
    assert 'fixture-secret-no-output' not in gate.shared.canonical(after)
    tool = repo / 'fake-node'
    tool.write_text('first build')
    which = gate.receipts.shutil.which
    monkeypatch.setattr(gate.receipts.shutil, 'which', lambda name: str(tool) if name == 'node' else which(name))
    first = gate.receipts.runtime(repo)
    tool.write_text('second build')
    assert first != gate.receipts.runtime(repo)



@pytest.mark.parametrize('influence', ['external', 'cross-category', 'ignored'])
def test_unresolved_runtime_input_never_mints_or_reuses_false_green(repo, tmp_path_factory, influence):
    evidence = tmp_path_factory.mktemp('unresolved-runtime-input')
    if influence == 'ignored':
        commit(repo, '.gitignore', 'ignored-data/\n')
    base = git(repo, 'rev-parse', 'HEAD')
    if influence == 'cross-category':
        target = repo / 'ha-ops/tests/test_logs_data.py'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# value: valid\ndef test_logs():\n    assert True\n')
        expression = 'Path(__file__).with_name("test_logs_data.py").read_text()'
        assertion = '"value: valid" in ' + expression
    else:
        target = (repo / 'ignored-data/value.txt') if influence == 'ignored' else evidence / 'external.txt'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('good')
        assertion = 'Path(' + repr(str(target)) + ').read_text() == "good"'
    commit(repo, 'ha-ops/tests/test_reset_reader.py',
           'from pathlib import Path\ndef test_reset():\n    assert ' + assertion + '\n')
    raw = records(repo, base)
    first_dir = evidence / 'first'
    first_dir.mkdir()
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], first_dir / 'collection')
    reset = [n for n in inventory['nodes'] if gate.category(n) == 'reset']
    assert reset and all(node['unknown'] for node in reset)
    plan = gate.make_plan(repo, inventory, raw)
    first = gate.execute(repo, inventory, plan, raw, first_dir)
    assert first['categories']['reset']['status'] == 'executed'
    assert 'Unresolved runtime dependency' in first['categories']['reset']['reason']
    assert 'reset' not in gate.receipts.Store(repo).value['categories']
    target.write_text('# value: invalid\ndef test_logs():\n    assert True\n' if influence == 'cross-category' else 'bad')
    second_dir = evidence / 'second'
    second_dir.mkdir()
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], second_dir / 'collection')
    plan = gate.make_plan(repo, inventory, raw)
    # A fresh canonical execution must expose the now failing assertion instead
    # of returning the prior green result. No invented success/evidence injection.
    with pytest.raises(gate.shared.Refused, match='Protected invocation failed'):
        gate.execute(repo, inventory, plan, raw, second_dir)
    assert 'reset' not in gate.receipts.Store(repo).value['categories']


@pytest.mark.parametrize('unknown', [None, False, {}, ['unresolved runtime call influence'],
                                     ['unresolved import: fixture_provider']])
def test_receipt_admission_requires_closed_runtime_influence(repo, monkeypatch, unknown):
    current, inventory, _ = receipt_binding(repo, monkeypatch)
    inventory['nodes'][0]['unknown'] = unknown
    with pytest.raises(gate.shared.Refused, match='Unresolved runtime dependency'):
        current()


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'raced-fifo'])
@pytest.mark.parametrize('through_store', [False, True])
def test_nonregular_receipt_evidence_rejected_in_bounded_owned_child(repo, kind, through_store):
    store = gate.receipts.Store(repo)
    path = store.index
    if kind == 'fifo':
        os.mkfifo(path, 0o600)
    elif kind == 'directory':
        path.mkdir()
    else:
        path.write_text('{}')
    program = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import test_receipts as receipts
if sys.argv[5] == 'raced-fifo':
    import os
    original_open = receipts.os.open
    replaced = False
    def race(path, flags, *args, **kwargs):
        global replaced
        if str(path) == sys.argv[3] and not replaced:
            replaced = True
            pathlib.Path(path).unlink()
            os.mkfifo(path, 0o600)
        return original_open(path, flags, *args, **kwargs)
    receipts.os.open = race
try:
    if sys.argv[4] == 'store':
        assert receipts.Store(pathlib.Path(sys.argv[2])).lookup('reset', {})[0] is False
    else:
        receipts.regular_bytes(pathlib.Path(sys.argv[3]))
        raise AssertionError('Nonregular evidence admitted')
except receipts.shared.Refused:
    pass
"""
    completed = subprocess.run([sys.executable, '-c', program, str(ROOT / 'dev'), str(repo), str(path),
                                'store' if through_store else 'direct', kind],
                               capture_output=True, timeout=5)
    assert completed.returncode == 0, completed.stderr.decode()



@pytest.mark.parametrize('mutation', ['module-bytes', 'package-bytes', 'external-config'])
def test_empty_lexical_unknown_never_authorizes_external_import_reuse(repo, tmp_path_factory, monkeypatch, mutation):
    evidence = tmp_path_factory.mktemp('external-import-authority')
    external = evidence / 'external'
    external.mkdir()
    if mutation == 'package-bytes':
        module = external / 'colorsys' / '__init__.py'
        module.parent.mkdir()
    else:
        module = external / 'colorsys.py'
    configuration = evidence / 'external-config.txt'
    configuration.write_text('good')
    if mutation == 'external-config':
        module.write_text('from pathlib import Path\nFLAG = Path(' + repr(str(configuration)) + ').read_text()\n')
    else:
        module.write_text('FLAG = "good"\n')
    monkeypatch.setenv('PYTHONPATH', str(external))
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'ha-ops/tests/test_reset_import.py',
           'import colorsys\ndef test_reset_import():\n    assert colorsys.FLAG == "good"\n')
    raw = records(repo, base)
    first_dir = evidence / 'first'
    first_dir.mkdir()
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], first_dir / 'collection')
    # These are authoritative unmodified collected facts: the original bug
    # specifically bypassed lexical uncertainty without any Call in this test.
    assert inventory['nodes'][0]['unknown'] == []
    plan = gate.make_plan(repo, inventory, raw)
    first = gate.execute(repo, inventory, plan, raw, first_dir)
    assert first['categories']['reset']['status'] == 'executed'
    assert 'No reviewed execution/input qualification' in first['categories']['reset']['reason']
    assert gate.receipts.Store(repo).value['categories'] == {}
    if mutation == 'external-config':
        configuration.write_text('bad')
    else:
        module.write_text('FLAG = "bad"\n')
    second_dir = evidence / 'second'
    second_dir.mkdir()
    with pytest.raises(gate.shared.Refused, match='Protected invocation failed'):
        gate.execute(repo, inventory, plan, raw, second_dir)
    assert gate.receipts.Store(repo).value['categories'] == {}


def test_closed_literal_source_requires_positive_qualification(repo, tmp_path_factory):
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'ha-ops/tests/test_reset_literal.py', 'def test_reset():\n    assert True\n')
    raw = records(repo, base)
    evidence = tmp_path_factory.mktemp('literal-without-authority')
    inventory, _ = gate.shared.inventory(repo, ['ha-ops/tests'], evidence / 'collection')
    assert inventory['nodes'][0]['unknown'] == []
    plan = gate.make_plan(repo, inventory, raw)
    for number in range(2):
        run = evidence / ('run-' + str(number))
        run.mkdir()
        result = gate.execute(repo, inventory, plan, raw, run)
        assert result['categories']['reset']['status'] == 'executed'
        assert 'No reviewed execution/input qualification' in result['categories']['reset']['reason']
        assert result['jobs']
    assert gate.receipts.Store(repo).value['categories'] == {}



def linux_outer(command):
    linux = gate.qualified_linux
    return {'status': 'passed', 'command_exit_code': 0, 'failure': None, 'primary_error': None,
            'cleanup_errors': [], 'capture_errors': [],
            'receipt_profile': {'image': linux.IMAGE, 'readonly': True, 'network': 'none',
                'tmpfs': {'/tmp': 'exec'}, 'privileged': False, 'devices': [], 'command': command,
                'mounts': [{'destination': '/workspace/input', 'rw': False},
                           {'destination': '/workspace/output', 'rw': True}]},
            'containers': [{'wait_exit_code': 0, 'docker_wait_returncode': 0, 'supervisor_exit_code': 0,
                'watchdog_reason': 'completed', 'wait_failure': None,
                'state': {'OOMKilled': False, 'ExitCode': 0, 'Status': 'exited'}}]}


@pytest.mark.parametrize('mutation', ['image', 'readonly', 'network', 'tmpfs', 'privileged', 'devices',
    'extra-mount', 'mount-type', 'command', 'outer-bool', 'cleanup', 'capture', 'oom', 'wait-bool',
    'missing-inner-state', 'signal', 'extra-container'])
def test_qualified_linux_requires_original_outer_isolation_and_exit_evidence(mutation):
    command = ['python3', 'dev/test_receipt_linux.py', '--child', '[]']
    value = linux_outer(command)
    gate.qualified_linux.validate_outer(value, command)
    security = value['receipt_profile']
    if mutation == 'image': security['image'] = 'sha256:other'
    elif mutation == 'readonly': security['readonly'] = False
    elif mutation == 'network': security['network'] = 'host'
    elif mutation == 'tmpfs': security['tmpfs'] = {}
    elif mutation == 'privileged': security['privileged'] = True
    elif mutation == 'devices': security['devices'] = ['/dev/sda']
    elif mutation == 'extra-mount': security['mounts'].append({'destination': '/host', 'rw': True})
    elif mutation == 'mount-type': security['mounts'][0]['rw'] = 0
    elif mutation == 'command': security['command'] = ['arbitrary']
    elif mutation == 'outer-bool': value['command_exit_code'] = False
    elif mutation == 'cleanup': value['cleanup_errors'] = ['unproven']
    elif mutation == 'capture': value['capture_errors'] = ['unproven']
    elif mutation == 'oom': value['containers'][0]['state']['OOMKilled'] = True
    elif mutation == 'wait-bool': value['containers'][0]['wait_exit_code'] = False
    elif mutation == 'missing-inner-state': value['containers'][0].pop('state')
    elif mutation == 'signal': value['containers'][0]['watchdog_reason'] = 'interrupted'
    else: value['containers'].append(value['containers'][0])
    with pytest.raises(gate.shared.Refused):
        gate.qualified_linux.validate_outer(value, command)


def test_explicit_category_verification_selects_complete_generated_membership_and_excludes_hook_modes():
    inventory = data([('ha-ops/tests/test_server.py', 'test_reset_a'),
                      ('ha-ops/tests/test_client_contract.py', 'test_reset_b'),
                      ('ha-ops/tests/test_server.py', 'test_log_scroll')])
    plan = gate.verification_plan(inventory, 'reset')
    assert plan['mode'] == 'category-verification'
    assert plan['category_counts'] == {'reset': 2}
    assert {row['id'] for row in plan['decisions'] if row['selected']} == {n['id'] for n in inventory['nodes'][:2]}
    with pytest.raises(SystemExit):
        gate.main(['--verify-category', 'reset', '--records', 'hook-records'])
    with pytest.raises(SystemExit):
        gate.main(['--verify-category', 'reset', '--full'])


@pytest.mark.parametrize('mutation', ['source', 'new-app', 'conftest', 'membership', 'config', 'nested-config'])
def test_reviewed_linux_contract_stales_on_changed_source_or_execution_boundary(repo, monkeypatch, mutation):
    linux = gate.qualified_linux
    monkeypatch.setattr(linux, 'cached_runtime', lambda: {'KernelVersion': 'unit-only-fixture'})
    app = repo / 'ha-ops/app/fixture.py'
    source = repo / 'ha-ops/tests/test_server.py'
    for target in (app, source):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# reviewed source')
    policy = {'schema': 1, 'image': linux.IMAGE, 'membership_count': 1,
              'test_sources': ['ha-ops/tests/test_server.py'],
              'sources': {str(p.relative_to(repo)): gate.shared.digest(p.read_bytes()) for p in (app, source)}}
    policy_dir = repo / 'policy'
    policy_dir.mkdir()
    gate.shared.write_json(policy_dir / 'test_receipt_reset_policy.json', policy)
    monkeypatch.setattr(linux.shared, 'HERE', policy_dir)
    inventory = data([('ha-ops/tests/test_server.py', 'test_reset')])
    nodes = inventory['nodes']
    assert linux.contract(repo, inventory, 'reset', nodes)['kind'] == 'reviewed-reset-linux-v1'
    qualified = linux.contract(repo, inventory, 'reset', nodes)
    env = dict(linux.ENV)
    monkeypatch.setitem(linux.ENV, 'TZ', 'unit-changed-child-environment')
    assert linux.contract(repo, inventory, 'reset', nodes) != qualified
    monkeypatch.setattr(linux, 'ENV', env)
    if mutation == 'source': source.write_text('# modified')
    elif mutation == 'new-app': (app.parent / 'new_import.py').write_text('external = True')
    elif mutation == 'conftest': inventory['inputs']['conftest.py'] = 'new'
    elif mutation == 'membership': nodes.append(dict(nodes[0]))
    elif mutation == 'nested-config': inventory['inputs']['ha-ops/tests/pytest.ini'] = 'new config'
    else: (repo / 'pytest.ini').write_text('[pytest]')
    with pytest.raises(gate.shared.Refused):
        linux.contract(repo, inventory, 'reset', nodes)



def test_qualified_runtime_binds_actual_kernel_and_cached_image_identity(monkeypatch):
    linux = gate.qualified_linux
    import types
    state = {'KernelVersion': 'kernel-one', 'ServerVersion': 'engine', 'Architecture': 'aarch64',
             'OSType': 'linux', 'Name': 'ha-ops-dev'}
    image = {'Id': linux.IMAGE, 'Os': 'linux', 'Architecture': 'arm64'}
    fake = types.SimpleNamespace(Refused=RuntimeError, endpoint_info=lambda: dict(state),
        docker=lambda *args: types.SimpleNamespace(stdout=gate.shared.canonical([image]).encode()))
    monkeypatch.setattr(linux, 'wrapper', lambda: fake)
    first = linux.cached_runtime()
    state['KernelVersion'] = 'kernel-two'
    assert linux.cached_runtime() != first
    image['Id'] = 'sha256:other'
    with pytest.raises(gate.shared.Refused):
        linux.cached_runtime()



def test_qualified_source_stage_is_owned_inside_wrapper_repository(repo):
    stage = gate.qualified_linux.make_stage(repo)
    try:
        assert repo in stage.parents
        assert stage.parent == repo / 'ha-ops/tests/browser/artifacts'
        assert stage.name.startswith('receipt-qualified-source-')
        assert not any(parent.is_symlink() for parent in (stage, *stage.parents))
    finally:
        stage.rmdir()



def test_clean_git_template_preserves_normal_owned_info_exclude(tmp_path):
    env = gate.qualified_linux.prepare_child_environment(tmp_path)
    target = tmp_path / 'new-fixture-repository'
    subprocess.run(['git', 'init', str(target)], env=env, check=True, capture_output=True)
    exclude = target / '.git/info/exclude'
    assert exclude.is_file()
    exclude.write_text('fixture-only-ignore\n')
    assert exclude.read_text() == 'fixture-only-ignore\n'


def test_cached_host_job_cannot_qualify_linux_route(repo, monkeypatch):
    current, _, _ = receipt_binding(repo, monkeypatch)
    bound = current()
    bound['profile']['route'] = 'qualified-reset-linux-v1'
    job = retained_job(repo)
    key = gate.shared.digest(gate.shared.canonical(job).encode())
    store = gate.receipts.Store(repo)
    store.publish({'reset': {'binding': bound, 'jobs': [key]}}, {key: job})
    assert store.lookup('reset', bound)[0] is False



def test_fixed_linux_runtime_ignores_unused_host_terminal_env_and_git_config(repo, monkeypatch):
    before = gate.receipts.runtime(repo, qualified=True)
    monkeypatch.setenv('TERM_SESSION_ID', 'different-unused-host-session')
    monkeypatch.setenv('ARBITRARY_HOST_CONTROLLER_VALUE', 'different')
    git(repo, 'config', 'user.name', 'Changed unused host config')
    after = gate.receipts.runtime(repo, qualified=True)
    assert before == after
    assert 'environment' not in after and 'git_config' not in after
    host = gate.receipts.runtime(repo)
    monkeypatch.setenv('TERM_SESSION_ID', 'another-host-session')
    assert gate.receipts.runtime(repo)['environment'] != host['environment']
    tool = repo / 'qualified-controller-docker'
    tool.write_text('controller-build-one')
    which = gate.receipts.shutil.which
    monkeypatch.setattr(gate.receipts.shutil, 'which', lambda name: str(tool) if name == 'docker' else which(name))
    first_tool = gate.receipts.runtime(repo, qualified=True)
    tool.write_text('controller-build-two')
    assert gate.receipts.runtime(repo, qualified=True) != first_tool


@pytest.mark.parametrize('kind', ['policy-fifo', 'source-fifo', 'source-raced-fifo', 'duplicate-json', 'schema-bool'])
def test_qualified_policy_and_sources_use_strict_bounded_owned_child_reads(repo, kind):
    policy_dir = repo / 'policy-owned'
    policy_dir.mkdir()
    app = repo / 'ha-ops/app/fixture.py'
    source = repo / 'ha-ops/tests/test_server.py'
    for target in (app, source):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# reviewed')
    policy = {'schema': 1, 'image': gate.qualified_linux.IMAGE, 'membership_count': 1,
              'test_sources': ['ha-ops/tests/test_server.py'],
              'sources': {str(p.relative_to(repo)): gate.shared.digest(p.read_bytes()) for p in (app, source)}}
    target = policy_dir / 'test_receipt_reset_policy.json'
    gate.shared.write_json(target, policy)
    if kind == 'policy-fifo':
        target.unlink(); os.mkfifo(target, 0o600)
    elif kind == 'source-fifo':
        app.unlink(); os.mkfifo(app, 0o600)
    elif kind == 'source-raced-fifo':
        pass
    elif kind == 'duplicate-json':
        target.write_text('{"schema":1,"schema":1}')
    else:
        policy['schema'] = True; gate.shared.write_json(target, policy)
    program = """
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import test_receipt_linux as linux
linux.shared.HERE=Path(sys.argv[2])
linux.cached_runtime=lambda: {'fixture':'unit-only'}
if sys.argv[4]=='source-raced-fifo':
    import os
    original=Path.is_file
    selected=Path(sys.argv[3])/'ha-ops/app/fixture.py'
    replaced=False
    def raced(path):
        global replaced
        if path==selected and not replaced:
            replaced=True
            path.unlink()
            os.mkfifo(path,0o600)
            return True
        return original(path)
    Path.is_file=raced
try:
    linux.contract(Path(sys.argv[3]), {'inputs':{}}, 'reset', [{'source':'ha-ops/tests/test_server.py'}])
except (ValueError,OSError):
    pass
else:
    raise AssertionError('Unbounded/malformed policy/source admitted')
"""
    result = subprocess.run([sys.executable, '-c', program, str(ROOT / 'dev'), str(policy_dir), str(repo), kind],
                            timeout=3, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


# Frozen diagnostic *paths*, not test IDs: collection still supplies membership.
REVIEWED_DIAGNOSTIC_PATHS = [
    '.github/workflows/local-virtualization.yml',
    '.github/workflows/native-topology.yml',
    '.gitignore',
    'TODO.md',
    'UI_COMPONENT_AUDIT.md',
    'ha-ops/Dockerfile',
    'ha-ops/app/app_context.py',
    'ha-ops/app/backups.py',
    'ha-ops/app/dev_harness.py',
    'ha-ops/app/direct_access.py',
    'ha-ops/app/direct_auth.py',
    'ha-ops/app/git_auth.py',
    'ha-ops/app/i18n.py',
    'ha-ops/app/jobs.py',
    'ha-ops/app/server.py',
    'ha-ops/app/state.py',
    'ha-ops/app/static/ha-ops.css',
    'ha-ops/app/static/ha-ops.js',
    'ha-ops/app/supervisor.py',
    'ha-ops/app/sync.py',
    'ha-ops/app/web.py',
    'ha-ops/build.yaml',
    'ha-ops/frontend/package-lock.json',
    'ha-ops/frontend/package.json',
    'ha-ops/frontend/src/ha-ops.js',
    'ha-ops/runtime-apk-aarch64.lock',
    'ha-ops/runtime-apk-amd64.lock',
    'ha-ops/runtime-install.sh',
    'ha-ops/runtime-pip.lock',
    'ha-ops/runtime-profile.json',
    'ha-ops/tests/browser/README.md',
    'ha-ops/tests/browser/backup-continuation.mjs',
    'ha-ops/tests/browser/direct-access.mjs',
    'ha-ops/tests/browser/direct_fixture.py',
    'ha-ops/tests/browser/folder-scope.mjs',
    'ha-ops/tests/browser/folder_scope_fixture.py',
    'ha-ops/tests/browser/linux_bridge.py',
    'ha-ops/tests/browser/migration-review-interception-test.mjs',
    'ha-ops/tests/browser/migration-review-interception.mjs',
    'ha-ops/tests/browser/migration-review-lifecycle.mjs',
    'ha-ops/tests/browser/migration-review.mjs',
    'ha-ops/tests/browser/migration_review_fixture.py',
    'ha-ops/tests/browser/native_apply_fixture.py',
    'ha-ops/tests/browser/preview-decisions-source.cjs',
    'ha-ops/tests/browser/reset-git-state.mjs',
    'ha-ops/tests/browser/reset_git_state_fixture.py',
    'ha-ops/tests/browser/run.mjs',
    'ha-ops/tests/browser/storage-compact.mjs',
    'ha-ops/tests/browser/storage-header-source.cjs',
    'ha-ops/tests/browser/storage_compact_fixture.py',
    'ha-ops/tests/integration/Dockerfile',
    'ha-ops/tests/integration/container_probe.py',
    'ha-ops/tests/integration/fixture_cleanup.py',
    'ha-ops/tests/integration/run_native_topology.py',
    'ha-ops/tests/native_topology_helpers.py',
    'ha-ops/tests/network_publication_helpers.py',
    'requirements-dev.txt',
]


def semantic_inventory():
    sources = ['server', 'client_contract', 'preview_decisions', 'migration_review_bridge',
               'native_migration_review', 'native_migration_review_http', 'native_storage',
               'native_compact', 'native_scope', 'native_apply', 'native_topology',
               'native_network_publication', 'local_virtualization', 'release_readiness',
               'selective_pre_push', 'native_future_collected']
    result = data([('ha-ops/tests/test_' + stem + '.py', 'test_case') for stem in sources])
    result['nodes'].extend(data([('ha-ops/tests/test_server.py', name) for name in
                                ('test_reset', 'test_log_scroll', 'test_command')])['nodes'])
    return result


@pytest.mark.parametrize('path', REVIEWED_DIAGNOSTIC_PATHS)
def test_reviewed_historical_diagnostic_has_explained_coverage(repo, monkeypatch, path):
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([path], []))
    plan = gate.make_plan(repo, semantic_inventory(), '')
    assert plan['mode'] == 'selective' and plan['errors'] == []
    chosen = [row for row in plan['decisions'] if row['selected']]
    if path in gate.DOCUMENTATION:
        assert not chosen
    else:
        assert chosen and all(path in row['reason'] for row in chosen)


@pytest.mark.parametrize('path', [
    '.gitignore', 'requirements-dev.txt', 'ha-ops/Dockerfile',
    'ha-ops/frontend/package.json', 'ha-ops/frontend/src/ha-ops.js',
    'ha-ops/app/server.py', 'ha-ops/app/app_context.py', 'ha-ops/dev_harness.py',
    'ha-ops/tests/browser/run.mjs',
])
def test_reviewed_shared_boundaries_include_new_collected_category(path):
    nodes = semantic_inventory()['nodes']
    areas, error = gate.areas_for_path(path, nodes)
    assert not error and areas == {gate.category(node) for node in nodes}
    assert 'native-future_collected' in areas


@pytest.mark.parametrize('filename,primary', [
    ('reset_git_state_fixture.py', {'reset'}),
    ('preview-decisions-source.cjs', {'ui-common'}),
    ('migration_review_fixture.py', {'migration', 'native-migration_review', 'native-migration_review_http'}),
    ('storage_compact_fixture.py', {'native-storage', 'native-compact'}),
    ('direct_fixture.py', {'backend-common'}),
    ('backup-continuation.mjs', {'backend-common'}),
    ('folder_scope_fixture.py', {'native-scope', 'native-apply'}),
])
def test_browser_semantic_primary_adjacent_and_reverse_edges(filename, primary):
    path = 'ha-ops/tests/browser/' + filename
    nodes = semantic_inventory()['nodes']
    nodes[-1]['dependencies'] = [path]
    areas, error = gate.areas_for_path(path, nodes)
    assert not error
    assert primary | gate.MIXED_AREAS | {'ui-common'} <= areas
    # A syntactically discovered additional consumer can never narrow policy.
    extra = data([('ha-ops/tests/test_native_additional.py', 'test_case')])['nodes'][0]
    extra['dependencies'] = [path]
    assert 'native-additional' in gate.areas_for_path(path, nodes + [extra])[0]
    assert 'native-future_collected' not in areas


@pytest.mark.parametrize('path', [
    'ha-ops/app/future.py', 'ha-ops/app/future.json', 'ha-ops/app/native_future.py',
    'ha-ops/app/native_future/native_git.py', 'ha-ops/app/native_future_collected.py',
    'ha-ops/frontend/src/future.js',
    'ha-ops/tests/browser/future.mjs', 'ha-ops/tests/browser/deleted_helper.py',
    'ha-ops/tests/integration/future.py', 'ha-ops/tests/data/future.json',
    '.github/workflows/future.yml', 'ha-ops/runtime-future.lock', 'FUTURE.md',
    'other/server.py', 'other/reset_git_state_fixture.py',
])
def test_future_semantic_inputs_block_even_when_fingerprinted_or_imported(repo, monkeypatch, path):
    inventory = semantic_inventory()
    inventory['inputs'][path] = {'digest': 'known does not mean reviewed'}
    inventory['nodes'][0]['dependencies'] = [path]
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([path], []))
    plan = gate.make_plan(repo, inventory, '')
    assert plan['errors'] and plan['mode'] == 'selective'
    assert not any('No surviving collected category' in error for error in plan['errors'])


@pytest.mark.parametrize('path', ['ha-ops/app/server.py', 'ha-ops/app/reset_git.py',
                                 'ha-ops/tests/test_reset_git.py', 'ha-ops/frontend/src/ha-ops.js',
                                 'ha-ops/tests/browser/reset-git-state.mjs', '.gitignore'])
@pytest.mark.parametrize('transform', [lambda p: './' + p, lambda p: '/' + p,
                                     lambda p: 'other/../' + p, lambda p: 'other//' + p,
                                     lambda p: p.replace('/', '//'), lambda p: p + '/',
                                     lambda p: p.replace('/', '\\')])
def test_every_semantic_rule_requires_canonical_repository_path(path, transform):
    altered = transform(path)
    if altered == path:
        return
    areas, error = gate.areas_for_path(altered, semantic_inventory()['nodes'])
    assert not areas and error


@pytest.mark.parametrize('filename', sorted(gate.BROWSER_FAMILIES))
def test_deleted_required_browser_family_and_rename_endpoint_still_block(repo, monkeypatch, filename):
    path = 'ha-ops/tests/browser/' + filename
    for missing in gate.BROWSER_FAMILIES[filename] | gate.MIXED_AREAS | {'ui-common'}:
        inventory = semantic_inventory()
        inventory['nodes'] = [n for n in inventory['nodes'] if gate.category(n) != missing]
        monkeypatch.setattr(gate, 'change_basis', lambda *a: ([path], []))
        assert 'Required category has no collected tests: ' + missing in gate.make_plan(repo, inventory, '')['errors']
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([path, 'ha-ops/tests/browser/future-name.mjs'], []))
    assert any('future-name.mjs' in e for e in gate.make_plan(repo, semantic_inventory(), '')['errors'])


def test_future_reset_only_boundary_excludes_logs_and_unrelated_native():
    inventory = semantic_inventory()
    for node in inventory['nodes']:
        node['dependencies'] = ['ha-ops/app/reset_git.py']
    assert gate.areas_for_path('ha-ops/app/reset_git.py', inventory['nodes']) == ({'reset', 'backend-common'}, None)


@pytest.mark.parametrize('filename', ['test_server.py', 'test_client_contract.py'])
def test_mixed_module_reverse_consumers_add_native_and_non_native_categories(repo, monkeypatch, filename):
    source = 'ha-ops/tests/' + filename
    inventory = semantic_inventory()
    consumers = data([('ha-ops/tests/test_native_apply.py', 'test_imported_fixture'),
                      ('ha-ops/tests/test_local_virtualization.py', 'test_imported_fixture')])['nodes']
    for node in consumers:
        node['dependencies'] = [source]
    inventory['nodes'].extend(consumers)
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([source], []))
    plan = gate.make_plan(repo, inventory, '')
    assert not plan['errors']
    assert {row['category'] for row in plan['decisions'] if row['selected']} == (
        gate.MIXED_AREAS | {'native-apply', 'virtualization'})
    assert all(source in row['reason'] for row in plan['decisions'] if row['selected'])


@pytest.mark.parametrize('filename', ['test_server.py', 'test_client_contract.py'])
@pytest.mark.parametrize('operation', ['delete', 'rename'])
@pytest.mark.parametrize('missing', sorted(gate.MIXED_AREAS))
def test_mixed_reverse_consumers_never_erase_deleted_required_family(repo, monkeypatch, filename, operation, missing):
    source = 'ha-ops/tests/' + filename
    inventory = semantic_inventory()
    inventory['nodes'] = [node for node in inventory['nodes'] if gate.category(node) != missing]
    for node in inventory['nodes']:
        if gate.category(node) in {'native-apply', 'virtualization'}:
            node['dependencies'] = [source]
    changed = [source] + (['ha-ops/tests/test_renamed_mixed.py'] if operation == 'rename' else [])
    monkeypatch.setattr(gate, 'change_basis', lambda *a: (changed, []))
    plan = gate.make_plan(repo, inventory, '')
    assert 'Required category has no collected tests: ' + missing in plan['errors']
    assert {'native-apply', 'virtualization'} <= {row['category'] for row in plan['decisions'] if row['selected']}


@pytest.mark.parametrize('path', ['release-policy.py', 'dev/helper.py',
                                 'dev/virtualization/helper.py', '.githooks/pre-push', 'README.md'])
def test_other_reviewed_inputs_keep_additive_reverse_consumers(path):
    nodes = semantic_inventory()['nodes']
    consumer = next(node for node in nodes if gate.category(node) == 'native-apply')
    consumer['dependencies'] = [path]
    areas, error = gate.areas_for_path(path, nodes)
    assert not error and 'native-apply' in areas


def test_reverse_consumers_cannot_admit_future_unknown_input(repo, monkeypatch):
    path = 'ha-ops/app/unreviewed_future.py'
    inventory = semantic_inventory()
    for node in inventory['nodes']:
        node['dependencies'] = [path]
    monkeypatch.setattr(gate, 'change_basis', lambda *a: ([path], []))
    plan = gate.make_plan(repo, inventory, '')
    assert any('Unknown influence: ' + path in error for error in plan['errors'])
    assert not any(row['selected'] for row in plan['decisions'])


def test_reviewed_execution_only_route_map_is_exact_and_future_unsafe_refuses():
    isolated = gate.execution_linux
    assert len(isolated.HOST) == 11 and len(isolated.SAVE) == 4
    for nodeid in isolated.HOST:
        node = {'id': nodeid, 'source': nodeid.split('::')[0]}
        assert isolated.route(node, True, gate.EXCLUSIVE_FAMILIES) == 'host'
    for nodeid in isolated.SAVE:
        node = {'id': nodeid, 'source': nodeid.split('::')[0]}
        assert isolated.route(node, True, gate.EXCLUSIVE_FAMILIES) == 'linux'
    for nodeid in [next(iter(isolated.SAVE)).replace('hash-object', 'future-command') + '-new',
                   'ha-ops/tests/test_future.py::test_enospc']:
        with pytest.raises(gate.shared.Refused, match='Unreviewed'):
            isolated.route({'id': nodeid, 'source': nodeid.split('::')[0]}, True, gate.EXCLUSIVE_FAMILIES)


def test_ext4_support_is_complete_ordered_and_recreation_only_second_attempt():
    isolated = gate.execution_linux
    ids = [nodeid for recipe in isolated.RECIPES.values() for nodeid in recipe] + [isolated.RECREATION, isolated.COMPACT]
    inventory = [{'id': nodeid, 'source': nodeid.split('::')[0]} for nodeid in ids]
    recipes = isolated.jobs([(node, isolated.route(node, True, gate.EXCLUSIVE_FAMILIES)) for node in inventory], inventory)
    assert {row['profile'] for row in recipes} == {'allocation', 'bytes', 'inodes'}
    selected = [nodeid for row in recipes for nodeid in row['selected']]
    assert sorted(selected) == sorted(ids)
    for row in recipes:
        first = row['attempts'][0]
        assert first[:len(isolated.RECIPES[row['profile']])] == isolated.RECIPES[row['profile']]
        assert first[-1] == isolated.RECREATION
        assert row['attempts'][1:] == ([[isolated.RECREATION]] if row['profile'] == 'allocation' else [])
    with pytest.raises(gate.shared.Refused, match='Unmapped ext4'):
        isolated.route({'id': isolated.PREFIX + 'test_future', 'source': isolated.PREFIX.split('::')[0]}, True, gate.EXCLUSIVE_FAMILIES)
    with pytest.raises(gate.shared.Refused, match='Missing mandatory'):
        isolated.jobs([(inventory[0], 'allocation')], inventory[:1])


def test_execution_route_never_host_or_receipt_and_stale_sibling_blocks(repo, monkeypatch, tmp_path):
    inventory = data([('ha-ops/tests/test_native_disk.py', 'test_disk')])
    plan = {'errors': [], 'mode': 'full', 'decisions': [{'id': node['id'], 'selected': True} for node in inventory['nodes']]}
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *a: [])
    monkeypatch.setattr(gate, 'make_plan', lambda *a: plan)
    def forbidden(*args, **kwargs):
        pytest.fail('Execution-only route entered host or receipt lookup')
    monkeypatch.setattr(gate.receipts.Store, 'lookup', forbidden)
    calls = []
    def launch(root, data, recipe, directory):
        calls.append(recipe)
        return [machine(recipe['attempts'][0], 1)], {}
    monkeypatch.setattr(gate.execution_linux, 'launch', launch)
    result = gate.execute(repo, inventory, plan, '', tmp_path, launch=forbidden)
    assert calls[0]['profile'] == 'linux'
    assert result['categories']['native-disk']['proof'] == 'execution-only; no receipt authority'
    calls.clear()
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *a: ['changed'] if calls else [])
    with pytest.raises(gate.shared.Refused, match='Selection inputs changed'):
        gate.execute(repo, inventory, plan, '', tmp_path, launch=forbidden)


@pytest.mark.parametrize('change', ['bool', 'schema', 'workers', 'skip', 'xfail'])
def test_execution_machine_strict_supported_proof(change):
    record = machine(['node'], 1)
    if change == 'bool': record['exitstatus'] = False
    if change == 'schema': record['schema'] = True
    if change == 'workers': record['worker_collections'] *= 2
    if change == 'skip': record['reports'][1]['outcome'] = 'skipped'
    if change == 'xfail': record['reports'][1]['xfail'] = True
    if change == 'skip':
        # Accounting preserves skipped as unproven; the isolated proof gate rejects it.
        assert gate.verify_outcomes(record, ['node'], 1) == ['node']
        assert 'node' not in gate.execution_linux.DEFERRED
    else:
        with pytest.raises(gate.shared.Refused):
            gate.verify_outcomes(record, ['node'], 1)


@pytest.mark.parametrize('field,value', [('supervisor_exit_code', False), ('wait_exit_code', True), ('docker_wait_returncode', None), ('watchdog_reason', 'timeout')])
def test_execution_outer_never_accepts_partial_or_boolean_success(field, value):
    row = {'wait_exit_code': 0, 'docker_wait_returncode': 0, 'supervisor_exit_code': 0,
           'watchdog_reason': 'completed', 'wait_failure': None,
           'state': {'ExitCode': 0, 'OOMKilled': False, 'Status': 'exited'}}
    row[field] = value
    outer = {'status': 'passed', 'failure': None, 'cleanup_errors': [], 'capture_errors': [], 'containers': [row]}
    with pytest.raises(gate.shared.Refused):
        gate.execution_linux.containers_success(outer, 1)


def test_execution_adapter_input_selects_its_reviewed_resource_and_controller_consumers():
    nodes = data([('ha-ops/tests/test_native_disk.py', 'test_disk'),
                  ('ha-ops/tests/test_native_ext4.py', 'test_kernel_byte_enospc'),
                  ('ha-ops/tests/test_native_git.py', 'test_git')])['nodes']
    assert gate.areas_for_path('dev/test_execution_linux.py', nodes) == ({'runner', 'virtualization', 'native-disk', 'native-ext4'}, None)


@pytest.mark.parametrize('attempt', ['0', '1'])
def test_execution_child_preserves_only_ext4_allowlist_and_exact_attempt(monkeypatch, attempt):
    isolated = gate.execution_linux
    monkeypatch.setattr(isolated.sys, 'platform', 'linux')
    monkeypatch.setattr(isolated.linux, 'prepare_child_environment', lambda: dict(isolated.linux.ENV))
    fields = {'ROOT': '/workspace/ext4', 'PROFILE': 'allocation', 'RUN': 'a' * 32,
              'DEVICE': '7:3', 'REPORT': '/workspace/output/current.json',
              'PRIOR_REPORT': '/workspace/output/prior.json', 'TOKEN': 'b' * 32, 'ATTEMPT': attempt}
    for key, value in fields.items(): monkeypatch.setenv('HA_OPS_EXT4_' + key, value)
    monkeypatch.setenv('HA_OPS_EXT4_UNREVIEWED', 'must-not-survive')
    monkeypatch.setenv('PYTEST_ADDOPTS', '--unreviewed')
    captured = {}
    def execve(program, command, environment):
        captured.update(command=command, environment=environment)
    monkeypatch.setattr(isolated.os, 'execve', execve)
    isolated.child({'profile': 'allocation', 'attempts': [['first.py::case'], ['recreate.py::case']]})
    env, command = captured['environment'], captured['command']
    assert {key for key in env if key.startswith('HA_OPS_EXT4_')} == {'HA_OPS_EXT4_' + key for key in fields}
    assert 'PYTEST_ADDOPTS' not in env
    assert all(env[key] == isolated.linux.ENV[key] for key in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'))
    assert env['TEST_PLAN_OUTPUT'].endswith('allocation-' + attempt + '.json')
    assert command[-1] == ('first.py::case' if attempt == '0' else 'recreate.py::case')
    assert command[command.index('-n') + 1] == '1'


@pytest.mark.parametrize('field,value', [('ROOT', '/foreign'), ('PROFILE', 'bytes'), ('TOKEN', 'foreign'), ('ATTEMPT', '2'), ('RUN', '')])
def test_execution_child_refuses_altered_ext4_binding_before_exec(monkeypatch, field, value):
    isolated = gate.execution_linux
    monkeypatch.setattr(isolated.sys, 'platform', 'linux')
    monkeypatch.setattr(isolated.linux, 'prepare_child_environment', lambda: dict(isolated.linux.ENV))
    fields = {'ROOT': '/workspace/ext4', 'PROFILE': 'allocation', 'RUN': 'a' * 32,
              'DEVICE': '7:3', 'REPORT': '/workspace/output/current.json',
              'PRIOR_REPORT': '/workspace/output/prior.json', 'TOKEN': 'b' * 32, 'ATTEMPT': '0'}
    fields[field] = value
    for key, value in fields.items(): monkeypatch.setenv('HA_OPS_EXT4_' + key, value)
    monkeypatch.setattr(isolated.os, 'execve', lambda *args: pytest.fail('Altered binding executed'))
    with pytest.raises(gate.shared.Refused):
        isolated.child({'profile': 'allocation', 'attempts': [['first.py::case']]})


@pytest.mark.parametrize('expected_skip', [True, False])
def test_execution_expected_deferred_skip_is_unproven_and_unexpected_skip_blocks_publication(repo, monkeypatch, tmp_path, expected_skip):
    nodeid = next(iter(gate.execution_linux.DEFERRED)) if expected_skip else 'ha-ops/tests/test_native_disk.py::test_disk'
    inventory = data([(nodeid.split('::')[0], nodeid.split('::', 1)[1])])
    plan = {'errors': [], 'mode': 'full', 'decisions': [{'id': nodeid, 'selected': True}]}
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *a: [])
    monkeypatch.setattr(gate, 'make_plan', lambda *a: plan)
    published = []
    monkeypatch.setattr(gate.receipts.Store, 'lookup', lambda *a: pytest.fail('Execution route looked up receipt'))
    monkeypatch.setattr(gate.receipts.Store, 'publish', lambda *a: published.append(a[1:]))
    def launch(root, data, recipe, directory):
        record = machine([nodeid], 1); record['reports'][1]['outcome'] = 'skipped'
        return [record], {}
    monkeypatch.setattr(gate.execution_linux, 'launch', launch)
    if expected_skip:
        result = gate.execute(repo, inventory, plan, '', tmp_path)
        assert result['not_proven'] == [nodeid]
        assert published == [({}, {})]
    else:
        with pytest.raises(gate.shared.Refused, match='Unexpected isolated skip'):
            gate.execute(repo, inventory, plan, '', tmp_path)
        assert published == []


def test_generic_linux_recipes_bound_complete_json_argument_and_exact_coverage():
    import json
    isolated = gate.execution_linux
    inventory = [{'id': 'ha-ops/tests/test_native_disk.py::test_' + str(i) + '_' + 'x' * 180,
                  'source': 'ha-ops/tests/test_native_disk.py'} for i in range(1500)]
    jobs = isolated.jobs([(node, 'linux') for node in inventory], inventory)
    assert len(jobs) > 1
    assert [nodeid for job in jobs for nodeid in job['selected']] == [node['id'] for node in inventory]
    for job in jobs:
        assert job['attempts'] == [job['selected']]
        assert len(json.dumps(job, separators=(',', ':')).encode()) <= isolated.MAX_RECIPE_BYTES


@pytest.mark.parametrize('malformed', [None, 0, '', [], {}, True])
@pytest.mark.parametrize('target,phase', [('passed-peer', 'setup'), ('deferred-peer', 'call'), ('deferred-peer', 'teardown')])
@pytest.mark.parametrize('deferred_skip', [True, False])
def test_deferred_skip_never_relaxes_strict_boolean_phase_schema(malformed, target, phase, deferred_skip):
    deferred = next(iter(gate.execution_linux.DEFERRED))
    expected = ['passed-peer', deferred]
    record = machine(expected, 1)
    if deferred_skip:
        next(row for row in record['reports'] if row['id'] == deferred and row['when'] == 'call')['outcome'] = 'skipped'
    nodeid = 'passed-peer' if target == 'passed-peer' else deferred
    next(row for row in record['reports'] if row['id'] == nodeid and row['when'] == phase)['xfail'] = malformed
    with pytest.raises(gate.shared.Refused):
        gate.verify_outcomes(record, expected, 1)


@pytest.mark.parametrize('field,value', [('reports', None), ('worker_collections', None), ('schema', True)])
def test_deferred_skip_never_relaxes_top_level_machine_shape(field, value):
    deferred = next(iter(gate.execution_linux.DEFERRED))
    record = machine(['peer', deferred], 1)
    next(row for row in record['reports'] if row['id'] == deferred and row['when'] == 'call')['outcome'] = 'skipped'
    record[field] = value
    with pytest.raises(gate.shared.Refused):
        gate.verify_outcomes(record, ['peer', deferred], 1)
