#!/usr/bin/env python3
"""One-command local transcript cache and Jev-only ranking; stdout never has excerpts."""
import argparse
import contextlib
import fcntl
import getpass
import json
import os
from pathlib import Path
import shlex
import sys
import time

BASE=Path.home()
DEFAULT_CACHE=BASE/'.cache/codex-thread-search'
CONFIG_DIR=BASE/'.config/codex-thread-search'

class CLIError(Exception):
    def __init__(self,code):self.code=code

def emit(value):
    print(json.dumps(value,ensure_ascii=True,allow_nan=False,separators=(',',':')))

def config():
    p=CONFIG_DIR/'config.json'
    if not p.exists():return {}
    try:
        result=json.loads(p.read_text())
        if not isinstance(result,dict):raise ValueError()
        return result
    except Exception:raise CLIError('invalid_configuration') from None

def defaults_roots(cfg):
    configured=cfg.get('roots')
    if configured is not None:
        if not isinstance(configured,list) or not all(isinstance(r,str) for r in configured):raise CLIError('invalid_roots')
        return sorted(set(Path(r).expanduser().resolve() for r in configured))
    paths=[BASE/'.codex']
    if os.environ.get('CODEX_HOME'):paths.append(Path(os.environ['CODEX_HOME']))
    return sorted(set(p.expanduser().resolve() for p in paths))

def get_key(cfg):
    if os.environ.get('TYPESAFE_API_KEY'):return os.environ['TYPESAFE_API_KEY'].strip()
    p=Path(os.environ.get('TYPESAFE_API_KEY_FILE') or cfg.get('key_file') or CONFIG_DIR/'api-key').expanduser()
    if not p.exists():return None
    try:
        key=p.read_text().strip()
        if '\n' in key or not key:raise ValueError()
        return key
    except Exception:raise CLIError('credential_file_unreadable') from None

