"""Read-only bridge to the bundled KCPrice core and database adapter.

SATPrice/Baseline and the offline comparison CLI do not import that installation.
The bridge is activated only for database export or an explicit KCPrice run.
No sys.path entry points into kcprice, and no bytecode is written there.
"""
from __future__ import annotations
from contextlib import contextmanager
import functools
import hashlib
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType

from pricing.common import KCError, DeadlineExceeded

_LOCK=threading.RLock()
_CACHE={}


def default_kcprice_dir():
    return Path(__file__).resolve().parents[1]/'kcprice'


def _existing_file(directory, name):
    path=(Path(directory).expanduser().resolve()/name)
    if not path.is_file():
        raise KCError('Existing KCPrice file not found: %s. Offline SATPrice/Baseline do not need it; '
                      'database export and explicit KCPrice runs require the installed adapter/core.' % path)
    return path


def _load_source(path: Path, kind: str, binding=None):
    """Load one exact source file without touching unrelated imports or caches."""
    raw=path.read_bytes()
    digest=hashlib.sha256(str(path).encode()+b'\x00'+raw).hexdigest()[:24]
    binding_name=getattr(binding,'__name__','')
    key=(kind,digest,binding_name)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        name='_forbackbench_pricing_%s_%s'%(kind,digest)
        spec=importlib.util.spec_from_file_location(name,str(path))
        if spec is None or spec.loader is None:
            raise KCError('Cannot load source: '+str(path))
        module=importlib.util.module_from_spec(spec)
        sentinel=object()
        previous=sys.modules.get('kcprice',sentinel)
        prior_module=sys.modules.get(name,sentinel)
        write_setting=sys.dont_write_bytecode
        try:
            # dataclasses need the module registered while its body executes.
            sys.modules[name]=module
            sys.dont_write_bytecode=True
            if binding is not None:
                sys.modules['kcprice']=binding
            # Execute the exact bytes already hashed; do not even read a stale
            # pre-existing __pycache__ entry from the KCPrice installation.
            exec(compile(raw,str(path),'exec',dont_inherit=True),module.__dict__)
        except Exception as exc:
            if prior_module is sentinel:sys.modules.pop(name,None)
            else:sys.modules[name]=prior_module
            raise KCError('Cannot import installed %s: %s: %s'%(path,type(exc).__name__,exc)) from exc
        finally:
            sys.dont_write_bytecode=write_setting
            if binding is not None:
                if previous is sentinel:sys.modules.pop('kcprice',None)
                else:sys.modules['kcprice']=previous
        _CACHE[key]=module
        return module


def load_kcprice_core(directory=None):
    """Only callers explicitly selecting KCPrice need to call this function."""
    path=_existing_file(directory or default_kcprice_dir(),'kcprice.py')
    return _load_source(path,'core')


@contextmanager
def translate_legacy_errors(core):
    """Retain TIMEOUT vs ERROR across the independently namespaced modules."""
    try:
        yield
    except core.DeadlineExceeded as exc:
        raise DeadlineExceeded(str(exc)) from exc
    except core.KCError as exc:
        translated=KCError(str(exc))
        # Preserve the v5 callback diagnostics when present.
        translated.__dict__.update(getattr(exc,'__dict__',{}))
        raise translated from exc


class AdapterProxy:
    def __init__(self,module,core):
        self._module,self._core=module,core
        self._functions={}

    def __getattr__(self,name):
        item=getattr(self._module,name)
        if not callable(item) or isinstance(item,type):
            return item
        if name not in self._functions:
            @functools.wraps(item)
            def call(*args,**kwargs):
                with translate_legacy_errors(self._core):
                    return item(*args,**kwargs)
            self._functions[name]=call
        return self._functions[name]


def load_adapter(directory=None):
    directory=directory or default_kcprice_dir()
    core=load_kcprice_core(directory)
    module=_load_source(_existing_file(directory,'forbackbench.py'),'adapter',binding=core)
    return AdapterProxy(module,core)
