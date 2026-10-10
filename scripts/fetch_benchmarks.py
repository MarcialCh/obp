#!/usr/bin/env python3
"""Install only the benchmark resources in the pinned dependency manifest."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def valid(data, entry):
    header = ('blob %d\0' % len(data)).encode('ascii')
    return (len(data) == entry['size'] and
            hashlib.sha1(header + data).hexdigest() == entry['git_blob_sha1'] and
            hashlib.sha256(data).hexdigest() == entry['sha256'])


def install(entry, manifest, root, local_source, replace):
    relative = Path(entry['path'])
    target = (root / relative).resolve()
    target.relative_to(root.resolve())
    if target.exists():
        if valid(target.read_bytes(), entry):
            return 'Verified: ' + entry['path']
        if not replace:
            raise RuntimeError('Different existing file: %s; inspect it, then use --replace if intended' % relative)
    if local_source:
        data = (local_source / relative).read_bytes()
    else:
        url = manifest['raw_base'] + entry['path']
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'query-pricing-artifact'})
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
    if not valid(data, entry):
        raise RuntimeError('Content verification failed: ' + entry['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.download.tmp')
    temporary.write_bytes(data)
    temporary.replace(target)
    return 'Installed: ' + entry['path']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenarios', default='all', help='Comma-separated names, or all')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--from-directory', type=Path, help='Import exact pinned files without network access')
    parser.add_argument('--replace', action='store_true', help='Replace mismatching external files after verification')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'dependencies/forbackbench.lock.json').read_text())
    selected = set(manifest['scenarios'] if args.scenarios == 'all' else args.scenarios.split(','))
    unknown = selected - set(manifest['scenarios'])
    if unknown:
        parser.error('Unknown scenarios: ' + ', '.join(sorted(unknown)))
    files = [e for e in manifest['files'] if e['group'] == 'runtime' or e['group'] in selected]
    print('ForBackBench revision:', manifest['revision'], flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(install, e, manifest, args.root, args.from_directory, args.replace) for e in files]
        failed = []
        for entry, future in zip(files, futures):
            try:
                print(future.result(), flush=True)
            except Exception as exc:
                failed.append(entry['path'])
                print('ERROR: %s: %s' % (entry['path'], exc), file=sys.stderr, flush=True)
    if failed:
        print('Rerun to resume; already verified files are kept.', file=sys.stderr)
        return 1
    print('All %d resources verified. Next: configure a database and generate CSVs.' % len(files))
    return 0


if __name__ == '__main__':
    sys.exit(main())
