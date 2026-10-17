"""Portable guards complement, never replace, executed App-image proof."""
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import native_runtime as runtime


@pytest.fixture
def observed():
    return {'os': 'alpine', 'release': '3.24.1', 'architecture': 'aarch64',
            'git': 'git version 2.54.0', 'python': '3.14.8', 'ssl': 'reviewed',
            'ssh': 'reviewed', 'packages': ['git-2.54.0-r0'],
            'files': {'/usr/bin/git': {'realpath': '/usr/bin/git', 'sha256': 'reviewed'}}}


def profile(tmp_path, monkeypatch, observed):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps({'schema': 1, 'qualified': {'aarch64': observed}}))
    monkeypatch.setattr(runtime, 'PROFILE', path)
    return path


def test_exact_reviewed_profile_is_observational(tmp_path, monkeypatch, observed):
    profile(tmp_path, monkeypatch, observed)
    monkeypatch.setattr(runtime, 'observe', lambda: observed)
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k: pytest.fail('profile admission executed subprocess'))
    assert runtime.qualify() == 'alpine-aarch64-2.54.0'


@pytest.mark.parametrize('field,value', [('os', 'unknown'), ('architecture', 'armv7l'),
    ('git', 'git version 2.55.0'), ('python', '3.14.9'), ('ssl', 'other'), ('ssh', 'other'),
    ('packages', ['git-2.54.0-r1']), ('files', {})])
def test_tuple_drift_refuses_without_environment_self_approval(tmp_path, monkeypatch, observed, field, value):
    profile(tmp_path, monkeypatch, observed)
    changed = {**observed, field: value}
    monkeypatch.setattr(runtime, 'observe', lambda: changed)
    monkeypatch.setenv('HA_OPS_NATIVE_GIT_VERSION', changed['git'])
    monkeypatch.setenv('HA_OPS_RUNTIME_PROFILE', str(tmp_path / 'profile.json'))
    with pytest.raises(runtime.UnsupportedRuntime, match='Unsupported native runtime.*requalify'):
        runtime.qualify()


@pytest.mark.parametrize('payload', ['{', '{}', '{"schema":1,"qualified":[]}', '{"schema":1,"qualified":null}'])
def test_missing_or_malformed_identity_fails_actionably(tmp_path, monkeypatch, observed, payload):
    path = profile(tmp_path, monkeypatch, observed)
    path.write_text(payload)
    monkeypatch.setattr(runtime, 'observe', lambda: observed)
    with pytest.raises(runtime.UnsupportedRuntime, match='requalify'):
        runtime.qualify()


def test_legacy_development_profile_is_separate(monkeypatch):
    monkeypatch.setattr(runtime, 'observe', lambda: {'os': 'debian', 'release': '12',
        'architecture': 'aarch64', 'git': 'git version 2.39.5', 'python': '3.12.10'})
    assert runtime.qualify() == 'debian-development-2.39.5'
