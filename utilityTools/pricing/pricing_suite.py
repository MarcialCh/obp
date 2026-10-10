#!/usr/bin/env python3
"""Compare SATPrice, its no-reduction ablation, and minimal-why Baseline.

All methods receive the same immutable provenance/price JSON snapshot.
Public CLI launches each run in a separate process with a hard wall-time limit;
RC2 native code and baseline enumeration cannot leave a background run behind.
"""
from __future__ import annotations
import argparse
import csv
from decimal import Decimal
import hashlib
import json
import os
import platform
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Optional

# Direct-file and package-mode execution both resolve sibling packages.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pricing.common import KCError, Deadline, DeadlineExceeded, Prices, ProvenanceGraph, write_json, sha256_file
from satprice.satprice import run_satprice, solve_rc2, RC2_OPTIONS
from baseline.why_baseline import SupportLimitExceeded, run_baseline
from baseline.satprice_no_reduction import run_no_reduction
from pricing.legacy_bridge import load_kcprice_core, translate_legacy_errors
from pricing.runtime_stats import write_reports, FUNCTIONAL_STAGES, STAGE_FIELDS, PHASE_PROTOCOL

SUITE_VERSION='pricing-suite-3.1-functional-stages'
DEFAULT_METHODS='SATPrice,SATPrice-NoReduction,Baseline'
ALIASES={'satprice':'SATPrice','satprice-noreduction':'SATPrice-NoReduction',
         'satprice-noprune':'SATPrice-NoReduction','ilp':'SATPrice-NoReduction',
         'baseline':'Baseline','kcprice':'KCPrice'}
SUMMARY_FIELDS=['scenario','data_size','query','batch_id','comparison_id','run','measurement_index',
    'is_warmup','execution_position','algorithm','status','price','support_verified','selected_count','n_answers','n_tuples',
    'snapshot_sha256','raw_instance_sha256','n_cnf_vars','n_cnf_clauses','n_hard_after',
    'indispensable_tuples','strong_dominated_tuples','binary_weak_substitutions',
    'signed_guard_rejections','objective_offset','n_minimal_supports',
    'input_load_seconds','cnf_seconds','reduction_seconds','solver_seconds','why_provenance_seconds',
    'min_support_price_seconds','compile_seconds','nnf_read_seconds','price_seconds','verify_seconds',
    'method_total_seconds','worker_wall_seconds','shared_provenance_seconds','shared_mapping_seconds',
    'shared_export_seconds','shared_rewrite_seconds','timeout_seconds','price_policy','error','result_directory',
    'price_mode','price_seed','price_decimals','price_parameters','price_config_sha256',
    # Retain enough metadata to distinguish real c2d time in CSV-only reports.
    'compiler','compiler_invoked','compilation_cache',
    'phase_partition_protocol','phase_boundary_perf_counter_ns',*STAGE_FIELDS]


def parse_methods(spec):
    result=[]
    for name in spec.split(','):
        key=name.strip().lower()
        if key not in ALIASES:raise KCError('Unknown method %r; choose %s or KCPrice'%(name,DEFAULT_METHODS))
        if ALIASES[key] not in result:result.append(ALIASES[key])
    if not result:raise KCError('At least one method must be selected')
    return result


def preflight(methods,c2d='c2d',smoke=False,kcprice_dir=None):
    report={'suite_version':SUITE_VERSION,'python':sys.executable,'methods':methods}
    if any(m.startswith('SATPrice') for m in methods):
        try:
            import pysat
            from pysat.examples.rc2 import RC2
            from pysat.formula import WCNF
        except ImportError as exc:
            raise KCError('PySAT RC2 unavailable in %s: %s. Run this interpreter with '
                          '-m pip install -r utilityTools/pricing/requirements.txt'%(sys.executable,exc)) from exc
        report.update(maxsat_backend='PySAT-RC2',python_sat_version=pysat.__version__,solver_options=dict(RC2_OPTIONS))
        if smoke:
            model,cost,_=solve_rc2([(1,2)],{1:3,2:2},2)
            if model is None or cost!=2 or not model.get(2):raise KCError('RC2 backend self-test failed')
            report['rc2_smoke_test']='PASS'
    if 'KCPrice' in methods:
        core=load_kcprice_core(kcprice_dir)
        with translate_legacy_errors(core):
            report['c2d_executable']=core.executable(c2d)
        report['kcprice_core_file']=str(Path(core.__file__).resolve())
    return report


