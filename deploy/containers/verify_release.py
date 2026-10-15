import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
manifest = json.loads((root / 'release-manifest.json').read_text())
for name, checksum in manifest['files'].items():
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise SystemExit('Release file invalid; stop.')
    if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
        raise SystemExit('Release checksum mismatch; stop.')
print('Release file checksums verified:', len(manifest['files']))
