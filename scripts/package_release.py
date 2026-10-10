#!/usr/bin/env python3
"""Recreate the source ZIP using only the verified release manifest."""
import argparse
from pathlib import Path
import zipfile

from verify_artifact import ROOT, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    entries, problems = verify()
    if problems:
        parser.error('; '.join(problems))
    if args.out.exists():
        parser.error('Output already exists; choose a new archive path')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    names = sorted([name for _, name in entries] + ['SHA256SUMS'])
    with zipfile.ZipFile(args.out, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            info = zipfile.ZipInfo('query-pricing-artifact/' + name, date_time=(2000, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (ROOT / name).read_bytes(), compresslevel=9)
    print('Created:', args.out)


if __name__ == '__main__':
    main()
