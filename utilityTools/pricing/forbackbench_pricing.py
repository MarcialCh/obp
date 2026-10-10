#!/usr/bin/env python3
"""Freeze one ForBackBench/ProvSQL snapshot and compare exact pricing methods.

The bundled forbackbench.py supplies all database and legacy API operations;
this module does not patch that adapter or recreate its semiring callbacks.
Database rebuild is intentionally unsupported in this comparison entry point.
"""
from __future__ import annotations
import argparse
import csv
import datetime
import json
import os
from pathlib import Path
import sys
import time

# Direct-file and package-mode execution both resolve sibling packages.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pricing.legacy_bridge import load_adapter
from pricing.common import KCError, Deadline, DeadlineExceeded, Prices, write_json, sha256_file
from pricing.pricing_suite import DEFAULT_METHODS, SUITE_VERSION, parse_methods, preflight, compare, write_summary
from pricing.runtime_stats import write_reports
from pricing.price_generation import (add_price_arguments, settings as price_settings, generate_prices,
                                      batch_settings, selected_modes, mode_args, RepricedSnapshots)


def require_adapter(fb):
    required=('get_paths','read_pg_config','check_provsql','fetch_provenance','resolve_tuples',
              'tuple_prices','prepare','validate_sql','obtain_gqr_sql','query_ids','build_parser')
    missing=[name for name in required if not callable(getattr(fb,name,None))]
    if missing:raise KCError('Installed forbackbench.py lacks adapter functions: '+', '.join(missing))
    return getattr(fb,'ADAPTER_VERSION','unreported')


class _AnswerTimingCursor:
    """Time the existing answer cursor without issuing another query."""
    def __init__(self,cursor,timing):
        object.__setattr__(self,'_cursor',cursor)
        object.__setattr__(self,'_timing',timing)

    def __getattr__(self,name):return getattr(self._cursor,name)
    def __setattr__(self,name,value):setattr(self._cursor,name,value)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self,kind,value,tb):
        closed=False
        try:
            result=self._cursor.__exit__(kind,value,tb)
            closed=True
            return result
        finally:
            self._timing.answer_end=time.perf_counter()
            self._timing.answers_complete=closed and kind is None and self._timing.exhausted

    def execute(self,*args,**kwargs):
        self._timing.executions+=1
        self._timing.answer_start=time.perf_counter()
        return self._cursor.execute(*args,**kwargs)

    def __iter__(self):
        for row in self._cursor:
            yield row
        self._timing.exhausted=True


class _TimedFetchConnection:
    """Known v5/v6/v7 adapter cursor contract; all other cursors stay native."""
    def __init__(self,conn,timing):self._conn,self._timing=conn,timing
    def __getattr__(self,name):return getattr(self._conn,name)
    def cursor(self,*args,**kwargs):
        cursor=self._conn.cursor(*args,**kwargs)
        name=kwargs.get('name',args[0] if args else None)
        return _AnswerTimingCursor(cursor,self._timing) if name=='kcprice_answers' else cursor


class _SharedFetchTiming:
    def __init__(self):
        self.started=time.perf_counter()
        self.answer_start=self.answer_end=None
        self.executions=0
        self.exhausted=self.answers_complete=False

    def finish(self,succeeded):
        ended=time.perf_counter()
        info=dict(shared_timing_protocol='query-roots-then-dag-v1',
                  shared_answer_query_executions=self.executions,
                  shared_fetch_seconds=ended-self.started,
                  shared_query_execution_seconds=None,shared_provenance_build_seconds=None,
                  shared_provenance_setup_seconds=None,
                  query_execution_status='UNAVAILABLE' if succeeded else 'NOT_STARTED',
                  provenance_build_status='UNAVAILABLE' if succeeded else 'NOT_STARTED')
        if self.executions==1 and self.answer_start is not None and self.answer_end is not None:
            info.update(shared_query_execution_seconds=self.answer_end-self.answer_start,
                        shared_provenance_setup_seconds=self.answer_start-self.started,
                        query_execution_status='OK' if self.answers_complete else 'ERROR')
            if self.answers_complete:
                info.update(shared_provenance_build_seconds=ended-self.answer_end,
                            provenance_build_status='OK' if succeeded else 'ERROR')
        return info


