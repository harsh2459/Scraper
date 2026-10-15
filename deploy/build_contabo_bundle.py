"""Build a code-only staging archive. Private files are never candidates."""
import hashlib
import argparse
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_TYPES = {'.ts', '.tsx', '.css', '.svg', '.png', '.jpg', '.jpeg', '.webp',
                  '.woff', '.woff2', '.ico', '.json', '.html'}


def release_files(root):
    paths = {root / '.dockerignore', root / 'deploy/fleet-service-grants.sql'}
    for directory in ('shared-scraper', 'shared-scraper/control_plane',
                      'shared-scraper/migrations', 'shared-scraper/tm_manager'):
        folder = root / directory
        paths.update(folder.glob('*.py'))
        if (folder / 'requirements.txt').exists():
            paths.add(folder / 'requirements.txt')
    paths.update((root / 'shared-scraper/migrations').glob('[0-9]*.sql'))
    paths.update((root / 'shared-scraper/tm_manager/templates').rglob('*.html'))
    for filename in ('parse_journal.py', 'auto_fetch_journal.py'):
        paths.add(root / 'shared-scraper/tm_manager/scripts' / filename)
    for directory in ('shared-scraper/tm_manager/frontend', 'shared-scraper/ops_frontend'):
        for path in (root / directory).rglob('*'):
            parts = path.relative_to(root / directory).parts
            if any(part in ('node_modules', 'dist', '__pycache__') or part.startswith('.') for part in parts):
                continue
            if path.is_file() and path.suffix in FRONTEND_TYPES:
                paths.add(path)
    for path in (root / 'deploy/containers').iterdir():
        if path.is_file() and (path.suffix in ('.py', '.sh', '.yaml', '.md', '.conf') or path.name == 'Dockerfile'):
            paths.add(path)
    for path in paths:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unexpected missing file or symlink in release selection.')
    return sorted(paths)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--production', action='store_true')
    args = parser.parse_args()
    output = ROOT / ('.runtime/contabo-production.tar.gz' if args.production else '.runtime/contabo-staging.tar.gz')
    output.parent.mkdir(exist_ok=True)
    files = release_files(ROOT)
    manifest = {'kind': 'production' if args.production else 'staging-only; not a production release', 'files': {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files}}
    with tarfile.open(output, 'w:gz') as archive:
        for path in files:
            info = archive.gettarinfo(str(path), arcname=path.relative_to(ROOT).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o644
            with path.open('rb') as handle:
                archive.addfile(info, handle)
        data = (json.dumps(manifest, indent=2) + '\n').encode()
        info = tarfile.TarInfo('release-manifest.json')
        info.size, info.mode = len(data), 0o644
        archive.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
    print('Created code-only release archive:', output)
    print('Files:', len(files), 'Bytes:', output.stat().st_size)
    print('No runtime exports, OAuth bundle, VPN configs, database dumps or .env files included.')


if __name__ == '__main__':
    main()
