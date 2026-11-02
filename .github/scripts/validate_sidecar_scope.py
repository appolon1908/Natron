"""Validate owned sidecar source while freezing the untested native engine snapshot."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess


PROFILES = {
    'appolon1908/Blender': {
        'base': '42884f20f38258b33cb2d9c4d7d7982a2112de27',
        'tree': '33c4029b7cb3e8bd268719fa0fd9692e04ca391b',
        'gitlinks': {},
    },
    'appolon1908/Natron': {
        'base': 'c987a8c88b3d718eaff790e672bcde524d3da312',
        'tree': 'e7066f0e8c4faa3439ba6206e30b6ca6895147bc',
        'gitlinks': {
            'Tests/google-mock': '17945db42c0b42496b2f3c6530307979f2e2a5ce',
            'Tests/google-test': '50d6fc317c843a2e40dbf08c2efd3f068801ae6d',
            'libs/OpenFX': '2303ff811bee3ffe085287602f684fe5fe5357e0',
            'libs/SequenceParsing': '3c93fcc488632b0bdfeee3181586809932357598',
            'libs/google-breakpad': '9474c3f7f9939391f281d46c42bfe20cc0f0abd9',
        },
    },
}
OWNED_SOURCE = 'codestra_api/server.py'
MANIFEST = '.github/video-source-boundary.json'
POLICY = 'governance/interim-release-policy.json'
CI_FILES = frozenset({
    '.github/workflows/video-source.yml',
    '.github/scripts/validate_sidecar_scope.py',
    '.github/scripts/test_sidecar_scope.py',
    '.github/scripts/test_sidecar_api.py',
    '.github/scripts/scan_sidecar_secrets.py',
    '.github/gitleaks-source.toml',
    '.github/requirements-video-source.txt',
    MANIFEST, POLICY,
})


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read_json(value):
    return json.loads(value, object_pairs_hook=unique_pairs)


def same_json(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def expected_manifest(repository):
    profile = PROFILES[repository]
    return {
        'schema_version': 1, 'repository': repository,
        'reviewed_base_sha': profile['base'], 'reviewed_base_tree': profile['tree'],
        'tested_sources': [OWNED_SOURCE], 'native_engine_certified': False,
        'scope': 'owned-sidecar-source-with-frozen-native-snapshot',
    }


def source_policy(repository):
    return {
        'schema_version': 1, 'policy_kind': 'source_merge', 'repository': repository,
        'authorization_date': '2026-11-02',
        'required_checks': [{'context': 'video-sidecar-source', 'integration_id': 15368,
                             'workflow': '.github/workflows/video-source.yml'}],
        'replaces_contexts': ['codestra-control-plane', 'codestra/control-plane'],
        'preserve': ['other_required_checks', 'review_requirements', 'ref_conditions',
                     'bypass_actors', 'enforcement', 'force_push_protection',
                     'deletion_protection', 'deployment_authority'],
        'production_activation_authorized': False,
        'note': 'This gate tests the owned sidecar API with process doubles and freezes every other original native, configuration, contract and authority Git object. Native engine edits, new executable sources and CI authority changes require separate reviewed validation. It does not certify rendering, hardware, upstream security or runtime deployment.',
    }


def git_entries(root, ref=None):
    command = (['git', 'ls-tree', '-rz', '--full-tree', ref] if ref
               else ['git', 'ls-files', '--stage', '-z'])
    raw = subprocess.check_output(command, cwd=root).decode()
    entries = {}
    for row in raw.split('\0'):
        if not row:
            continue
        metadata, name = row.split('\t', 1)
        if ref:
            mode, kind, oid = metadata.split()
        else:
            mode, oid, stage = metadata.split()
            if stage != '0':
                raise ValueError('Unmerged candidate index')
            kind = 'commit' if mode == '160000' else 'blob'
        entries[name] = {'mode': mode, 'kind': kind, 'oid': oid}
    return entries


def baseline_entries(root, repository):
    profile = PROFILES[repository]
    tree = subprocess.check_output(
        ['git', 'rev-parse', profile['base'] + '^{tree}'], cwd=root, text=True).strip()
    if tree != profile['tree']:
        raise ValueError('Reviewed immutable native root tree differs')
    entries = git_entries(root, profile['base'])
    observed_links = {name: row['oid'] for name, row in entries.items() if row['mode'] == '160000'}
    if observed_links != profile['gitlinks']:
        raise ValueError('Unreviewed native gitlink inventory')
    for row in entries.values():
        if (row['mode'], row['kind']) not in {
                ('100644', 'blob'), ('100755', 'blob'), ('120000', 'blob'), ('160000', 'commit')}:
            raise ValueError('Unreviewed original Git object type/mode')
    if entries.get(OWNED_SOURCE, {}).get('mode') != '100644':
        raise ValueError('Owned sidecar source is missing or executable')
    return entries


def trusted_manifest(root, repository, base, bootstrap):
    if not re.fullmatch('[a-f0-9]{40}', base):
        raise ValueError('Missing or invalid trusted event base')
    profile = PROFILES[repository]
    existing = subprocess.run(['git', 'show', base + ':' + MANIFEST], cwd=root,
                              capture_output=True, check=False)
    if existing.returncode == 0:
        changes = subprocess.check_output(
            ['git', 'diff', '--name-only', base, '--', '.github', POLICY], cwd=root)
        if changes.strip():
            raise ValueError('CI authority changed; independent authority review required')
        if not same_json(read_json(existing.stdout), expected_manifest(repository)):
            raise ValueError('Trusted base manifest differs from reviewed profile')
    elif base != profile['base'] or bootstrap != profile['base']:
        raise ValueError('Unrecognized bootstrap; no permissive fallback')
    return expected_manifest(repository)


def validate_entries(entries, baseline, manifest, repository):
    errors = []
    if not same_json(manifest, expected_manifest(repository)):
        errors.append('Candidate scope manifest differs from trusted native/owned-source profile')
    for name, expected in baseline.items():
        if name == OWNED_SOURCE:
            continue
        if entries.get(name) != expected:
            errors.append('Frozen original native/config/authority object changed: ' + name)
    allowed = set(baseline) | CI_FILES
    for name in set(entries) - allowed:
        errors.append('New unvalidated source/config path: ' + name)
    for name in CI_FILES | {OWNED_SOURCE}:
        row = entries.get(name, {})
        if row.get('mode') != '100644' or row.get('kind') != 'blob':
            errors.append('Required reviewed source/CI file missing or unsafe: ' + name)
    return errors


def main():
    root = Path.cwd()
    repository = os.environ['GITHUB_REPOSITORY']
    if repository not in PROFILES:
        raise SystemExit('Unreviewed repository')
    subprocess.run(['git', 'diff', '--exit-code'], cwd=root, check=True)
    trusted = trusted_manifest(root, repository, os.environ.get('BASE_SHA', ''),
                               os.environ.get('BOOTSTRAP_BASE_SHA', ''))
    manifest = read_json((root / MANIFEST).read_text())
    if not same_json(manifest, trusted):
        raise SystemExit('Candidate manifest is not the trusted event-base authority')
    errors = validate_entries(git_entries(root), baseline_entries(root, repository), manifest, repository)
    if not same_json(read_json((root / POLICY).read_text()), source_policy(repository)):
        errors.append('Source-only policy differs from reviewed guard context and controls')
    source = root / OWNED_SOURCE
    if source.is_symlink():
        errors.append('Owned source symlink is forbidden')
    else:
        ast.parse(source.read_text(encoding='utf-8'), filename=OWNED_SOURCE)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('OWNED_SIDECAR_SCOPE=PASS; all native/config objects frozen; rendering/runtime NOT certified')


if __name__ == '__main__':
    main()
