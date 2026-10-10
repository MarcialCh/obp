#!/usr/bin/env python3
"""Verify the clean release's recorded files and source-document hygiene."""
import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def manifest(root=ROOT):
    entries = []
    for line in (root / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        digest, name = line.split('  ', 1)
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid digest in release manifest')
        path = (root / name).resolve()
        path.relative_to(root.resolve())
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Unsafe manifest path')
        entries.append((digest, name))
    if len({name for _, name in entries}) != len(entries):
        raise ValueError('Duplicate manifest path')
    return entries


def verify(root=ROOT):
    entries = manifest(root)
    problems = []
    for digest, name in entries:
        path = root / name
        if path.is_symlink() or not path.is_file():
            problems.append('Missing file or symlink: ' + name)
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            problems.append('Changed file: ' + name)
        if data.startswith(b'\xef\xbb\xbf'):
            problems.append('Unexpected byte-order mark: ' + name)
        if b'\r\n' in data:
            problems.append('Expected LF line endings: ' + name)
        if re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]', data.decode('utf-8')):
            problems.append('Non-English CJK text requires review: ' + name)
        if any(part in {'.git', '__pycache__', '.venv'} for part in Path(name).parts):
            problems.append('Private/generated artifact in manifest: ' + name)
        if Path(name).name in {'postgres-config.ini', '.pgpass', '.env'}:
            problems.append('Local configuration in manifest: ' + name)
    return entries, problems


def main():
    try:
        entries, problems = verify()
    except (OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1
    for problem in problems:
        print('ERROR:', problem, file=sys.stderr)
    if problems:
        return 1
    print('Verified %d release files. External assets and generated results are outside this manifest.' % len(entries))
    return 0


if __name__ == '__main__':
    sys.exit(main())
