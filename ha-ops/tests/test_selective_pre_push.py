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
    return {'nodes': [{'id': source + '::' + name, 'source': source} for source, name in nodes],
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
    assert gate.areas_for_path('ha-ops/app/server.py', inventory['nodes'])[1]


def machine(ids):
    return {'errors': [], 'exitstatus': 0, 'deselected': [], 'worker_collections': [ids, ids],
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


def test_ordinary_finishes_before_all_nested_timing_families(repo, monkeypatch, tmp_path):
    inventory = data([('ha-ops/tests/test_server.py', 'test_command'), *
                      [('ha-ops/tests/' + name, 'test_case') for name in sorted(gate.ISOLATED)]])
    plan = {'errors': [], 'mode': 'full', 'decisions': [{'id': node['id'], 'selected': True} for node in inventory['nodes']]}
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *args: [])
    monkeypatch.setattr(gate, 'make_plan', lambda *args: plan)
    calls = []
    def launch(root, args, directory):
        ids = [line for arg in args if arg.startswith('@') for line in Path(arg[1:]).read_text().splitlines()]
        calls.append((ids, int(args[1])))
        return machine(ids), {}
    result = gate.execute(repo, inventory, plan, '', tmp_path, launch=launch)
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
    inventory = data([('ha-ops/tests/test_native_disk.py', 'test_linux_real_allocations_with_capped_initial_capacity_warning')])
    plan = {'errors': [], 'mode': 'full', 'decisions': [{'id': node['id'], 'selected': True} for node in inventory['nodes']]}
    monkeypatch.setattr(gate.shared, 'validate_map', lambda *a: [])
    monkeypatch.setattr(gate, 'make_plan', lambda *a: plan)
    def forbidden(*a):
        pytest.fail('Exclusive case entered host launch')
    with pytest.raises(gate.shared.Refused, match='qualified isolated'):
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