def export_snapshot(config,scenario,sql_file,out,args,metadata=None,adapter=None):
    """One REPEATABLE READ transaction, one full-answer DAG, one tuple-price policy."""
    fb=adapter or load_adapter(Path(args.root)/'utilityTools'/'kcprice')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter()
    info=dict(metadata or {},suite_version=SUITE_VERSION,adapter_version=require_adapter(fb),
              db_config=fb.public_config(config),status='EXPORTING')
    price_settings(args)
    psycopg2,_=fb.dependencies()
    conn=None
    try:
        query=fb.validate_sql(Path(sql_file).read_text(encoding='utf-8'))
        (out/'query.sql').write_text(query+';\n',encoding='utf-8')
        info['sql_sha256']=sha256_file(out/'query.sql')
        tables=[p.stem for p in fb.source_files(scenario,args.size)]
        conn=psycopg2.connect(**config)
        conn.set_session(isolation_level='REPEATABLE READ')
        info.update(fb.check_provsql(conn,args.schema))
        timing=_SharedFetchTiming()
        fetched=False
        try:
            graph,nodes,prov=fb.fetch_provenance(_TimedFetchConnection(conn,timing),query,args.schema,tables,
                    args.provenance_timeout,args.batch_size,args.max_gates)
            fetched=True
        finally:
            info.update(timing.finish(fetched))
        wrapped=prov.pop('provenance_sql')
        (out/'provenance_query.sql').write_text(wrapped+';\n',encoding='utf-8')
        info.update(prov)
        info['shared_provenance_seconds']=prov['provenance_seconds']
        t=time.perf_counter()
        catalog=fb.resolve_tuples(conn,graph.inputs,args.schema,tables,args.provenance_timeout)
        info['shared_mapping_seconds']=time.perf_counter()-t
        # No expensive solver is invoked while a DB snapshot is held.
        conn.rollback();conn.close();conn=None
        price_start=time.perf_counter()
        raw_prices,policy=generate_prices(catalog,args,fb)
        info['shared_price_generation_seconds']=time.perf_counter()-price_start
        write_json(out/'price_policy.json',policy)
        prices=Prices(graph.inputs,raw_prices)  # fail before launching any method if a price is invalid
        info.update(policy)
        # Only gates reached by this complete answer set and the corresponding
        # original inputs are kept; this is shared identically by all methods.
        snapshot={'roots':list(graph.roots),'nodes':nodes,
                  'prices':{token:prices.text[token] for token in graph.inputs}}
        write_json(out/'provenance.json',snapshot)
        with (out/'tuple_catalog.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream);writer.writerow(['provenance','relation','tuple_id','price','mapping'])
            for token in graph.inputs:
                fact=catalog[token]
                writer.writerow([token,fact['relation'],fact['tuple_id'],prices.text[token],fact.get('mapping','')])
        info.update(status='EXPORTED',snapshot_sha256=sha256_file(out/'provenance.json'),
                    n_tuples=len(graph.inputs),n_provenance_gates=len(graph.nodes),
                    tuple_catalog_sha256=sha256_file(out/'tuple_catalog.csv'))
    except Exception as exc:
        info.update(status='TIMEOUT' if isinstance(exc,DeadlineExceeded) or getattr(exc,'pgcode',None)=='57014' else 'ERROR',
                    error='%s: %s'%(type(exc).__name__,exc))
        raise
    finally:
        if conn is not None:
            try:conn.rollback()
            finally:conn.close()
        info['shared_export_seconds']=time.perf_counter()-started
        write_json(out/'export_metadata.json',info)
    return out/'provenance.json',out/'tuple_catalog.csv',info


def base_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "price", "run", "diagnose"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--scenario", default="scenarios/Deep100")
    parser.add_argument("--size", default="medium")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--provsql-backend", choices=['auto','gate-api','legacy-provenance-evaluate'],
                        default=os.environ.get('PROVSQL_BACKEND','auto'))
    parser.add_argument("--legacy-max-chars", type=int, default=int(os.environ.get('LEGACY_MAX_CHARS','16777216')))
    parser.add_argument("--self-test", action="store_true", help="diagnose: test live input/OR/AND/self-join semantics")
    parser.add_argument("--diagnostic-out", type=Path, help="diagnose: write JSON without passwords")
    parser.add_argument("--queries", default="all")
    parser.add_argument("--query", default="1", help="Single query for the price command")
    parser.add_argument("--sql-file", type=Path)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--c2d", default=os.environ.get("C2D_BIN", "c2d"))
    parser.add_argument("--java", default=os.environ.get("JRE", "java"))
    parser.add_argument("--timeout", type=float, default=100)
    parser.add_argument("--gqr-timeout", type=float, default=300)
    parser.add_argument("--provenance-timeout", type=float, default=300)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-gates", type=int, default=2000000, help="0 disables guard; guard never truncates")
    add_price_arguments(parser)
    parser.add_argument("--reuse-sql", action="store_true")
    parser.add_argument("--regenerate-tgds", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--refresh-mappings", action="store_true")
    parser.add_argument("--rebuild-db", action="store_true")
    parser.add_argument("--confirm-rebuild", default="")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument("--check-decomposable", action="store_true")
    return parser


def build_parser():
    p=base_parser()
    p.description=__doc__
    p.add_argument('--methods',default=os.environ.get('PRICING_METHODS',DEFAULT_METHODS))
    p.add_argument('--encoding',choices=['tseitin','preserve-cnf'],default='tseitin')
    p.add_argument('--max-supports',type=int,default=200000)
    p.add_argument('--max-candidates',type=int,default=5000000)
    p.add_argument('--warmups',type=int,default=int(os.environ.get('WARMUP_RUNS','0')))
    p.add_argument('--stats-reference',default=os.environ.get('STATS_REFERENCE','Baseline'))
    return p


def main():
    args=build_parser().parse_args()
    try:
        price_config=batch_settings(args)
        price_modes=selected_modes(args)
        print('Tuple prices: '+json.dumps(price_config,sort_keys=True),flush=True)
        if args.warmups<0:raise KCError('--warmups must be nonnegative')
        if args.rebuild_db:
            raise KCError('Comparison runner never drops a database. Load the required benchmark data separately, then reuse it.')
        if args.no_prune:
            raise KCError('Select --methods SATPrice-NoReduction rather than --no-prune; keep both methods explicitly named.')
        if args.check_decomposable:
            raise KCError('--check-decomposable belongs to the original KCPrice CLI, not this WPMS comparison runner')
        if min(args.runs,args.batch_size)<=0 or min(args.max_gates,args.max_supports,args.max_candidates,args.legacy_max_chars)<0:
            raise KCError('Invalid repetitions or resource guard')
        for seconds in (args.timeout,args.provenance_timeout,args.gqr_timeout):Deadline(seconds)
        os.environ['PROVSQL_BACKEND']=args.provsql_backend
        os.environ['LEGACY_MAX_CHARS']=str(args.legacy_max_chars)
        fb=load_adapter(Path(args.root)/'utilityTools'/'kcprice')
        require_adapter(fb)
        root,scenario,relative=fb.get_paths(args)
        config=fb.read_pg_config(scenario/'postgres-config.ini')
        print('Database: %s @ %s:%s; schema=%s'%(config['dbname'],config['host'],config['port'],args.schema),flush=True)
        if args.command=='diagnose':
            result=fb.diagnose(config,scenario,args.size,args.schema,args.self_test,args.provenance_timeout,args.batch_size)
            if args.diagnostic_out:write_json(args.diagnostic_out,result)
            print(json.dumps(result,ensure_ascii=False,indent=2));return int(result['status']=='ERROR')
        if args.command=='prepare':
            print(json.dumps(fb.prepare(config,scenario,args.size,args.schema,args.refresh_mappings),indent=2));return 0
        methods=parse_methods(args.methods)
        solver_info=preflight(methods,args.c2d,smoke=True,kcprice_dir=root/'utilityTools'/'kcprice')
        fb.dependencies()
        try:import sqlparse
        except ImportError as exc:
            raise KCError('Missing SQL parser; install the original utilityTools/kcprice/requirements.txt using '+sys.executable) from exc
        setup={}
        if args.command=='run' and not args.skip_prepare:
            setup=fb.prepare(config,scenario,args.size,args.schema,args.refresh_mappings)
        ids=fb.query_ids(scenario,args.queries if args.command=='run' else args.query)
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        failed=False
        comparison_folders=[];pipeline_errors=[]
        session_report=root/'experiments'/'pricing'/relative/args.size/'runtime'/stamp
        for q in ids:
            batch=root/'experiments'/'pricing'/relative/args.size/('Q'+q)/stamp
            batch.mkdir(parents=True,exist_ok=False)
            shared=dict(setup,query='Q'+q,batch_id=stamp,scenario=str(relative),data_size=args.size,
                        db_mode='reuse_existing_database',solver_preflight=solver_info,status='REWRITING')
            try:
                rewrite_start=time.perf_counter()
                try:
                    if args.command=='run':
                        sql_file,rewrite=fb.obtain_gqr_sql(root,scenario,relative,q,batch,args)
                    else:
                        sql_file=args.sql_file or root/'experiments'/'outputs'/'gqr'/relative/'rewritings'/('Q'+q+'-SQL.txt')
                        rewrite={'rewrite_mode':'provided_sql','gqr_seconds':0.0}
                finally:
                    shared['shared_rewrite_seconds']=time.perf_counter()-rewrite_start
                shared.update(rewrite)
                shared['shared_query_rewrite_seconds']=rewrite.get('gqr_seconds')
                shared['status']='EXPORTING'
                shared.update(shared_preprocessing_id=str((batch/'export').resolve()),
                              shared_price_modes=[c['mode'] for c in price_config['configurations']],
                              shared_price_seed=price_config['seed'])
                snapshot,catalog,shared=export_snapshot(config,scenario,sql_file,batch/'export',
                        mode_args(args,price_modes[0]),shared,adapter=fb)
                shared['shared_snapshot_sha256']=shared.get('snapshot_sha256')
                repriced=RepricedSnapshots(snapshot,catalog,shared)
                for mode_index,mode in enumerate(price_modes):
                    mode_folder=batch/'price_modes'/mode if len(price_modes)>1 else batch
                    mode_folder.mkdir(parents=True,exist_ok=True)
                    comparison=mode_folder/'comparison'
                    mode_shared=dict(shared,requested_price_mode=mode)
                    try:
                        if mode_index:
                            mode_snapshot,mode_catalog,mode_shared=repriced.write(
                                mode_folder/'export',mode_args(args,mode),fb)
                        else:
                            mode_snapshot,mode_catalog=snapshot,catalog
                        print('Price mode: %s; seed=%s'%(mode_shared.get('price_mode',mode),
                              mode_shared.get('price_seed')),flush=True)
                        records,consistency=compare(mode_snapshot,comparison,methods,args.runs,args.timeout,
                                args.encoding,args.c2d,mode_catalog,args.max_supports,args.max_candidates,mode_shared,
                                kcprice_dir=root/'utilityTools'/'kcprice',
                                warmups=args.warmups,stats_reference=args.stats_reference)
                        print('Comparison:',comparison/'comparison.csv',flush=True)
                        # Observed answers must have satisfiable provenance.
                        if any(r['status']=='UNSAT' for r in records):
                            raise KCError('Exported observed-answer provenance unexpectedly UNSAT; inspect export and solvers')
                        if consistency['status']!='AGREE':
                            failed=True
                            if not args.continue_on_error:return 1
                    except Exception as exc:
                        saved=mode_folder/'export'/'export_metadata.json'
                        if mode_index and saved.is_file():
                            mode_shared.update(json.loads(saved.read_text(encoding='utf-8')))
                        status='TIMEOUT' if isinstance(exc,DeadlineExceeded) else 'ERROR'
                        write_json(mode_folder/'pipeline_error.json',dict(query='Q'+q,status=status,price=None,
                            price_mode=mode,error=str(exc),shared=mode_shared))
                        print('ERROR [%s]: %s'%(mode,exc),file=sys.stderr)
                        failed=True
                        if not args.continue_on_error:return 1
                    finally:
                        if (comparison/'results.json').exists() or (comparison/'comparison.csv').exists():
                            comparison_folders.append(comparison)
                        if (mode_folder/'pipeline_error.json').exists():
                            pipeline_errors.append(mode_folder/'pipeline_error.json')
            except Exception as exc:
                status='TIMEOUT' if isinstance(exc,DeadlineExceeded) or getattr(exc,'pgcode',None)=='57014' else 'ERROR'
                saved_export=batch/'export'/'export_metadata.json'
                if saved_export.is_file():shared.update(json.loads(saved_export.read_text(encoding='utf-8')))
                if shared.get('status')!='EXPORTED':shared.update(status=status,error=str(exc))
                write_json(batch/'pipeline_error.json',dict(query='Q'+q,status=status,price=None,error=str(exc),shared=shared))
                print('ERROR: '+str(exc),file=sys.stderr);failed=True
                if not args.continue_on_error:return 1
            finally:
                if (batch/'pipeline_error.json').exists() and batch/'pipeline_error.json' not in pipeline_errors:
                    pipeline_errors.append(batch/'pipeline_error.json')
                try:
                    write_reports(comparison_folders,session_report,reference=args.stats_reference,
                                  pipeline_failures=pipeline_errors,
                                  metadata={'batch_id':stamp,'requested_queries':ids,
                                            'selection':'this invocation only'})
                    print('Batch runtime summary:',session_report/'runtime_summary.csv',flush=True)
                except Exception as report_error:
                    failed=True
                    print('ERROR: runtime summary failed: '+str(report_error),file=sys.stderr)
        return int(failed)
    except KeyboardInterrupt:
        print('Interrupted; active pricing worker is stopped by its parent',file=sys.stderr);return 130
    except Exception as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
