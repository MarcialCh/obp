"""Reproducible synthetic tuple prices, shared before any pricing worker runs."""
from __future__ import annotations
import argparse
import copy
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import math
import os
import time
from pathlib import Path

from pricing.common import KCError, Prices, write_json, sha256_file

VERSION = 'tuple-prices-v1'
PRICE_FIELDS = ('price_mode', 'price_seed', 'price_decimals', 'price_parameters', 'price_config_sha256')
MODES = ('uniform', 'random', 'skewed', 'heavy-tailed')


def add_price_arguments(parser):
    env = os.environ
    parser.add_argument('--prices-csv', type=Path, default=env.get('PRICE_CSV') or None)
    parser.add_argument('--unit-price', default=env.get('UNIT_PRICE', '1'))
    parser.add_argument('--price-seed', default=env.get('PRICE_SEED') or '2026',
                        help='Reproducible seed; default 2026')
    parser.add_argument('--price-mode', choices=('all', *MODES), default=env.get('PRICE_MODE') or None,
                        help='Default all: uniform, random, skewed, heavy-tailed')
    parser.add_argument('--price-decimals', type=int, default=env.get('PRICE_DECIMALS', '3'))
    parser.add_argument('--price-skew-prob', default=env.get('PRICE_SKEW_PROB', '0.9'))
    parser.add_argument('--price-skew-cutoff', default=env.get('PRICE_SKEW_CUTOFF', '1'))
    parser.add_argument('--price-pareto-alpha', default=env.get('PRICE_PARETO_ALPHA', '1.5'))
    parser.add_argument('--price-pareto-min', default=env.get('PRICE_PARETO_MIN', '1'))


def _decimal(value, label):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise KCError(label + ' must be a finite decimal') from exc
    if not result.is_finite():
        raise KCError(label + ' must be a finite decimal')
    return result


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def settings(args):
    """Validate before a DB connection/load; preserve explicit legacy controls."""
    mode = getattr(args, 'price_mode', None)
    seed = getattr(args, 'price_seed', None)
    csv = getattr(args, 'prices_csv', None)
    unit = _decimal(getattr(args, 'unit_price', '1'), 'UNIT_PRICE')
    Prices(('unit',), {'unit': str(unit)})
    if mode is not None and mode not in MODES:
        raise KCError('PRICE_MODE must be one of: ' + ', '.join(MODES))
    if csv and mode is not None:
        raise KCError('PRICE_CSV cannot be combined with PRICE_MODE')
    if mode is not None and unit != 1:
        raise KCError('PRICE_MODE requires UNIT_PRICE=1; uniform means every tuple costs exactly 1')
    if csv:
        mode = 'seller_csv'
        seed = None
    elif mode is None:
        mode = 'legacy-random' if seed is not None else 'uniform' if unit == 1 else 'legacy-unit'
    params = {}
    decimals = None
    if mode in ('random', 'skewed', 'heavy-tailed'):
        seed = str(seed) if seed is not None else '2026'
        decimals = getattr(args, 'price_decimals', 3)
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 1 <= decimals <= 6:
            raise KCError('PRICE_DECIMALS must be an integer from 1 to 6')
        params = dict(decimals=decimals)
        if mode in ('random', 'skewed'):
            params.update(lower='0', upper='10', endpoints='open', sampling='decimal-grid')
        if mode == 'skewed':
            probability = _decimal(getattr(args, 'price_skew_prob', '0.9'), 'PRICE_SKEW_PROB')
            cutoff = _decimal(getattr(args, 'price_skew_cutoff', '1'), 'PRICE_SKEW_CUTOFF')
            if not 0 < probability < 1 or probability.as_tuple().exponent < -6:
                raise KCError('PRICE_SKEW_PROB must be in (0,1) with at most 6 decimal places')
            if not Decimal(1).scaleb(-decimals) < cutoff < 10 or cutoff * 10**decimals != (cutoff * 10**decimals).to_integral_value():
                raise KCError('PRICE_SKEW_CUTOFF must be a PRICE_DECIMALS grid point above one tick and below 10')
            params.update(low_probability=_text(probability), cutoff=_text(cutoff))
        elif mode == 'heavy-tailed':
            alpha = _decimal(getattr(args, 'price_pareto_alpha', '1.5'), 'PRICE_PARETO_ALPHA')
            minimum = _decimal(getattr(args, 'price_pareto_min', '1'), 'PRICE_PARETO_MIN')
            if not alpha > 0 or not math.isfinite(float(alpha)) or float(alpha) == 0:
                raise KCError('PRICE_PARETO_ALPHA must be finite and positive')
            if minimum <= 0 or not math.isfinite(float(minimum)) or minimum * 10**decimals != (minimum * 10**decimals).to_integral_value():
                raise KCError('PRICE_PARETO_MIN must be a positive PRICE_DECIMALS grid point')
            params.update(distribution='pareto-type-I', alpha=_text(alpha), minimum=_text(minimum),
                          rounding='half-up', upper=None)
    elif mode in ('uniform', 'legacy-unit'):
        seed = None
        params = dict(unit_price='1' if mode == 'uniform' else _text(unit))
    elif mode == 'legacy-random':
        seed = str(seed)
        params = dict(lower=1, upper=10, endpoints='closed', sampling='legacy-sha256-mod10')
    return dict(mode=mode, seed=seed, decimals=decimals, parameters=params)


