#!/usr/bin/env python3
"""Runtime reports for separated ForBackBench pricing methods (stdlib only).

Primary timing = parent-observed worker_wall_seconds, including interpreter
startup, imports, loading, algorithm and worker output. Successful-only means
are accompanied by failures. Pairwise speedups use the SAME snapshot/run and
agreeing exact prices. No timeout is replaced by 0, 100, infinity, or a guess.

This module reads run records, not the algorithm implementation. Rebuilding a
report never imports a solver, accesses PostgreSQL, or reruns an experiment.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import statistics
import sys
from typing import Iterable, Optional

REPORT_VERSION = 'pricing-runtime-1.6'
PRICE_FIELDS = ('price_mode','price_seed','price_decimals','price_parameters','price_config_sha256')
PHASE_PROTOCOL = 'worker-wall-functional-v1'
FUNCTIONAL_STAGES = {'KCPrice': ('c2d_stage_seconds', 'price_stage_seconds'),
                     'SATPrice': ('reduction_stage_seconds', 'solver_stage_seconds')}
STAGE_FIELDS = tuple(field for pair in FUNCTIONAL_STAGES.values() for field in pair)
PHASES = ('input_load_seconds', 'cnf_seconds', 'reduction_seconds', 'solver_seconds',
          'why_provenance_seconds', 'min_support_price_seconds', 'compile_seconds',
          'nnf_read_seconds', 'price_seconds', 'verify_seconds', 'c2d_seconds', *STAGE_FIELDS)
SUMMARY_PHASES = ('c2d', 'price', 'reduction', 'solver')
CONTEXT = ('scenario','data_size','query','experiment_config_id','snapshot_sha256')
RUN_FIELDS = ['comparison_id','batch_id',*CONTEXT,'run','measurement_index','algorithm',
    'status','effective_status','price','support_verified','is_warmup','excluded_from_stats',
    'timeout_seconds','worker_wall_seconds','method_total_seconds',*PHASES,
    'shared_provenance_seconds','shared_mapping_seconds','shared_export_seconds',
    'shared_rewrite_seconds','time_sample_valid','price_policy','error','result_directory',
    'phase_partition_protocol','phase_boundary_perf_counter_ns',*PRICE_FIELDS]
STAT_FIELDS = ('n','mean_seconds','stddev_seconds','min_seconds','p25_seconds',
               'median_seconds','p75_seconds','p95_seconds','p99_seconds','max_seconds')
SUMMARY_FIELDS = [*CONTEXT,'algorithm','n_total','n_warmup','n_measured','n_exact','n_timeout',
    'n_resource_limit','n_error','n_interrupted','n_unsat','n_mismatch','n_other','success_rate',
    'n_time_samples',*STAT_FIELDS[1:],'n_method_time_samples','mean_method_seconds',
    'n_wall_samples_all','n_wall_samples_measured','wall_spent_seconds','measured_wall_spent_seconds','price_policy']
SUMMARY_FIELDS += list(PRICE_FIELDS)
SUMMARY_FIELDS += [field for phase in SUMMARY_PHASES
                   for field in ('n_' + phase + '_samples', 'mean_' + phase + '_seconds')]
SUMMARY_FIELDS += ['stage_partition_status','n_stage_time_samples',
                   *('mean_' + field for field in STAGE_FIELDS)]
PAIR_FIELDS = [*CONTEXT,'reference','target','n_matched','n_reference_only','n_target_only',
    'n_pairs','n_excluded_status','n_excluded_input','n_excluded_price','n_excluded_time',
    'reference_mean_seconds','target_mean_seconds','ratio_of_means',
    'geomean_speedup','median_speedup','time_reduction_fraction']
OVERALL_FIELDS = ['scenario','data_size','experiment_config_id','algorithm','n_queries',
    'n_query_instances','n_instances_with_exact_timing','n_measured','n_exact','n_timeout',
    'n_resource_limit','n_error','n_interrupted','n_unsat','n_mismatch','n_other',
    'success_rate','macro_mean_seconds','micro_mean_seconds','n_time_samples',
    'n_wall_samples_all','n_wall_samples_measured','wall_spent_seconds','measured_wall_spent_seconds']
OVERALL_PAIR_FIELDS = ['scenario','data_size','experiment_config_id','reference','target',
    'n_queries','n_query_instances','n_instances_with_pairs','n_pairs',
    'macro_geomean_speedup','pooled_ratio_of_means','n_excluded_status',
    'n_excluded_input','n_excluded_price','n_excluded_time']
SHARED_TIMES = ('shared_rewrite_seconds','tgd_seconds','gqr_seconds','shared_export_seconds',
    'shared_provenance_seconds','shared_mapping_seconds','shared_query_rewrite_seconds',
    'shared_query_execution_seconds','shared_provenance_build_seconds',
    'shared_provenance_setup_seconds','shared_fetch_seconds','shared_price_generation_seconds')
SHARED_FIELDS = ['comparison_id','scenario','data_size','query','batch_id','snapshot_sha256','rewrite_mode',
    *SHARED_TIMES,'preprocessing_status','shared_timing_protocol','shared_answer_query_executions',
    'query_execution_status','provenance_build_status','n_answers','n_tuples','n_provenance_gates','error',*PRICE_FIELDS]


def number(value):
    if value is None or isinstance(value,bool) or value == '': return None
    try: value=float(value)
    except (ValueError,TypeError,OverflowError): return None
    return value if math.isfinite(value) and value>=0 else None


def truth(value): return value is True or str(value).lower() in ('true','1')


def c2d_time(record):
    """Recover external c2d time from KCPrice metadata, including v5/v6 logs.

    compile_seconds can also time the optimized core's built-in unit compiler.
    A known skipped invocation is zero; an ambiguous old CSV is missing, not a
    guessed c2d measurement. An explicitly recorded c2d_seconds takes priority.
    """
    if record.get('algorithm') != 'KCPrice':return None
    if 'c2d_seconds' in record:return number(record['c2d_seconds'])
    invoked=str(record.get('compiler_invoked','')).strip().lower()
    compiler=record.get('compiler')
    if (invoked in ('false','0') or compiler in ('builtin-unit-cnf','not-needed-empty-answer') or
            record.get('compilation_cache')=='hit'):
        return 0.0
    if invoked in ('true','1') or (isinstance(compiler,str) and compiler.strip()):
        return number(record.get('compile_seconds'))
    return None


def functional_stage_pair(record):
    """Accept only two measured, adjacent stages covering this whole worker."""
    fields=FUNCTIONAL_STAGES.get(record.get('algorithm'))
    if not fields or record.get('phase_partition_protocol')!=PHASE_PROTOCOL:return None
    total=number(record.get('worker_wall_seconds'))
    values=tuple(number(record.get(field)) for field in fields)
    if total is None or any(v is None or v>total for v in values):return None
    if not math.isclose(sum(values),total,rel_tol=1e-12,abs_tol=1e-9):return None
    return values


def integer(value):
    try:
        n=int(value)
        return n if n>=0 and str(value).strip() in (str(n),str(float(n))) else None
    except (TypeError,ValueError,OverflowError): return None


def exact_price(value):
    if value is None or isinstance(value,bool): return None
    try: p=Decimal(str(value))
    except (InvalidOperation,ValueError): return None
    return p if p.is_finite() and p>=0 else None


def percentile(sorted_values,p):
    if not sorted_values: return None
    location=(len(sorted_values)-1)*p
    lo=int(math.floor(location));hi=int(math.ceil(location));fraction=location-lo
    return sorted_values[lo]*(1-fraction)+sorted_values[hi]*fraction


def describe(values):
    values=sorted(values)
    if not values: return dict(n=0,**{name:None for name in STAT_FIELDS[1:]})
    return dict(n=len(values),mean_seconds=statistics.mean(values),
        stddev_seconds=statistics.stdev(values) if len(values)>1 else None,
        min_seconds=values[0],p25_seconds=percentile(values,.25),
        median_seconds=percentile(values,.5),p75_seconds=percentile(values,.75),
        p95_seconds=percentile(values,.95),p99_seconds=percentile(values,.99),max_seconds=values[-1])


def _atomic(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp.'+str(os.getpid()))
    try:
        temp.write_text(text,encoding='utf-8');os.replace(str(temp),str(path))
    finally:
        if temp.exists():temp.unlink()


def json_file(path,value):
    _atomic(path,json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def csv_file(path,rows,fields):
    import io
    text=io.StringIO(newline='')
    writer=csv.DictWriter(text,fieldnames=fields,extrasaction='ignore')
    writer.writeheader();writer.writerows(rows)
    _atomic(path,text.getvalue())


def _read_json(path,default=None):
    if not path.is_file():return default
    return json.loads(path.read_text(encoding='utf-8'))


def _config_id(config,exclude_first):
    shared=config.get('shared') or {}
    # Do not pool timeout/price/solver/encoding/protocol changes invisibly.
    fields={name:config.get(name) for name in ('suite_version','methods','timeout','encoding',
        'solver_options','max_supports','max_candidates','warmups','machine','ordering')}
    fields['price_policy']={key:shared.get(key) for key in
        ('price_policy','unit_price','price_seed','price_file_sha256','db_mode')}
    if shared.get('price_config_sha256'):
        fields['price_config_sha256']=shared['price_config_sha256']
    fields['exclude_first']=exclude_first
    if config.get('phase_partition_protocol'):
        fields['phase_partition_protocol']=config['phase_partition_protocol']
    digest=hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
    return digest[:16]


def load_comparison(folder,exclude_first=0):
    """Prefer results.json, falling back to incremental comparison.csv.

    Return normalized COPY of records; never modify old measurement files.
    """
    folder=Path(folder).resolve()
    config=_read_json(folder/'comparison_config.json',{})
    shared=config.get('shared') or {}
    rows=_read_json(folder/'results.json')
    if rows is None:
        path=folder/'comparison.csv'
        if not path.is_file(): raise ValueError('No results.json or comparison.csv in '+str(folder))
        with path.open(newline='',encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
    if not isinstance(rows,list):raise ValueError('results.json must be a list: '+str(folder))
    warnings=[];normalized=[];seen=set()
    expected_hash=config.get('input_sha256')
    config_id=_config_id(config,exclude_first)
    for raw in rows:
        if not isinstance(raw,dict):raise ValueError('Invalid result record in '+str(folder))
        r=dict(raw)
        run=integer(r.get('run'));method=r.get('algorithm')
        if run is None or run<1 or not isinstance(method,str) or not method:
            raise ValueError('Invalid method/run identifier in '+str(folder))
        if (run,method) in seen:raise ValueError('Duplicate method/run would double-count: '+str(folder))
        seen.add((run,method))
        for key in ('scenario','data_size','query','batch_id','price_policy','shared_provenance_seconds',
                    'shared_mapping_seconds','shared_export_seconds','shared_rewrite_seconds',*PRICE_FIELDS):
            if key not in r or r[key] in ('',None):r[key]=shared.get(key)
        r['scenario']=r.get('scenario') or '(offline)'
        r['data_size']=r.get('data_size') or '(unspecified)'
        r['query']=r.get('query') or config.get('input_name') or folder.parent.name
        r['batch_id']=r.get('batch_id') or folder.parent.name
        r['comparison_id']=str(folder);r['experiment_config_id']=config_id;r['run']=run
        r['timeout_seconds']=number(r.get('timeout_seconds',config.get('timeout')))
        r['snapshot_sha256']=r.get('snapshot_sha256') or expected_hash or ''
        status=str(r.get('status','UNKNOWN')).upper();r['status']=status;r['effective_status']=status
        if expected_hash and r['snapshot_sha256']!=expected_hash:
            r['effective_status']='INPUT_MISMATCH'
        if status=='EXACT' and (exact_price(r.get('price')) is None or not truth(r.get('support_verified'))):
            r['effective_status']='INVALID_RESULT'
        r['is_warmup']=truth(r.get('is_warmup')) or run <= int(config.get('warmups') or 0)
        r['excluded_from_stats']=r['is_warmup'] or run<=exclude_first
        r['measurement_index']=integer(r.get('measurement_index'))
        r['support_verified']=truth(r.get('support_verified'))
        r['c2d_seconds']=c2d_time(r)
        for name in ('worker_wall_seconds','method_total_seconds',*PHASES,
                     'shared_provenance_seconds','shared_mapping_seconds','shared_export_seconds','shared_rewrite_seconds'):
            r[name]=number(r.get(name))
        r['time_sample_valid']=r['worker_wall_seconds'] is not None
        if status=='EXACT' and not r['time_sample_valid']:
            warnings.append('EXACT row has no valid parent wall time: %s / %s run %s'%(folder,method,run))
        normalized.append(r)
    # For one immutable snapshot, optimal prices should agree across all methods
    # AND repeated runs (including warmups). Never rank a faster wrong answer.
    prices={exact_price(r['price']) for r in normalized if r['effective_status']=='EXACT'}
    sat_hashes={r['raw_instance_sha256'] for r in normalized
                if r['algorithm'].startswith('SATPrice') and r.get('raw_instance_sha256')}
    consistency=_read_json(folder/'consistency.json',{})
    mismatch=(len(prices)>1 or (prices and any(r['status']=='UNSAT' for r in normalized)) or
              len(sat_hashes)>1 or consistency.get('status')=='MISMATCH')
    if mismatch:
        warnings.append('Consistency mismatch; exact rows excluded from runtime ranking: '+str(folder))
        for r in normalized:
            if r['effective_status']=='EXACT':r['effective_status']='MISMATCH'
    expected_methods=config.get('methods') or []
    planned_runs=integer(config.get('runs'))
    planned_warmups=integer(config.get('warmups')) or 0
    if expected_methods and planned_runs is not None and len(rows)<len(expected_methods)*(planned_runs+planned_warmups):
        warnings.append('Partial comparison: some scheduled rows never started; success rates use recorded measured attempts only: '+str(folder))
    return normalized,dict(comparison_id=str(folder),config=config,
        expected_methods=config.get('methods',[]),reported_rows=len(rows),warnings=warnings,
        shared_costs={k:number(shared.get(k)) for k in ('shared_export_seconds','shared_provenance_seconds',
                     'shared_mapping_seconds','shared_rewrite_seconds')})


def _context(r):return tuple(r[k] for k in CONTEXT)


def _eligible(r):return r['effective_status']=='EXACT' and not r['excluded_from_stats']


def _status_counts(rows):
    counts=Counter(r['effective_status'] for r in rows)
    answer=dict(n_exact=counts['EXACT'],n_timeout=counts['TIMEOUT'],n_resource_limit=counts['RESOURCE_LIMIT'],
        n_interrupted=counts['INTERRUPTED'],n_unsat=counts['UNSAT'],
        n_mismatch=counts['MISMATCH']+counts['INPUT_MISMATCH'],
        n_error=counts['ERROR']+counts['INVALID_RESULT'])
    answer['n_other']=len(rows)-sum(answer.values())
    return answer


def summarize(rows):
    groups=defaultdict(list);summaries=[];phases=[]
    for r in rows:groups[(*_context(r),r['algorithm'])].append(r)
    for key,items in sorted(groups.items(),key=lambda x:str(x[0])):
        common=dict(zip((*CONTEXT,'algorithm'),key))
        measured=[r for r in items if not r['excluded_from_stats']]
        good=[r for r in measured if _eligible(r)]
        wall=[r['worker_wall_seconds'] for r in good if r['worker_wall_seconds'] is not None]
        own=[r['method_total_seconds'] for r in good if r['method_total_seconds'] is not None]
        stats=describe(wall)
        row=dict(common,n_total=len(items),n_warmup=len(items)-len(measured),n_measured=len(measured),
            **_status_counts(measured),n_time_samples=stats.pop('n'),**stats,
            n_method_time_samples=len(own),mean_method_seconds=statistics.mean(own) if own else None,
            n_wall_samples_all=sum(r['worker_wall_seconds'] is not None for r in items),
            n_wall_samples_measured=sum(r['worker_wall_seconds'] is not None for r in measured),
            wall_spent_seconds=sum(r['worker_wall_seconds'] for r in items if r['worker_wall_seconds'] is not None)
                               if any(r['worker_wall_seconds'] is not None for r in items) else None,
            measured_wall_spent_seconds=sum(r['worker_wall_seconds'] for r in measured if r['worker_wall_seconds'] is not None)
                               if any(r['worker_wall_seconds'] is not None for r in measured) else None,
            price_policy=items[0].get('price_policy'))
        row.update({field:items[0].get(field) for field in PRICE_FIELDS})
        row['success_rate']=row['n_exact']/len(measured) if measured else None
        # Preserve the existing whole-worker / method means. Phase means use
        # the same successful, non-warmup population, each with its own count.
        for phase in SUMMARY_PHASES:
            values=[r[phase + '_seconds'] for r in good if r[phase + '_seconds'] is not None]
            row['n_' + phase + '_samples']=len(values)
            row['mean_' + phase + '_seconds']=statistics.mean(values) if values else None
        row.update({'mean_' + field:None for field in STAGE_FIELDS})
        stage_fields=FUNCTIONAL_STAGES.get(common['algorithm'])
        pairs=[pair for r in good if (pair:=functional_stage_pair(r)) is not None]
        row['n_stage_time_samples']=len(pairs)
        row['stage_partition_status']=('NOT_APPLICABLE' if not stage_fields else
            'COMPLETE' if pairs and len(pairs)==len(wall) else 'PARTIAL' if pairs else 'MISSING')
        if row['stage_partition_status']=='COMPLETE':
            # Exactly the SAME rows as mean_seconds. Historical rows without a
            # boundary never silently become a different phase-mean population.
            first=statistics.mean(pair[0] for pair in pairs)
            row['mean_' + stage_fields[0]]=first
            row['mean_' + stage_fields[1]]=row['mean_seconds']-first
        summaries.append(row)
        for phase in PHASES:
            values=[r[phase] for r in good if r[phase] is not None]
            phases.append(dict(common,phase=phase,n_exact=len(good),**describe(values)))
    return summaries,phases


def pairwise(rows):
    groups=defaultdict(list)
    for r in rows:
        if not r['excluded_from_stats']:groups[_context(r)].append(r)
    output=[]
    for key,items in sorted(groups.items(),key=lambda x:str(x[0])):
        methods=sorted({r['algorithm'] for r in items})
        by_method={m:{(r['comparison_id'],r['run']):r for r in items if r['algorithm']==m} for m in methods}
        for reference,target in itertools.permutations(methods,2):
            left,right=by_method[reference],by_method[target]
            matched=sorted(left.keys() & right.keys());ta=[];tb=[];logs=[];ratios=[]
            row=dict(zip(CONTEXT,key));row.update(reference=reference,target=target,n_matched=len(matched),
                n_reference_only=len(left.keys()-right.keys()),n_target_only=len(right.keys()-left.keys()),
                n_pairs=0,n_excluded_status=0,n_excluded_input=0,n_excluded_price=0,n_excluded_time=0)
            for k in matched:
                a,b=left[k],right[k]
                if not (_eligible(a) and _eligible(b)):
                    row['n_excluded_status']+=1;continue
                if (not a['snapshot_sha256'] or a['snapshot_sha256']!=b['snapshot_sha256'] or
                    a['timeout_seconds']!=b['timeout_seconds'] or
                    (reference.startswith('SATPrice') and target.startswith('SATPrice') and
                     a.get('raw_instance_sha256') and b.get('raw_instance_sha256') and
                     a['raw_instance_sha256']!=b['raw_instance_sha256'])):
                    row['n_excluded_input']+=1;continue
                if exact_price(a['price'])!=exact_price(b['price']):
                    row['n_excluded_price']+=1;continue
                t1,t2=a['worker_wall_seconds'],b['worker_wall_seconds']
                if t1 is None or t2 is None or t1<=0 or t2<=0:
                    row['n_excluded_time']+=1;continue
                ratio=t1/t2
                if not math.isfinite(ratio):row['n_excluded_time']+=1;continue
                ta.append(t1);tb.append(t2);ratios.append(ratio);logs.append(math.log(t1)-math.log(t2))
            row.update(n_pairs=len(ta),reference_mean_seconds=statistics.mean(ta) if ta else None,
                target_mean_seconds=statistics.mean(tb) if tb else None,
                ratio_of_means=sum(ta)/sum(tb) if ta else None,
                geomean_speedup=math.exp(statistics.mean(logs)) if logs else None,
                median_speedup=statistics.median(ratios) if ratios else None,
                time_reduction_fraction=1-sum(tb)/sum(ta) if ta else None)
            output.append(row)
    return output


def overall(summaries,pairs):
    grouped=defaultdict(list);methods=[];speedups=[]
    prefix=('scenario','data_size','experiment_config_id')
    for r in summaries:grouped[tuple(r[k] for k in (*prefix,'algorithm'))].append(r)
    for key,items in sorted(grouped.items(),key=lambda x:str(x[0])):
        good=[r for r in items if r['mean_seconds'] is not None]
        n=sum(r['n_time_samples'] for r in good)
        row=dict(zip((*prefix,'algorithm'),key));row.update(n_queries=len({r['query'] for r in items}),
            n_query_instances=len(items),n_instances_with_exact_timing=len(good),n_time_samples=n,
            macro_mean_seconds=statistics.mean(r['mean_seconds'] for r in good) if good else None,
            micro_mean_seconds=sum(r['mean_seconds']*r['n_time_samples'] for r in good)/n if n else None)
        for k in ('n_measured','n_exact','n_timeout','n_resource_limit','n_error','n_interrupted','n_unsat',
                  'n_mismatch','n_other','n_wall_samples_all','n_wall_samples_measured'):
            row[k]=sum(r[k] for r in items)
        for k in ('wall_spent_seconds','measured_wall_spent_seconds'):
            values=[r[k] for r in items if r[k] is not None]
            row[k]=sum(values) if values else None
        row['success_rate']=row['n_exact']/row['n_measured'] if row['n_measured'] else None
        methods.append(row)
    grouped=defaultdict(list)
    for r in pairs:grouped[tuple(r[k] for k in (*prefix,'reference','target'))].append(r)
    for key,items in sorted(grouped.items(),key=lambda x:str(x[0])):
        good=[r for r in items if r['n_pairs']]
        row=dict(zip((*prefix,'reference','target'),key));row.update(n_queries=len({r['query'] for r in items}),
            n_query_instances=len(items),n_instances_with_pairs=len(good),n_pairs=sum(r['n_pairs'] for r in good),
            macro_geomean_speedup=math.exp(statistics.mean(math.log(r['geomean_speedup']) for r in good)) if good else None,
            pooled_ratio_of_means=(sum(r['reference_mean_seconds']*r['n_pairs'] for r in good)/
                                  sum(r['target_mean_seconds']*r['n_pairs'] for r in good)) if good else None)
        for k in ('n_excluded_status','n_excluded_input','n_excluded_price','n_excluded_time'):row[k]=sum(r[k] for r in items)
        speedups.append(row)
    return methods,speedups


def fmt(v,digits=6):return '--' if v is None else format(v,'.%df'%digits)


def render_text(summaries,pairs,reference='Baseline',shared_rows=None):
    lines=['Runtime comparison [seconds; EXACT only; mean/std are parent worker wall time]',
           'N=usable timing samples; warmups excluded; speedup>1 means target faster.']
    groups=defaultdict(list)
    for r in summaries:groups[tuple(r[k] for k in CONTEXT)].append(r)
    for key,items in sorted(groups.items(),key=lambda x:str(x[0])):
        lines.append('\n%s / %s / %s [config=%s snapshot=%s]'%(key[0],key[1],key[2],key[3],str(key[4])[:10]))
        price_info=items[0]
        if price_info.get('price_mode'):
            lines.append('Tuple prices: mode=%s; seed=%s; parameters=%s; config=%s'%(
                price_info['price_mode'],price_info.get('price_seed'),price_info.get('price_parameters'),
                price_info.get('price_config_sha256')))
        header='%-23s %9s %6s %11s %11s %11s %11s %9s %9s'%('Method','Exact/Run','N','Mean','Stddev','Median','P95','Timeout','Errors')
        lines.extend([header,'-'*len(header)])
        for r in sorted(items,key=lambda r:r['algorithm']):
            errors=r['n_error']+r['n_resource_limit']+r['n_interrupted']+r['n_mismatch']+r['n_other']
            lines.append('%-23s %9s %6s %11s %11s %11s %11s %9s %9s'%(
                r['algorithm'],'%d/%d'%(r['n_exact'],r['n_measured']),r['n_time_samples'],fmt(r['mean_seconds']),
                fmt(r['stddev_seconds']),fmt(r['median_seconds']),fmt(r['p95_seconds']),r['n_timeout'],errors))
            if r.get('stage_partition_status')=='COMPLETE':
                first,second=FUNCTIONAL_STAGES[r['algorithm']]
                total_text=fmt(r['mean_seconds']);first_text=fmt(r['mean_'+first])
                # Balance the final displayed decimal; CSV/JSON retain precision.
                second_text=fmt(Decimal(total_text)-Decimal(first_text))
                lines.append('  Whole worker: %s=%s + %s=%s = total %s s'%(first,
                    first_text,second,second_text,total_text))
        chosen=[p for p in pairs if tuple(p[k] for k in CONTEXT)==key and
                (p['reference']==reference or (p['reference']=='SATPrice-NoReduction' and p['target']=='SATPrice'))]
        for p in chosen:
            lines.append('  %s -> %s: paired=%d, mean-ratio=%sx, geometric=%sx'%(p['reference'],p['target'],
                         p['n_pairs'],fmt(p['ratio_of_means'],3),fmt(p['geomean_speedup'],3)))
    if not groups:lines.append('No pricing run records available; see pipeline_failures.csv/report warnings.')
    if shared_rows:
        lines.extend(['','Shared preprocessing [seconds; once per query/batch; shared by all methods and repeats]',
            'Rewrite=GQR query rewriting; Answers=query execution and fetching answer roots; '
            'Provenance=constructing the pricing DAG. -- means unavailable.',
            'Reused/provided SQL has zero rewriting time. Error rows may contain partial elapsed times.'])
        shared_groups=defaultdict(list)
        for r in shared_rows:
            shared_groups[(r.get('scenario') or '(offline)',r.get('data_size') or '(unspecified)')].append(r)
        for (scenario,size),items in sorted(shared_groups.items()):
            lines.append('\n%s / %s'%(scenario,size))
            layout='%-8s %-28s %12s %12s %14s  %s'
            header=layout%('Query','Batch','Rewrite(s)','Answers(s)','Provenance(s)','Overall/Query/Provenance status')
            lines.extend([header,'-'*len(header)])
            for r in items:
                status='/'.join(str(r.get(k) or '--') for k in
                                ('preprocessing_status','query_execution_status','provenance_build_status'))
                lines.append(layout%(r.get('query') or '--',r.get('batch_id') or '--',
                    fmt(number(r.get('shared_query_rewrite_seconds'))),
                    fmt(number(r.get('shared_query_execution_seconds'))),
                    fmt(number(r.get('shared_provenance_build_seconds'))),status))
    return '\n'.join(lines)+'\n'


def discover(root,selection='latest'):
    """Find each comparison once; never scan individual result.json as batches."""
    root=Path(root).resolve()
    if root.is_file():root=root.parent
    if (root/'comparison_config.json').is_file():return [root]
    folders=sorted({p.parent for p in root.rglob('comparison_config.json')
                    if (p.parent/'results.json').exists() or (p.parent/'comparison.csv').exists()})
    if selection=='all':return folders
    latest={}
    for folder in folders:
        config=_read_json(folder/'comparison_config.json',{});shared=config.get('shared') or {}
        # Unlabelled offline runs are independent; never pretend their names
        # establish that they are the same query.
        key=(shared.get('scenario'),shared.get('data_size'),shared.get('query'))
        if not all(key):key=('offline',str(folder),None)
        if shared.get('price_config_sha256'):
            key=(*key,shared['price_config_sha256'])
        rank=((folder/'comparison_config.json').stat().st_mtime_ns,str(folder))
        if key not in latest or rank>latest[key][0]:latest[key]=(rank,folder)
    return sorted(item[1] for item in latest.values())


def shared_report_rows(manifests,failures):
    """One shared record per query batch, regardless of repeats or failures."""
    records={}
    for manifest in manifests:
        cfg=manifest['config'];shared=dict(cfg.get('shared') or {})
        shared.setdefault('snapshot_sha256',cfg.get('input_sha256'))
        folder=Path(manifest['comparison_id'])
        records[str(folder.resolve())]=(str(folder),shared)
    for failure in failures:
        shared=failure.get('shared')
        if isinstance(shared,dict):
            batch=Path(failure['source_file']).parent.resolve()
            records.setdefault(str(batch/'comparison'),('',shared))
    # All price modes of a query reuse the very same DB export. Keep its
    # preprocessing once, while summing the distinct price-generation steps.
    groups=defaultdict(list)
    for key,(comparison_id,shared) in records.items():
        groups[shared.get('shared_preprocessing_id') or key].append((comparison_id,shared))
    rows=[]
    for _,items in sorted(groups.items()):
        comparison_id,shared=items[0]
        row={key:shared.get(key) for key in SHARED_FIELDS}
        row.update(comparison_id=comparison_id,preprocessing_status=shared.get('status'))
        for key in SHARED_TIMES:row[key]=number(shared.get(key))
        if shared.get('shared_preprocessing_id'):
            row['comparison_id']=shared['shared_preprocessing_id']
            row['snapshot_sha256']=shared.get('shared_snapshot_sha256',row.get('snapshot_sha256'))
            modes=shared.get('shared_price_modes') or []
            if len(modes)>1:
                row.update(price_mode='all',price_seed=shared.get('shared_price_seed'),
                           price_parameters=None,price_config_sha256=None,price_decimals=None)
            generation={}
            for _,part in items:
                value=number(part.get('shared_price_generation_seconds'))
                if part.get('price_generation_status')!='ERROR' and value is not None:
                    generation[part.get('price_config_sha256') or part.get('price_mode')]=value
            row['shared_price_generation_seconds']=sum(generation.values()) if generation else None
        # Historical cached/provided SQL did not execute the rewriter.
        if row['shared_query_rewrite_seconds'] is None:
            if shared.get('rewrite_mode') in ('explicitly_reused_sql','provided_sql'):
                row['shared_query_rewrite_seconds']=0.0
            else:row['shared_query_rewrite_seconds']=number(shared.get('gqr_seconds'))
        rows.append(row)
    return rows


def write_reports(comparisons,out,reference='Baseline',exclude_first=0,pipeline_failures=None,metadata=None):
    if exclude_first<0:raise ValueError('exclude_first must be nonnegative')
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    rows=[];manifests=[];warnings=[]
    for folder in sorted({Path(p).resolve() for p in comparisons}):
        part,manifest=load_comparison(folder,exclude_first)
        rows.extend(part);manifests.append(manifest);warnings.extend(manifest['warnings'])
    summaries,phases=summarize(rows);pairs=pairwise(rows);overall_rows,overall_pairs=overall(summaries,pairs)
    failures=[]
    for path in pipeline_failures or []:
        path=Path(path)
        value=_read_json(path)
        if value:failures.append(dict(value,source_file=str(path.resolve())))
    report=dict(report_version=REPORT_VERSION,unit='seconds',primary_metric='worker_wall_seconds',
        success_rule='EXACT + support_verified + finite nonnegative price + no batch price/input inconsistency',
        mean_population='successful measured rows only; failures are counted separately',
        speedup_rule='reference/target on matching comparison ID, snapshot, run, timeout and exact price',
        quantiles='linear interpolation at (n-1)*p',stddev='sample standard deviation; null when n<2',
        missing='JSON null / blank CSV; never zero-filled',
        shared_cost_rule='export/rewrite recorded once per comparison, not added to every repetition or called end-to-end',
        shared_preprocessing_rule='one record per query/batch; query execution includes fetching all answer roots '
            'and inline ProvSQL token/gate creation; provenance build extracts and constructs the pricing DAG '
            'after that cursor closes; query rewrite is the GQR invocation, zero for reused/provided SQL',
        phase_summary_rule='successful measured rows only; each phase has its own valid-sample count; '
                           'phase times are included in total times and need not sum to the total',
        phase_summary_columns={
            'mean_c2d_seconds':'KCPrice external c2d invocation; zero when explicitly skipped; '
                               'excludes built-in compilation, NNF reading and price evaluation',
            'mean_price_seconds':'KCPrice recorded price_seconds: circuit price evaluation and support selection',
            'mean_reduction_seconds':'recorded reduction_seconds: SATPrice pruning; no-reduction may record zero',
            'mean_solver_seconds':'recorded solver_seconds: solver backend call including backend setup'},
        functional_stage_rule='adjacent measured intervals covering worker_wall_seconds; startup/input/preparation '
            'in stage 1, verification/output/worker shutdown in stage 2; means emitted only when every '
            'successful wall-time sample has a valid partition; phase 1 mean + phase 2 mean = mean_seconds',
        functional_stage_boundaries={'KCPrice':'immediately before PriceEvaluator construction, after circuit loading',
                                     'SATPrice':'immediately before the solver backend call, after preprocessing'},
        overall_rule='macro means weight each query/snapshot/config instance equally; micro means weight each successful repetition',
        selection_metadata=metadata or {},exclude_first=exclude_first,reference=reference,
        comparison_count=len(manifests),run_record_count=len(rows),comparisons=manifests,
        warnings=warnings,pipeline_failure_count=len(failures),summary=summaries,speedups=pairs,
        overall=overall_rows,overall_speedups=overall_pairs)
    shared_rows=shared_report_rows(manifests,failures)
    report['shared_preprocessing']=shared_rows
    csv_file(out/'runtime_shared.csv',shared_rows,SHARED_FIELDS)
    csv_file(out/'runtime_runs.csv',rows,RUN_FIELDS)
    csv_file(out/'runtime_summary.csv',summaries,SUMMARY_FIELDS)
    csv_file(out/'runtime_speedups.csv',pairs,PAIR_FIELDS)
    csv_file(out/'runtime_phases.csv',phases,[*CONTEXT,'algorithm','phase','n_exact',*STAT_FIELDS])
    csv_file(out/'runtime_overall.csv',overall_rows,OVERALL_FIELDS)
    csv_file(out/'runtime_overall_speedups.csv',overall_pairs,OVERALL_PAIR_FIELDS)
    csv_file(out/'pipeline_failures.csv',failures,['query','status','price','error','source_file'])
    json_file(out/'runtime_statistics.json',report)
    text=render_text(summaries,pairs,reference,shared_rows)
    _atomic(out/'runtime_report.txt',text)
    return report,text


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True,help='One comparison directory or an experiment tree')
    parser.add_argument('--out',type=Path,required=True,help='Report output directory; raw results are never modified')
    parser.add_argument('--selection',choices=['latest','all'],default='latest',
                        help='latest chooses the most recently modified comparison_config for each labelled query')
    parser.add_argument('--reference',default='Baseline')
    parser.add_argument('--exclude-first',type=int,default=0,help='Exclude raw run indices <= N; native warmup flags also excluded')
    args=parser.parse_args()
    try:
        if not args.root.exists():raise ValueError('Result path does not exist: '+str(args.root))
        folders=discover(args.root,args.selection)
        if not folders:raise ValueError('No saved comparison results under '+str(args.root))
        failures=list(args.root.rglob('pipeline_error.json')) if args.root.is_dir() else []
        report,text=write_reports(folders,args.out,args.reference,args.exclude_first,failures,
            {'source':str(args.root.resolve()),'selection':args.selection,
             'pipeline_errors_scope':'all pipeline_error.json under root, not only latest selected comparisons'})
        print(text,end='');print('Reports:',args.out.resolve())
        for message in report['warnings']:print('WARNING:',message,file=sys.stderr)
        return 0
    except (OSError,ValueError,TypeError,KeyError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