def load_input(path):
    # Parse JSON decimal numbers without first rounding through binary floats.
    data=json.loads(Path(path).read_text(encoding='utf-8'),parse_float=Decimal)
    if not isinstance(data,dict) or not {'nodes','roots','prices'}<=set(data):
        raise KCError('Expected exported provenance JSON with nodes, roots and prices')
    graph=ProvenanceGraph(data['nodes'],data['roots'])
    prices=Prices(graph.inputs,data['prices'])
    return data,graph,prices


def annotate_selected(folder,catalog_file=None):
    source=folder/'selected_tokens.csv'
    if not source.exists() or catalog_file is None:return
    catalog={}
    with Path(catalog_file).open(newline='',encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            token=row['provenance']
            if token in catalog:raise KCError('Duplicate tuple token in supplied catalog')
            catalog[token]=row
    with source.open(newline='',encoding='utf-8') as src, \
         (folder/'selected_tuples.csv').open('w',newline='',encoding='utf-8') as dest:
        writer=csv.writer(dest);writer.writerow(['relation','tuple_id','provenance','price'])
        for row in csv.DictReader(src):
            token=row['provenance']
            if token not in catalog:raise KCError('Selected input is absent from tuple catalog: '+token)
            m=catalog[token];writer.writerow([m['relation'],m['tuple_id'],token,row['price']])


def compiler_in_worker_group(command, log, seconds, cwd=None):
    """Keep optional c2d in the worker's process group so parent timeout kills it.

    The inherited core normally starts a separate session for each compiler.
    In this already-isolated worker, another session would escape the outer
    process-group timeout. Only subprocess ownership changes, not KCPrice math.
    """
    with Path(log).open('wb') as stream:
        process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,
                                 cwd=str(cwd) if cwd else None, start_new_session=False)
        try:status=process.wait(timeout=seconds)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.kill();process.wait()
            if sys.exc_info()[0] is KeyboardInterrupt:raise
            raise DeadlineExceeded('Knowledge compiler exceeded worker time budget; see '+str(log))
    if status:
        raise KCError('Compiler exited with code %s; see %s'%(status,log))