def environment_settings():
    parser = argparse.ArgumentParser(add_help=False)
    add_price_arguments(parser)
    return batch_settings(parser.parse_args([]))


def mode_args(args, mode):
    result = copy.copy(args)
    result.price_mode = mode
    if getattr(result, 'price_seed', None) is None:
        result.price_seed = '2026'
    # CSV/custom fixed prices are explicit alternatives, independent of seed.
    if mode is None:
        result.price_seed = None
    return result


def selected_modes(args):
    mode = getattr(args, 'price_mode', None)
    if mode is None and (getattr(args, 'prices_csv', None) or Decimal(str(getattr(args, 'unit_price', '1'))) != 1):
        return (None,)
    return MODES if mode in (None, 'all') else (mode,)


def batch_settings(args):
    modes = selected_modes(args)
    configurations = [settings(mode_args(args, mode)) for mode in modes]
    return dict(mode='all' if len(modes) > 1 else configurations[0]['mode'],
                seed=str(getattr(args, 'price_seed', None) or '2026'),
                configurations=configurations)


def _canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _draw(key, stream, bound):
    """Uniform integer [0,bound), using rejection to avoid modulo bias."""
    limit = 2**256 - 2**256 % bound
    counter = 0
    while True:
        h = hashlib.sha256(key + b'\0' + stream.encode('ascii') + b'\0' + str(counter).encode('ascii')).digest()
        n = int.from_bytes(h, 'big')
        if n < limit:
            return n % bound
        counter += 1


