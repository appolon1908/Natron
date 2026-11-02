"""Complete scans with trusted controls; source comments/configuration cannot suppress findings."""
import argparse
import json
import os
from pathlib import Path
import secrets
import string
import subprocess
import tempfile
import tomllib


class ForbiddenSourceIgnore(ValueError):
    pass


def command(scanner, config, ignore, kind, target, report):
    if kind not in ('git', 'dir'):
        raise ValueError('Unknown scanner scope')
    return [str(scanner), kind, '--no-banner', '--redact=100', '--exit-code', '1',
            '--ignore-gitleaks-allow', '--config', str(config),
            '--gitleaks-ignore-path', str(ignore), '--report-format', 'json',
            '--report-path', str(report), str(target)]


def scan(scanner, config, ignore, kind, root, report, env=None):
    # Gitleaks loads source/.gitleaksignore additively even when -i is explicit.
    # This reviewed lane has no exemptions; do not let that implicit file exist.
    source_ignore = root / '.gitleaksignore'
    if source_ignore.exists() or source_ignore.is_symlink():
        raise ForbiddenSourceIgnore('Candidate .gitleaksignore is forbidden')
    report.unlink(missing_ok=True)
    result = subprocess.run(command(scanner, config, ignore, kind, '.', report),
                            cwd=root, env=env, capture_output=True)
    findings = json.loads(report.read_text())
    if (type(result.returncode) is not int or result.returncode not in (0, 1)
            or not isinstance(findings, list) or (result.returncode == 1) != bool(findings)
            or any(not isinstance(item, dict) for item in findings)):
        raise ValueError('Scanner error, malformed report or status/report mismatch')
    return findings


def verify_controls(scanner, config, ignore):
    parsed = tomllib.loads(config.read_text())
    if (set(parsed) != {'extend'} or set(parsed['extend']) != {'useDefault'}
            or parsed['extend']['useDefault'] is not True):
        raise ValueError('Only the trusted complete built-in rule set is allowed')
    if ignore.read_bytes() != b'':
        raise ValueError('This reviewed lane permits no fingerprint exemptions')
    with tempfile.TemporaryDirectory() as fixture, tempfile.TemporaryDirectory() as reports:
        root = Path(fixture)
        # Generated locally, never issued or sent anywhere; values are never printed.
        token = 'ghp_' + ''.join(secrets.SystemRandom().sample(string.ascii_letters + string.digits, 36))
        (root / 'credential.py').write_text('api_key = "' + token + '" # gitleaks:allow\n')
        report = Path(reports) / 'probe.json'
        control = scan(scanner, config, ignore, 'dir', root, report)
        matches = [item for item in control if item.get('RuleID') == 'github-pat'
                   and item.get('File', '').endswith('credential.py')]
        if not matches:
            raise ValueError('Inline allow comment concealed a generated credential')
        (root / '.gitleaksignore').write_text('\n'.join(item['Fingerprint'] for item in control) + '\n')
        try:
            scan(scanner, config, ignore, 'dir', root, report)
        except ForbiddenSourceIgnore:
            pass
        else:
            raise ValueError('Implicit candidate .gitleaksignore was not rejected')
        (root / '.gitleaksignore').unlink()
        malicious = '[allowlist]\ndescription = "Untrusted candidate config"\nregexes = [".*"]\n'
        (root / '.gitleaks.toml').write_text(malicious)
        env = dict(os.environ, GITLEAKS_CONFIG=str(root / '.gitleaks.toml'),
                   GITLEAKS_CONFIG_TOML=malicious)
        attacked = scan(scanner, config, ignore, 'dir', root, report, env)
        if not any(item.get('RuleID') == 'github-pat' and item.get('File', '').endswith('credential.py')
                   for item in attacked):
            raise ValueError('Candidate ignore/config or environment concealed a generated credential')
    print('INLINE_COMMENT_IGNORE_FILE_AND_CONFIG_SUPPRESSION_REJECTED=PASS')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scanner', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--ignore-path', type=Path, required=True)
    parser.add_argument('--reports', type=Path, required=True)
    args = parser.parse_args()
    scanner, config, ignore = (value.resolve() for value in (args.scanner, args.config, args.ignore_path))
    args.reports.mkdir(parents=True, exist_ok=True)
    failed = False
    try:
        verify_controls(scanner, config, ignore)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('SCANNER_CONTROL_REGRESSION=FAIL')
        failed = True
    for kind in ('git', 'dir'):
        try:
            findings = scan(scanner, config, ignore, kind, Path.cwd(), args.reports / (kind + '.json'))
            metadata = [{key: item.get(key) for key in
                         ('File', 'StartLine', 'EndLine', 'RuleID', 'Commit', 'Fingerprint')}
                        for item in findings]
            print(json.dumps({'scope': kind, 'finding_metadata': metadata}, sort_keys=True))
            print(kind.upper() + '_UNAPPROVED_FINDINGS=' + str(len(findings)))
            failed |= bool(findings)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            print(kind.upper() + '_SCANNER_EXECUTION=FAIL')
            failed = True
    if failed:
        raise SystemExit('Secret validation failed; no findings or scanner errors are waived')


if __name__ == '__main__':
    main()
