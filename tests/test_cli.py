import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'/'jev-thread-search'/'skills'/'jev-thread-search'/'scripts'))
import thread_search as cli

class CLIIntegrationTests(unittest.TestCase):
    def run_cli(self,args,refresh,rank=None):
        out=io.StringIO()
        module=types.ModuleType('thread_cache');module.refresh_cache=refresh
        rankmod=types.ModuleType('jev_ranker')
        class RankError(Exception):
            def __init__(self,code):self.code=code
        rankmod.RankError=RankError;rankmod.rank_threads=rank
        with patch.dict(sys.modules,{'thread_cache':module,'jev_ranker':rankmod}),patch.object(cli,'config',return_value={}),patch.object(cli,'get_key',return_value='fake'),contextlib.redirect_stdout(out):
            cli.main(args)
        return json.loads(out.getvalue())

    def test_search_only_returns_metadata_and_excludes_current(self):
        with tempfile.TemporaryDirectory() as d:
            marker='DO_NOT_PRINT_PRIVATE_TRANSCRIPT'
            candidate={'id':'test-id','title':'Example title','profile':'/profiles/test profile','created_at':'2026-09-20T00:00:00Z','last_activity':'2026-09-21T00:00:00Z','models':['test-model'],'parts':[{'text':marker}],'content_hash':'hash','source_file':marker}
            def refresh(cache,roots,days,exclude_ids):
                self.assertEqual(days,30);self.assertIn('current-id',exclude_ids)
                return [candidate],{'parsed':0,'cache_hits':1,'accidental_text':marker}
            def rank(query,candidates,cache,key,**kw):
                self.assertEqual(query,'Original user wording');self.assertEqual(kw['top_k'],5)
                return [{**candidate,'relevance_score':0.9}],{'api_calls':0,'cache_hits':1,'accidental_text':marker}
            with patch.dict('os.environ',{'CODEX_THREAD_ID':'current-id'}):
                r=self.run_cli(['--cache-dir',d,'search','--query','Original user wording'],refresh,rank)
            self.assertNotIn(marker,json.dumps(r))
            self.assertEqual(r['results'][0]['resume_command'],"CODEX_HOME='/profiles/test profile' codex resume test-id")
            self.assertFalse(r['no_clear_match'])

    def test_refresh_does_not_load_ranker_or_require_key(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.run_cli(['--cache-dir',d,'refresh','--days','14'],lambda *a,**k:([],{'parsed':0}))
            self.assertEqual(r['status'],'refreshed')

    def test_wider_and_all_history_windows_reach_cache(self):
        with tempfile.TemporaryDirectory() as d:
            for flags,expected in [(['--days','90'],90),(['--days','5000'],5000),(['--all'],None)]:
                def refresh(*args,**kwargs):
                    self.assertEqual(kwargs['days'],expected)
                    return [],{}
                result=self.run_cli(['--cache-dir',d,'search','--query','older conversation',*flags],refresh)
                self.assertEqual(result['days'],expected)
                self.assertEqual(result['status'],'no_threads')
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                cli.parser().parse_args(['search','--query','q','--all','--days','30'])

    def test_query_stdin_and_rejected_id(self):
        with tempfile.TemporaryDirectory() as d:
            def refresh(*a,**kw):
                self.assertIn('wrong-id',kw['exclude_ids']);return [],{}
            with patch('sys.stdin',io.StringIO('a query with "quotes" and $(literal)')):
                r=self.run_cli(['--cache-dir',d,'search','--query-stdin','--exclude-thread','wrong-id'],refresh)
            self.assertEqual(r['status'],'no_threads')

    def test_key_setup_private_and_no_echo(self):
        with tempfile.TemporaryDirectory() as d:
            secret='synthetic-key-not-a-real-credential'
            out=io.StringIO()
            with patch.object(cli,'CONFIG_DIR',Path(d)),patch('sys.stdin',io.StringIO(secret)),contextlib.redirect_stdout(out):cli.main(['set-key'])
            self.assertNotIn(secret,out.getvalue())
            self.assertEqual((Path(d)/'api-key').stat().st_mode&0o777,0o600)

    def test_lock_prevents_parallel_mutations(self):
        with tempfile.TemporaryDirectory() as d:
            with cli.locked(Path(d)):
                with self.assertRaises(cli.CLIError):
                    with cli.locked(Path(d)):pass

if __name__=='__main__':unittest.main()