def solve_worker(args):
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'result.json').exists():raise KCError('Refusing to overwrite a previous result')
    start=time.perf_counter();deadline=Deadline(args.timeout)
    result={'suite_version':SUITE_VERSION,'algorithm':args.method,'status':'STARTED','price':None,
            'snapshot_sha256':sha256_file(args.input),'result_directory':str(out)}
    phase_timing={}
    def start_second_stage():
        if not phase_timing:
            phase_timing.update(phase_boundary_perf_counter_ns=time.perf_counter_ns(),
                                phase_partition_protocol=PHASE_PROTOCOL)
    if args.method=='SATPrice-NoReduction':
        result.update(paper_ablation_name='ILP',actual_formulation='WPMS; not an ILP optimizer')
    try:
        load_started=time.perf_counter()
        data,graph,prices=load_input(args.input)
        result['input_load_seconds']=time.perf_counter()-load_started
        result.update(n_tuples=len(graph.inputs),n_answer_roots=len(graph.roots),
                      price_scale=prices.scale)
        write_json(out/'progress.json',result)
        if args.method=='KCPrice':
            core=load_kcprice_core(getattr(args,'kcprice_dir',None))
            original_process_runner=core.run_process
            original_evaluator=core.PriceEvaluator
            try:
                def compiler_runner(*a, **kw):
                    try:
                        return compiler_in_worker_group(*a, **kw)
                    except DeadlineExceeded as exc:
                        raise core.DeadlineExceeded(str(exc)) from exc
                    except KCError as exc:
                        raise core.KCError(str(exc)) from exc
                core.run_process=compiler_runner
                def timed_evaluator(*a, **kw):
                    start_second_stage()
                    return original_evaluator(*a, **kw)
                core.PriceEvaluator=timed_evaluator
                with translate_legacy_errors(core):
                    result=core.solve(graph,data['prices'],out,args.c2d,deadline.remaining(),metadata=result)
            finally:
                core.run_process=original_process_runner
                core.PriceEvaluator=original_evaluator
            result['method_total_seconds']=time.perf_counter()-start
            annotate_selected(out,args.catalog)
            write_json(out/'result.json',result)
            return 0 if result['status']=='EXACT' else 2
        if args.method=='SATPrice':
            def timed_solver(*a, **kw):
                start_second_stage()
                return solve_rc2(*a, **kw)
            value,selected,details=run_satprice(graph,prices,out,deadline,
                    reductions=True,encoding=args.encoding,backend=timed_solver)
        elif args.method=='SATPrice-NoReduction':
            value,selected,details=run_no_reduction(graph,prices,out,deadline,encoding=args.encoding)
        elif args.method=='Baseline':
            value,selected,details=run_baseline(graph,prices,deadline,args.max_supports,args.max_candidates)
        else:raise KCError('Unsupported worker method')
        result.update(details)
        if value is not None:
            t=time.perf_counter()
            expected=sum(prices.units[i] for i in selected)
            if expected!=value or not graph.satisfied({graph.inputs[i-1] for i in selected}):
                raise KCError('Final support/price verification failed')
            with (out/'selected_tokens.csv').open('w',newline='',encoding='utf-8') as stream:
                writer=csv.writer(stream);writer.writerow(['variable','provenance','price'])
                for i in sorted(selected):
                    token=graph.inputs[i-1];writer.writerow([i,token,prices.text[token]])
            with (out/'fact_prices.csv').open('w',newline='',encoding='utf-8') as stream:
                writer=csv.writer(stream);writer.writerow(['variable','provenance','price'])
                for i,token in enumerate(graph.inputs,1):writer.writerow([i,token,prices.text[token]])
            annotate_selected(out,args.catalog)
            result.update(price=prices.format(value),price_integer_units=value,selected_count=len(selected),
                          support_verified=True,verify_seconds=time.perf_counter()-t)
        deadline.check()
    except DeadlineExceeded as exc:
        result.update(status='TIMEOUT',price=None,error=str(exc),support_verified=False)
    except SupportLimitExceeded as exc:
        result.update(status='RESOURCE_LIMIT',price=None,error=str(exc),support_verified=False)
    except MemoryError:
        result.update(status='RESOURCE_LIMIT',price=None,error='Worker memory allocation failed',support_verified=False)
    except Exception as exc:
        result.update(status='ERROR',price=None,error='%s: %s'%(type(exc).__name__,exc),support_verified=False)
    finally:
        result['method_total_seconds']=time.perf_counter()-start
        result.update(phase_timing)
        write_json(out/'result.json',result)
    return 0 if result['status']=='EXACT' else 2


def stop_group(process):
    if process.poll() is not None:return
    if os.name == 'nt':
        # Windows has no killpg; terminate the worker and its compiler subtree.
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:process.kill()
            process.wait()
        return
    try:
        os.killpg(process.pid,signal.SIGTERM)
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()
    except ProcessLookupError:process.wait()


def attach_wall_stages(result,started_ns,finished_ns):
    """Partition the parent-observed wall interval at the worker's timestamp."""
    fields=FUNCTIONAL_STAGES.get(result.get('algorithm'))
    if not fields:return
    for field in fields:result.pop(field,None)
    boundary=result.get('phase_boundary_perf_counter_ns')
    if (result.get('status')!='EXACT' or result.get('phase_partition_protocol')!=PHASE_PROTOCOL or
            not isinstance(boundary,int) or isinstance(boundary,bool) or
            not started_ns<=boundary<=finished_ns):return
    result[fields[0]]=(boundary-started_ns)/1_000_000_000
    result[fields[1]]=(finished_ns-boundary)/1_000_000_000


