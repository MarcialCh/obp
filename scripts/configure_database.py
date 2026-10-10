#!/usr/bin/env python3
"""Write password-free experimental database configurations without connecting."""
import argparse
import json
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenarios', default='all')
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=5432)
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--database-prefix', default='pricing_')
    args = parser.parse_args()
    known = json.loads((ROOT / 'dependencies/forbackbench.lock.json').read_text())['scenarios']
    names = known if args.scenarios == 'all' else args.scenarios.split(',')
    if not 1 <= args.port <= 65535 or set(names) - set(known):
        parser.error('Invalid port or scenario selection')
    for name in names:
        folder = ROOT / 'scenarios' / name
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / 'postgres-config.ini'
        if dest.exists():
            print('Kept existing configuration:', dest.relative_to(ROOT))
            continue
        values = dict(PGHOST=args.host, PGPORT=str(args.port), PGUSER=args.user,
                      PGDATABASE=args.database_prefix + name.lower())
        if any('\n' in v or '\r' in v for v in values.values()):
            parser.error('Configuration values must be single-line strings')
        text = '# Experimental database; the full-size runner rebuilds it.\n'
        text += '# Use libpq .pgpass or PGPASSWORD for authentication.\n'
        text += ''.join(k + '=' + shlex.quote(v) + '\n' for k, v in values.items())
        dest.write_text(text, encoding='utf-8', newline='\n')
        print('Created:', dest.relative_to(ROOT))


if __name__ == '__main__':
    main()