@contextlib.contextmanager
def locked(cache):
    cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    cache.chmod(0o700)
    fd=os.open(cache/'run.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise CLIError('another_search_is_running') from None
        yield
    finally:
        os.close(fd)

def safe_stats(stats):
    # Never let a future library change accidentally print transcript snippets.
    return {str(k):v for k,v in stats.items() if isinstance(v,(bool,int,float)) or v is None}

def summary(candidate,rank):
    tid=candidate['id']
    title=str(candidate.get('title') or '(untitled)').replace('\n',' ').replace('\r',' ')[:200]
    return {'rank':rank,'thread_id':tid,'title':title,'created_at':candidate.get('created_at'),
            'last_activity':candidate.get('last_activity'),'models':candidate.get('models',[]),
            'relevance_score':candidate.get('relevance_score'),
            'resume_command':'CODEX_HOME='+shlex.quote(candidate['profile'])+' codex resume '+shlex.quote(tid)}

def parser():
    p=argparse.ArgumentParser(description='Find local Codex threads with Jev. Output contains metadata only; no transcript excerpts.')
    p.add_argument('--cache-dir',type=Path,default=DEFAULT_CACHE)
    sub=p.add_subparsers(dest='command',required=True)
    for name in ['refresh','search']:
        s=sub.add_parser(name)
        window=s.add_mutually_exclusive_group()
        window.add_argument('--days',type=int,default=30,help='Search threads active in the last N days (default: 30).')
        window.add_argument('--all',dest='days',action='store_const',const=None,help='Search all available local history, without a date cutoff.')
        s.add_argument('--root',type=Path,action='append',help='Override configured roots; repeat for multiple profiles.')
        s.add_argument('--exclude-thread',action='append',default=[])
        s.add_argument('--include-current',action='store_true')
        if name=='search':
            g=s.add_mutually_exclusive_group(required=True)
            g.add_argument('--query')
            g.add_argument('--query-file',type=Path)
            g.add_argument('--query-stdin',action='store_true')
            s.add_argument('--top',type=int,default=5)
            s.add_argument('--workers',type=int,default=3)
            s.add_argument('--refresh-scores',action='store_true')
    sub.add_parser('status')
    sub.add_parser('set-key',help='Read API key from stdin; store with owner-only permissions.')
    return p

def main(argv=None):
    os.umask(0o077)
    args=parser().parse_args(argv)
    if args.command=='set-key':
        key=(getpass.getpass('TypeSafe API key: ') if sys.stdin.isatty() else sys.stdin.read()).strip()
        if not key or '\n' in key:raise CLIError('invalid_key_input')
        CONFIG_DIR.mkdir(parents=True,exist_ok=True,mode=0o700);CONFIG_DIR.chmod(0o700)
        target=CONFIG_DIR/'api-key'
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:f.write(key+'\n')
        target.chmod(0o600);emit({'status':'credential_saved'});return
    cfg=config();cache=args.cache_dir.expanduser().resolve()
    if args.command=='status':
        emit({'status':'ready','credential_configured':bool(get_key(cfg)),'cache_exists':cache.exists(),
              'roots':[{ 'path':str(p),'available':p.exists()} for p in defaults_roots(cfg)]})
        return
    if args.days is not None and args.days<1:raise CLIError('invalid_days')
    if args.command=='search' and (not 1<=args.top<=20 or not 1<=args.workers<=8):raise CLIError('invalid_search_options')
    query=None
    if args.command=='search':
        try:query=args.query if args.query is not None else (args.query_file.read_text() if args.query_file else sys.stdin.read())
        except Exception:raise CLIError('query_unreadable') from None
        query=query.strip()
        if not query:raise CLIError('empty_query')
    roots=sorted(set(p.expanduser().resolve() for p in args.root)) if args.root else defaults_roots(cfg)
    excluded=set(args.exclude_thread)
    if not args.include_current and os.environ.get('CODEX_THREAD_ID'):excluded.add(os.environ['CODEX_THREAD_ID'])
    started=time.monotonic()
    from thread_cache import refresh_cache
    with locked(cache):
        candidates,coverage=refresh_cache(cache,roots,days=args.days,exclude_ids=excluded)
        if args.command=='refresh':
            emit({'status':'refreshed','days':args.days,'threads':len(candidates),'coverage':safe_stats(coverage),'elapsed_seconds':round(time.monotonic()-started,3)})
            return
        if not candidates:
            emit({'status':'no_threads','days':args.days,'results':[],'coverage':safe_stats(coverage)});return
        from jev_ranker import rank_threads,RankError
        try:
            ranked,stats=rank_threads(query,candidates,cache,get_key(cfg),model=cfg.get('model','jev-1.13.0'),top_k=args.top,workers=args.workers,refresh=args.refresh_scores)
        except RankError as e:
            # Error code is deliberately allowlisted to a simple token.
            code=getattr(e,'code','jev_failed')
            if not isinstance(code,str) or not code.replace('_','').isalnum():code='jev_failed'
            raise CLIError(code) from None
        if ranked and 'id' not in ranked[0]:raise CLIError('invalid_ranker_result')
        result={'status':'ok','days':args.days,'scope':'titles_and_first_last_exchanges',
                'ranking':'Jev relative comparisons; independent evidence scores shown separately',
                'no_clear_match':not ranked or ranked[0].get('relevance_score',0)<0.7,
                'results':[summary(r,i) for i,r in enumerate(ranked,1)],
                'coverage':safe_stats(coverage),'jev':safe_stats(stats),'elapsed_seconds':round(time.monotonic()-started,3)}
        emit(result)

if __name__=='__main__':
    try:main()
    except CLIError as e:
        emit({'status':'error','error':e.code});sys.exit(1)
    except KeyboardInterrupt:
        emit({'status':'interrupted'});sys.exit(130)
    except Exception:
        emit({'status':'error','error':'unexpected_local_error'});sys.exit(1)
