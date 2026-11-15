"""Owned machine records for test_plan; no terminal-output parsing."""
import inspect
import ast
import textwrap
from functools import lru_cache
from collections import Counter
import json
import os
from pathlib import Path

import pytest

__test__ = False
STATE = None


def pytest_configure(config):
    global STATE
    STATE = {'schema': 1, 'collected': [], 'items': [], 'reports': [],
             'deselected': [], 'errors': [], 'worker_collections': [], 'discovered': [], 'plugins': []}
    for name in ('keyword', 'markexpr', 'deselect', 'lf', 'ff', 'stepwise', 'stepwise_skip'):
        if config.getoption(name, default=None):
            raise pytest.UsageError('test_plan refuses collection filter: ' + name)
    if pytest.__version__ != '8.4.2':
        raise pytest.UsageError('test_plan fixture introspection requires pytest 8.4.2')


def relative(path, root):
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=None)
def module_aliases(module):
    aliases = {}
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            aliases.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom):
            aliases.update({a.asname or a.name: (node.module or '') + '.' + a.name for a in node.names})
    return aliases


@lru_cache(maxsize=None)
def call_names(function):
    try:
        source = ast.parse(textwrap.dedent(inspect.getsource(function)))
        aliases = module_aliases(inspect.getmodule(function))
        def name(node):
            if isinstance(node, ast.Name): return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute): return name(node.value) + '.' + node.attr
            return '<indirect>'
        return sorted({name(node.func) for node in ast.walk(source) if isinstance(node, ast.Call)})
    except (OSError, TypeError, SyntaxError):
        return ['<unknown>']


def pytest_itemcollected(item):
    STATE['discovered'].append(item.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    root = Path(os.environ['TEST_PLAN_ROOT']).resolve()
    for _, plugin in config.pluginmanager.list_name_plugin():
        if plugin is None:
            continue
        try:
            source = inspect.getsourcefile(plugin if inspect.ismodule(plugin) or inspect.isclass(plugin) else type(plugin))
            local = relative(source, root)
            if local and {'source': local} not in STATE['plugins']:
                STATE['plugins'].append({'source': local})
        except (TypeError, OSError):
            pass
    STATE['plugins'].sort(key=lambda value: value['source'])
    for item in items:
        fixtures, unknown, calls = [], [], set(call_names(item.obj))
        try:
            info = item._fixtureinfo
            for name in sorted(item.fixturenames):
                definitions = info.name2fixturedefs.get(name, ())
                if not definitions:
                    # request is pytest's synthetic request object.
                    if name != 'request':
                        unknown.append('fixture provider unavailable: ' + name)
                    continue
                # Include all providers in an override chain: super-fixture
                # references can bind earlier definitions in pinned pytest.
                for definition in definitions:
                    source = inspect.getsourcefile(definition.func)
                    calls.update(call_names(definition.func))
                    fixtures.append({'name': name, 'source': relative(source, root),
                                     'external': relative(source, root) is None})
        except (AttributeError, TypeError, OSError) as error:
            unknown.append('fixture introspection: ' + type(error).__name__)
        STATE['items'].append({'id': item.nodeid,
                               'source': relative(item.path, root),
                               'fixtures': sorted(fixtures, key=lambda v: (v['name'], v['source'] or '')),
                               'markers': sorted({mark.name for mark in item.iter_markers()}),
                               'calls': sorted(calls),
                               'unknown': sorted(unknown)})


def pytest_collection_finish(session):
    STATE['collected'] = [item.nodeid for item in session.items]
    if Counter(STATE['discovered']) != Counter(STATE['collected']):
        STATE['errors'].append('collection was modified after actual item discovery')
        raise pytest.UsageError('test_plan refuses silent/explicit collection filtering')


def pytest_deselected(items):
    STATE['deselected'].extend(item.nodeid for item in items)


def pytest_collectreport(report):
    if report.failed:
        STATE['errors'].append('collection failed: ' + report.nodeid)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    STATE['worker_collections'].append(list(ids))


def pytest_runtest_logreport(report):
    try:
        report.wasxfail
        xfail = True
    except AttributeError:
        xfail = False
    STATE['reports'].append({'id': report.nodeid, 'when': report.when,
                             'outcome': report.outcome, 'duration': report.duration,
                             'xfail': xfail})


def pytest_sessionfinish(session, exitstatus):
    try:
        session.config.workerinput
    except AttributeError:
        pass
    else:
        return
    STATE['exitstatus'] = int(exitstatus)
    output = Path(os.environ['TEST_PLAN_OUTPUT'])
    with output.open('x', encoding='utf-8') as handle:
        json.dump(STATE, handle, sort_keys=True)
        handle.write('\n')
