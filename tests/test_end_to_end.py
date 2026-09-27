import contextlib
from datetime import datetime,timezone
import io,json
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'/'jev-thread-search'/'skills'/'jev-thread-search'/'scripts'))
import thread_search as cli
import jev_ranker

class EndToEndTests(unittest.TestCase):
    def test_real_extraction_ranking_cache_and_title_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'profile';sessions=root/'sessions';sessions.mkdir(parents=True)
            cache=Path(temp)/'cache';now=datetime.now(timezone.utc).isoformat()
            marker='PRIVATE_FIXTURE_MUST_NEVER_APPEAR_IN_CLI_OUTPUT'
            index=[]
            for i in range(6):
                tid=f'thread-{i}'
                events=[{'type':'session_meta','payload':{'id':tid,'source':'cli','timestamp':now,'agent_path':'/root','base_instructions':{'provenance':{'model':'gpt-6-astra'}}}},
                  {'type':'response_item','timestamp':now,'payload':{'type':'message','role':'user','content':[{'type':'input_text','text':marker}]}},
                  {'type':'response_item','timestamp':now,'payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'Completed synthetic work.'}]}}]
                (sessions/f'rollout-{tid}.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
                index.append({'id':tid,'thread_name':f'Thread {i}','updated_at':now})
            indexfile=root/'session_index.jsonl'
            indexfile.write_text('\n'.join(json.dumps(i) for i in index))
            def post(body,key):
                self.assertEqual(key,'synthetic-key')
                if next(iter(body['questions'].values()))['type']=='choice':
                    options=body['state']['threads']
                    winner=max(options,key=lambda k:int(options[k]['title'].split()[-1]))
                    answers={name:{'type':'choice','choice':winner,'probabilities':{k:float(k==winner) for k in options},'confidence':1.0} for name in body['questions']}
                else:
                    for question in body['questions'].values():
                        exs=question['instructions']['thread']['excerpt']['exchanges']
                        self.assertEqual(exs[0]['assistant'],'Completed synthetic work.')
                        self.assertEqual(exs[0]['response_model'],'gpt-6-astra')
                    answers={name:{'type':'noul','noul':int(q['instructions']['thread']['title'].split()[-1])/10} for name,q in body['questions'].items()}
                return {'model':body['model'],'answers':answers,'usage':{'input_tokens':300,'output_tokens':10}},1
            def run():
                out=io.StringIO()
                with patch.object(cli,'config',return_value={}),patch.object(cli,'get_key',return_value='synthetic-key'),patch.object(jev_ranker,'_post',side_effect=post),contextlib.redirect_stdout(out):
                    cli.main(['--cache-dir',str(cache),'search','--root',str(root),'--query','Find synthetic work'])
                self.assertNotIn(marker,out.getvalue())
                return json.loads(out.getvalue())
            first=run()
            self.assertEqual(len(first['results']),5)
            self.assertEqual(first['results'][0]['thread_id'],'thread-5')
            self.assertEqual(first['coverage']['parsed'],6)
            self.assertGreater(first['jev']['api_calls'],0)
            second=run()
            self.assertEqual(second['coverage']['parsed'],0)
            self.assertEqual(second['jev']['api_calls'],0)
            self.assertEqual(first['results'],second['results'])
            index[0]['thread_name']='Thread 9'
            indexfile.write_text('\n'.join(json.dumps(i) for i in index))
            third=run()
            self.assertEqual(third['coverage']['parsed'],0)
            self.assertGreater(third['jev']['api_calls'],0)
            self.assertEqual(third['jev']['cache_hits'],5)
            self.assertEqual(third['results'][0]['thread_id'],'thread-0')

if __name__=='__main__':unittest.main()
