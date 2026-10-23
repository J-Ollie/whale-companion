"""Export allowlisted source files with instance identifiers replaced by examples.

This never copies runtime directories, databases, credentials, pictures or archives.
It updates existing files but does not delete files from the destination.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--owner-qq', required=True)
    parser.add_argument('--bot-qq', required=True)
    parser.add_argument('--server-ip', required=True)
    parser.add_argument('--phone-ip', required=True)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        parser.error('Source and destination must be separate, non-nested directories')
    replacements = [(args.owner_qq, '100000001'), (args.bot_qq, '100000002'),
                    (args.server_ip, '203.0.113.10'), (args.phone_ip, '100.64.0.10')]
    if any(not old or old == new for old, new in replacements):
        parser.error('Supply actual instance identifiers, not empty/example values')
    selected = []
    for plugin in sorted(source.glob('astrbot_plugin_companion_*')):
        if plugin.is_dir():
            selected.extend((p, Path('astrbot-plugins') / plugin.name / p.name)
                            for p in sorted(plugin.iterdir()) if p.is_file()
                            and (p.suffix in ('.py', '.json', '.yaml') or p.name == 'QZONE_SDK_LICENSE'))
    panel = source / 'qq-recovery'
    selected.extend((p, Path('management-center') / p.name) for p in sorted(panel.iterdir())
                    if p.is_file() and (p.suffix == '.py' or p.name == 'THIRD-PARTY-NOTICES.md'))
    selected.extend((panel / 'web' / name, Path('management-center/web') / name)
                    for name in ('index.html', 'app.js', 'style.css'))
    selected.extend((p, Path('tests') / p.name) for p in sorted(source.glob('test_*.py')))
    forbidden = re.compile(r'(?i)(?:ghp_|github_pat_|sk-)[a-z0-9_-]{20,}|-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----')
    manifest = []
    for original, relative in selected:
        if original.is_symlink():
            raise ValueError('Symlink source rejected: ' + str(relative))
        content = original.read_text(encoding='utf-8-sig')
        for old, new in replacements:
            content = content.replace(re.escape(old), re.escape(new))
            content = content.replace(old, new)
        if forbidden.search(content):
            raise ValueError('Credential-shaped literal rejected: ' + str(relative))
        target = destination / relative
        normalized = content.replace('\r\n', '\n')
        existing = target.read_bytes() if target.exists() else b''
        # Preserve the established file's line endings for a focused public diff.
        if b'\r\n' in existing:
            normalized = normalized.replace('\n', '\r\n')
        payload = normalized.encode('utf-8')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        manifest.append({'path': relative.as_posix(), 'bytes': len(payload),
                         'sha256': hashlib.sha256(payload).hexdigest()})
    output = destination / 'docs/source-manifest.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Exported {len(manifest)} allowlisted source/test files; no runtime data copied')


if __name__ == '__main__':
    main()