def isolated(input_file,out,method,timeout=100,encoding='tseitin',c2d='c2d',
             catalog=None,max_supports=200000,max_candidates=5000000,kcprice_dir=None):
    input_file=Path(input_file).resolve();out=Path(out).resolve()
    out.mkdir(parents=True,exist_ok=False)
    command=[sys.executable,str(Path(__file__).resolve()),'_worker','--input',str(input_file),
             '--out',str(out),'--method',method,'--timeout',str(timeout),'--encoding',encoding,
             '--c2d',c2d,'--max-supports',str(max_supports),'--max-candidates',str(max_candidates)]
    if catalog:command+=['--catalog',str(Path(catalog).resolve())]
    if kcprice_dir:command+=['--kcprice-dir',str(Path(kcprice_dir).resolve())]
    write_json(out/'command.json',command)
    start_ns=time.perf_counter_ns();timed_out=False;interrupted=False
    process=None
    with (out/'worker.log').open('wb') as log:
        process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True;stop_group(process)
        except KeyboardInterrupt:
            interrupted=True;stop_group(process)
    finished_ns=time.perf_counter_ns()
    elapsed=(finished_ns-start_ns)/1_000_000_000
    result_path=out/'result.json'
    try:result=json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
    except (ValueError,OSError):result={}
    result.update(algorithm=method,result_directory=str(out),snapshot_sha256=sha256_file(input_file),
                  worker_wall_seconds=elapsed,worker_exit_code=process.returncode,
                  timeout_seconds=timeout,timeout_scope='entire isolated worker, including input and output')
    if timed_out or interrupted:
        result.update(status='TIMEOUT' if timed_out else 'INTERRUPTED',price=None,support_verified=False,
                      error='Parent stopped entire worker process group before a verified result was accepted')
    elif process.returncode and result.get('status') in (None,'STARTED','EXACT'):
        result.update(status='ERROR',price=None,error='Worker failed; see worker.log',support_verified=False)
    elif result.get('status') in (None,'STARTED'):
        result.update(status='ERROR',price=None,error='Worker produced no completed result',support_verified=False)
    attach_wall_stages(result,start_ns,finished_ns)
    write_json(result_path,result)
    if interrupted:raise KeyboardInterrupt
    return result


def write_summary(path,records):
    with Path(path).open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=SUMMARY_FIELDS,extrasaction='ignore')
        writer.writeheader();writer.writerows(records)


def compare(input_file,out,methods,runs=1,timeout=100,encoding='tseitin',c2d='c2d',
            catalog=None,max_supports=200000,max_candidates=5000000,metadata=None,kcprice_dir=None,
            warmups=0,stats_reference='Baseline'):
    if not methods or runs<1 or warmups<0 or max_supports<0 or max_candidates<0:
        raise KCError('Invalid methods, runs, warmups or resource limit')
    Deadline(timeout)
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    raw=Path(input_file).read_bytes()
    frozen=out/'snapshot.json';frozen.write_bytes(raw)
    frozen_catalog=None
    if catalog:
        frozen_catalog=out/'tuple_catalog.csv';frozen_catalog.write_bytes(Path(catalog).read_bytes())
    shared=dict(metadata or {})
    write_json(out/'comparison_config.json',dict(methods=methods,runs=runs,warmups=warmups,timeout=timeout,
        suite_version=SUITE_VERSION,encoding=encoding,solver_options=dict(RC2_OPTIONS),shared=shared,
        phase_partition_protocol=PHASE_PROTOCOL,
        max_supports=max_supports,max_candidates=max_candidates,stats_reference=stats_reference,
        input_name=Path(input_file).stem,input_sha256=sha256_file(frozen),
        ordering='rotate methods by run index',primary_time_metric='worker_wall_seconds',
        machine=dict(node=platform.node(),system=platform.platform(),python=sys.version,
                     executable=sys.executable),
        note='No-reduction disables paper preprocessing, not intrinsic SAT/MaxSAT search; '
             'warmups are extra fresh worker processes, never hidden measurements'))
    records=[];check={};interrupted=False
    try:
        for run in range(1,warmups+runs+1):
            shift=(run-1)%len(methods);order=methods[shift:]+methods[:shift]
            is_warmup=run<=warmups
            for position,method in enumerate(order,1):
                folder=out/method/('run_%03d'%run)
                label='warmup %d/%d'%(run,warmups) if is_warmup else 'run %d/%d'%(run-warmups,runs)
                print('%s %s'%(method,label),flush=True)
                try:
                    result=isolated(frozen,folder,method,timeout,encoding,c2d,frozen_catalog,
                                    max_supports,max_candidates,kcprice_dir)
                except KeyboardInterrupt:
                    interrupted=True
                    path=folder/'result.json'
                    result=json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
                        status='INTERRUPTED',price=None,algorithm=method,support_verified=False,
                        snapshot_sha256=sha256_file(frozen),result_directory=str(folder))
                for key in ('scenario','data_size','query','batch_id','n_answers','price_policy',
                            'shared_provenance_seconds','shared_mapping_seconds','shared_export_seconds',
                            'shared_rewrite_seconds','price_mode','price_seed','price_decimals',
                            'price_parameters','price_config_sha256'):
                    if key in shared:result[key]=shared[key]
                result.update(run=run,measurement_index=None if is_warmup else run-warmups,
                              is_warmup=is_warmup,execution_position=position,comparison_id=str(out))
                records.append(result)
                write_json(folder/'result.json',result)
                write_summary(out/'comparison.csv',records)
                write_json(out/'results.json',records)
                wall=result.get('worker_wall_seconds')
                wall_text='--' if wall is None else '%.6fs'%wall
                print('  %s, price=%s, wall=%s'%(result['status'],result.get('price'),wall_text),flush=True)
                if interrupted:raise KeyboardInterrupt
    finally:
        exact=[r for r in records if r.get('status')=='EXACT']
        values={Decimal(r['price']) for r in exact}
        mismatch=len(values)>1 or (bool(exact) and any(r['status']=='UNSAT' for r in records))
        sat_rows=[r for r in records if r['algorithm'].startswith('SATPrice') and r.get('raw_instance_sha256')]
        hashes={r['raw_instance_sha256'] for r in sat_rows}
        if len(hashes)>1:mismatch=True
        planned=(runs+warmups)*len(methods)
        check=dict(status='MISMATCH' if mismatch else 'AGREE' if len(exact)==len(records)==planned else 'INCOMPLETE',
                   exact_rows=len(exact),total_rows=len(records),planned_rows=planned,prices=sorted(map(str,values)),
                   same_sat_raw_instance=len(hashes)<=1,
                   note='Different optimal tuple sets are allowed; optimum price must agree')
        write_json(out/'consistency.json',check)
        write_json(out/'results.json',records)
        write_summary(out/'comparison.csv',records)
        report,text=write_reports([out],out,reference=stats_reference)
        print(text,end='',flush=True)
        print('Runtime report:',out/'runtime_summary.csv',flush=True)
    if mismatch:raise KCError('Exact methods disagree or SAT inputs differ; see consistency.json')
    return records,check


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['solve','compare','check','_worker'])
    p.add_argument('--input',type=Path)
    p.add_argument('--out',type=Path)
    p.add_argument('--method',default='SATPrice')
    p.add_argument('--methods',default=DEFAULT_METHODS)
    p.add_argument('--runs',type=int,default=1)
    p.add_argument('--warmups',type=int,default=int(os.environ.get('WARMUP_RUNS','0')),
                   help='Additional fresh-worker rounds excluded from runtime summaries')
    p.add_argument('--stats-reference',default=os.environ.get('STATS_REFERENCE','Baseline'))
    p.add_argument('--timeout',type=float,default=100)
    p.add_argument('--encoding',choices=['tseitin','preserve-cnf'],default='tseitin')
    p.add_argument('--c2d',default=os.environ.get('C2D_BIN','c2d'))
    p.add_argument('--kcprice-dir',type=Path,help='Optional existing KCPrice directory; used only when KCPrice is selected')
    p.add_argument('--catalog',type=Path)
    p.add_argument('--max-supports',type=int,default=200000,help='Baseline max family; 0 disables, never truncates')
    p.add_argument('--max-candidates',type=int,default=5000000,help='Baseline candidate budget; 0 disables')
    return p