def generate_prices(catalog, args, adapter):
    cfg = settings(args)
    mode, seed, params = cfg['mode'], cfg['seed'], cfg['parameters']
    policy = {}
    if mode in ('seller_csv', 'legacy-random', 'legacy-unit'):
        prices, policy = adapter.tuple_prices(catalog, args.prices_csv, args.unit_price,
                                              None if mode == 'seller_csv' else args.price_seed, args.schema)
    elif mode == 'uniform':
        prices = {token: '1' for token in catalog}
        policy = dict(price_policy='uniform_configured', unit_price='1')
    else:
        prices = {}
        scale = 10**cfg['decimals']
        if mode == 'skewed':
            cutoff = int(Decimal(params['cutoff']) * scale)
            probability = int(Decimal(params['low_probability']) * 1000000)
        if mode == 'heavy-tailed':
            alpha, minimum = float(params['alpha']), float(params['minimum'])
        for token, fact in catalog.items():
            # UUID, query number, data size and iteration order never enter this key.
            key = _canonical([VERSION, seed, str(fact['relation']), str(fact['tuple_id'])]).encode('utf-8')
            if mode == 'random':
                amount = Decimal(1 + _draw(key, 'value', 10*scale - 1)).scaleb(-cfg['decimals'])
            elif mode == 'skewed':
                low = _draw(key, 'mixture', 1000000) < probability
                start, count = (1, cutoff-1) if low else (cutoff, 10*scale-cutoff)
                amount = Decimal(start + _draw(key, 'value', count)).scaleb(-cfg['decimals'])
            else:
                # Strictly between 0 and 1 even after binary64 conversion.
                u = (1 + _draw(key, 'pareto', 2**53)) / (2**53 + 2)
                try:
                    value = minimum * u**(-1.0/alpha)
                    if not math.isfinite(value):
                        raise OverflowError()
                    with localcontext() as ctx:
                        ctx.prec = 40
                        amount = Decimal(str(value)).quantize(Decimal(1).scaleb(-cfg['decimals']), rounding=ROUND_HALF_UP)
                except (OverflowError, InvalidOperation) as exc:
                    raise KCError('Pareto price exceeds supported numeric range; change its parameters') from exc
            prices[token] = _text(amount)
        policy = dict(price_policy=VERSION + ':' + mode)
    config_record = dict(version=VERSION, **cfg)
    if policy.get('price_file_sha256'):
        config_record['price_file_sha256'] = policy['price_file_sha256']
    policy.update(price_mode=mode, price_seed=seed, price_decimals=cfg['decimals'],
                  price_parameters=_canonical(params),
                  price_config_sha256=hashlib.sha256(_canonical(config_record).encode('utf-8')).hexdigest(),
                  price_generation_config=config_record)
    return prices, policy


class RepricedSnapshots:
    """Load the shared DAG/catalog lazily, then only change tuple weights."""
    def __init__(self, snapshot, catalog, metadata):
        self.snapshot, self.catalog = Path(snapshot), catalog
        self.metadata = dict(metadata)
        self.data = self.facts = self.tokens = None

    def write(self, out, args, adapter):
        if self.data is None:
            self.data = json.loads(self.snapshot.read_text(encoding='utf-8'))
            if self.catalog is None:
                raise KCError('Price mode comparison requires the exported tuple catalog')
            with Path(self.catalog).open(newline='', encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            self.facts = {row['provenance']: row for row in rows}
            self.tokens = tuple(self.data['prices'])
            if len(self.facts) != len(rows) or set(self.facts) != set(self.tokens):
                raise KCError('Shared tuple catalog does not match the provenance snapshot')
        out = Path(out)
        out.mkdir(parents=True, exist_ok=False)
        info = dict(self.metadata)
        for key in ('unit_price','price_policy','price_file_sha256','price_generation_config',
                    'shared_price_generation_seconds',*PRICE_FIELDS):
            info.pop(key,None)
        info.update(requested_price_mode=args.price_mode,price_mode=args.price_mode)
        try:
            started = time.perf_counter()
            raw, policy = generate_prices(self.facts, args, adapter)
            prices = Prices(self.tokens, raw)
            info.update(policy, shared_price_generation_seconds=time.perf_counter()-started)
            write_json(out/'price_policy.json', policy)
            write_json(out/'provenance.json', dict(self.data, prices=prices.text))
            with (out/'tuple_catalog.csv').open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['provenance','relation','tuple_id','price','mapping'])
                for token in self.tokens:
                    fact = self.facts[token]
                    writer.writerow([token,fact['relation'],fact['tuple_id'],prices.text[token],fact.get('mapping','')])
            info.update(snapshot_sha256=sha256_file(out/'provenance.json'),
                        tuple_catalog_sha256=sha256_file(out/'tuple_catalog.csv'))
        except Exception as exc:
            info.update(price_generation_status='ERROR', error=str(exc))
            raise
        finally:
            write_json(out/'export_metadata.json', info)
        return out/'provenance.json', out/'tuple_catalog.csv', info
