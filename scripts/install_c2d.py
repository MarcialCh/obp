#!/usr/bin/env python3
"""Install a locally obtained c2d executable; do not modify the binary."""
import argparse
import hashlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    data = args.source.read_bytes()
    if not data.startswith((b'\x7fELF', b'MZ')):
        parser.error('Expected an ELF or Windows executable, not an archive or web page')
    windows = data.startswith(b'MZ')
    if windows != (os.name == 'nt'):
        parser.error('This binary targets a different operating system')
    dest = ROOT / 'utilityTools/kcprice' / ('c2d.exe' if windows else 'c2d')
    if dest.exists() and dest.read_bytes() != data and not args.replace:
        parser.error('A different c2d is installed; use --replace to replace it')
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.read_bytes() != data:
        dest.write_bytes(data)
    dest.chmod(0o755)
    print('Installed c2d SHA256:', hashlib.sha256(data).hexdigest())
    print('Set C2D_BIN to:', dest)
    print('The original c2d license and notices continue to apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