def main():
    args=parser().parse_args()
    try:
        Deadline(args.timeout)
        if args.warmups<0:raise KCError('--warmups must be nonnegative')
        if args.command=='_worker':return solve_worker(args)
        methods=parse_methods(args.method if args.command=='solve' else args.methods)
        info=preflight(methods,args.c2d,smoke=args.command=='check',kcprice_dir=args.kcprice_dir)
        if args.command=='check':print(json.dumps(info,indent=2));return 0
        if args.input is None or args.out is None:raise KCError('--input and --out are required')
        if args.command=='solve':
            result=isolated(args.input,args.out,methods[0],args.timeout,args.encoding,args.c2d,
                            args.catalog,args.max_supports,args.max_candidates,args.kcprice_dir)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result['status']=='EXACT' else 1
        _,check=compare(args.input,args.out,methods,args.runs,args.timeout,args.encoding,args.c2d,
                       args.catalog,args.max_supports,args.max_candidates,kcprice_dir=args.kcprice_dir,
                       warmups=args.warmups,stats_reference=args.stats_reference)
        print(json.dumps(check,ensure_ascii=False,indent=2))
        return 0 if check['status']=='AGREE' else 1
    except (KCError,OSError,ValueError,KeyError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 1
    except KeyboardInterrupt:
        print('Interrupted; worker process group stopped',file=sys.stderr);return 130


if __name__=='__main__':sys.exit(main())
