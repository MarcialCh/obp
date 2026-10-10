#!/usr/bin/env python3
"""Run all exact pricing methods on the bundled shared-tuple example."""
import argparse
import datetime
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--methods', default='KCPrice,SATPrice,SATPrice-NoReduction,Baseline')
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--warmups', type=int, default=0)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--timeout', type=float, default=100)
    args = parser.parse_args()
    installed = ROOT / 'utilityTools/kcprice' / ('c2d.exe' if os.name == 'nt' else 'c2d')
    c2d = os.environ.get('C2D_BIN') or (str(installed) if installed.is_file() else 'c2d')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out = args.out or ROOT / 'experiments/demo' / stamp
    command = [sys.executable, str(ROOT / 'utilityTools/pricing/pricing_suite.py'), 'compare',
               '--input', str(ROOT / 'utilityTools/pricing/examples/shared_tuple.json'),
               '--out', str(out), '--methods', args.methods, '--c2d', c2d,
               '--runs', str(args.runs), '--warmups', str(args.warmups), '--timeout', str(args.timeout)]
    return subprocess.call(command)


if __name__ == '__main__':
    sys.exit(main())
