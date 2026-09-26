import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'thread_cache.py'
spec = importlib.util.spec_from_file_location('thread_cache', MODULE)
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)
NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def event(kind, payload, timestamp='2026-09-20T12:00:00Z'):
    return dict(type=kind, payload=payload, timestamp=timestamp)


def rollout(tid='a', user='hello', answer='world', day='2026-09-20', model='gpt-a', extra=None):
    t = day + 'T12:00:00Z'
    rows = [event('session_meta', dict(id=tid, timestamp=t, source='cli'), t),
            event('turn_context', dict(model=model), t),
            event('response_item', dict(type='message', role='user', content=[dict(type='input_text', text=user)]), t),
            event('response_item', dict(type='message', role='assistant', channel='final', content=[dict(type='output_text', text=answer)]), t)]
    return rows + (extra or [])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'profileA'
        (self.root / 'sessions').mkdir(parents=True)
        self.store = self.base / 'cache'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, rows, folder='sessions'):
        directory = self.root / folder
        directory.mkdir(exist_ok=True)
        path = directory / (name + '.jsonl')
        path.write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in rows))
        return path

    def run_cache(self, **kw):
        return cache.refresh_cache(self.store, [self.root], now=NOW, **kw)

    def test_cache_hit_change_new_delete_and_permissions(self):
        a = self.write('a', rollout())
        found, st = self.run_cache()
        self.assertEqual((len(found), st['parsed']), (1, 1))
        self.assertEqual(found[0]['id'], 'a')
        self.assertEqual(os.stat(self.store).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.store / 'thread_cache.sqlite3').st_mode & 0o777, 0o600)
        found, st = self.run_cache()
        self.assertEqual((st['parsed'], st['cache_hits']), (0, 1))
        with a.open('a') as f:
            f.write(json.dumps(event('response_item', dict(type='message', role='user', content=[dict(type='input_text', text='followup')]))) + '\n')
        found, st = self.run_cache()
        self.assertEqual(st['parsed'], 1)
        self.assertEqual(found[0]['parts'][0]['exchanges'][0]['user'], 'hello')
        self.write('b', rollout(tid='b'))
        self.assertEqual(self.run_cache()[1]['parsed'], 1)
        a.unlink()
        found, st = self.run_cache()
        self.assertEqual((len(found), st['deleted']), (1, 1))

    def test_excluded_filename_skips_parse_and_title_never_uses_message(self):
        self.write('rollout-2026-09-20-current-id', rollout(tid='current-id', user='private prompt'))
        result, st = self.run_cache(exclude_ids={'current-id'})
        self.assertEqual(result, [])
        self.assertEqual((st['parsed'], st['excluded']), (0, 1))
        result, st = self.run_cache()
        self.assertEqual(st['parsed'], 1)
        self.assertEqual(result[0]['title'], 'current-id')

    def test_cutoff_wider_window_and_exclude(self):
        self.write('old', rollout(tid='old', day='2026-08-01'))
        self.assertEqual(self.run_cache()[0], [])
        self.assertEqual(self.run_cache()[1]['parsed'], 0)
        self.assertEqual(self.run_cache(days=60)[0][0]['id'], 'old')
        self.assertEqual(self.run_cache(days=60, exclude_ids={'old'})[1]['excluded'], 1)

    def test_all_history_expansion_repeat_and_narrow_retention(self):
        self.write('old', rollout(tid='old', day='2026-08-01'))
        self.write('recent', rollout(tid='recent'))
        result, st = self.run_cache()
        self.assertEqual([x['id'] for x in result], ['recent'])
        self.assertEqual(st['parsed'], 2)
        result, st = self.run_cache(days=None)
        self.assertEqual({x['id'] for x in result}, {'old', 'recent'})
        self.assertEqual((st['parsed'], st['cache_hits']), (1, 1))
        self.assertEqual(self.run_cache(days=None)[1]['parsed'], 0)
        self.assertEqual([x['id'] for x in self.run_cache()[0]], ['recent'])
        result, st = self.run_cache(days=None)
        self.assertEqual((len(result), st['parsed']), (2, 0))

    def test_huge_positive_days_clamps_and_empty_all_coverage(self):
        self.write('empty', [event('session_meta', {'id': 'empty'})])
        self.write('old', rollout(tid='old', day='2026-08-01'))
        huge = 10 ** 100
        result, st = self.run_cache(days=huge)
        self.assertEqual([x['id'] for x in result], ['old'])
        self.assertEqual(st['parsed'], 2)
        self.assertEqual(self.run_cache(days=None)[1]['parsed'], 0)
        self.assertEqual(self.run_cache(days=huge)[1]['parsed'], 0)
        with self.assertRaises(ValueError):
            self.run_cache(days=0)

    def test_setup_subagent_and_unanswered(self):
        setup = '<INSTRUCTIONS>hidden</INSTRUCTIONS>\n<environment_context>hidden</environment_context>\n## My request: find a cat'
        rows = rollout(user=setup, answer='cat found', extra=[event('response_item', dict(type='message', role='user', content=[dict(type='input_text', text='unanswered')]))])
        self.write('a', rows)
        sub = rollout(tid='sub')
        sub[0]['payload']['agent_path'] = 'root/child'
        self.write('sub', sub)
        sub2 = rollout(tid='sub2')
        sub2[0]['payload']['source'] = {'subagent': 'child'}
        self.write('sub2', sub2)
        result, _ = self.run_cache()
        self.assertEqual(len(result), 1)
        self.assertEqual([(e['user'], e['assistant']) for p in result[0]['parts'] for e in p['exchanges']], [('find a cat', 'cat found'), ('unanswered', '')])
        self.assertTrue(all(not p['same_as_first'] for p in result[0]['parts']))

    def test_models_last_exchange_and_unicode_split(self):
        big = '🐱 café ' * 3000
        extra = [event('turn_context', dict(model='gpt-b')),
                 event('response_item', dict(type='message', role='user', content=[dict(type='input_text', text='next')])) ,
                 event('response_item', dict(type='message', role='assistant', channel='final', content=[dict(type='output_text', text=big)]))]
        self.write('a', rollout(extra=extra))
        result, _ = self.run_cache()
        item = result[0]
        self.assertEqual(item['models'], ['gpt-a', 'gpt-b'])
        self.assertEqual(item['initial_model'], 'gpt-a')
        self.assertEqual(''.join(e['assistant'] for p in item['parts'] for e in p['exchanges'] if e['label'] == 'last'), big)
        self.assertTrue(all(cache._json_bytes(p) <= 16000 for p in item['parts']))
        self.assertEqual(item['parts'][0]['exchanges'][0]['response_model'], 'gpt-a')
        self.assertEqual(next(e['response_model'] for p in item['parts'] for e in p['exchanges'] if e['label'] == 'last'), 'gpt-b')

    def test_malformed_tail_and_interior(self):
        p = self.write('a', rollout())
        with p.open('a') as f:
            f.write('{bad}\n')
            f.write(json.dumps(event('turn_context', dict(model='gpt-b'))) + '\n')
            f.write('{unfinished')
        result, st = self.run_cache()
        self.assertEqual(len(result), 1)
        self.assertEqual((st['parse_errors'], st['incomplete']), (2, 1))
        self.assertEqual(self.run_cache()[1]['parsed'], 0)

    def test_provenance_index_future_and_widening_efficiency(self):
        rows = rollout(tid='a')
        rows[0]['payload']['base_instructions'] = {'provenance': {'model': 'gpt-origin'}}
        rows[1]['payload']['model'] = 'gpt-response'
        rows[0]['payload']['agent_path'] = '/root'
        self.write('a', rows)
        (self.root / 'session_index.json').write_text(json.dumps({'threads': [{'id': 'a', 'title': 'Indexed'}]}))
        result, st = self.run_cache()
        self.assertEqual((result[0]['title'], result[0]['initial_model'], result[0]['parts'][0]['exchanges'][0]['response_model']), ('Indexed', 'gpt-origin', 'gpt-response'))
        self.assertEqual(self.run_cache(days=60)[1]['parsed'], 0)
        future = rollout(tid='future', day='2026-09-22')
        self.write('future', future)
        result, st = self.run_cache()
        self.assertEqual([x['id'] for x in result], ['a'])
        self.assertEqual(cache.refresh_cache(self.store, [self.root, self.base / 'missing'], now=NOW)[1]['root_errors'], 1)

    def test_jsonl_index_combined_part_and_inline_markers(self):
        user = 'Please discuss ## My request: and <INSTRUCTIONS> literally.'
        rows = rollout(tid='a', user=user, extra=[
            event('response_item', dict(type='message', role='user', content=[dict(type='input_text', text='second')])) ,
            event('response_item', dict(type='message', role='assistant', channel='final', content=[dict(type='output_text', text='second answer')]))])
        self.write('a', rows)
        (self.root / 'session_index.jsonl').write_text(json.dumps({'id': 'a', 'thread_name': 'Real index title', 'updated_at': '2026-09-20T12:00:00Z'}) + '\n')
        result, _ = self.run_cache()
        self.assertEqual(result[0]['title'], 'Real index title')
        self.assertEqual(len(result[0]['parts']), 1)
        self.assertEqual(result[0]['parts'][0]['exchanges'][0]['user'], user)
        self.assertEqual([e['label'] for e in result[0]['parts'][0]['exchanges']], ['first', 'last'])

    def test_invalid_schema_and_analysis_ignored(self):
        rows = rollout(extra=[42, {'type': 'response_item', 'payload': 'bad'},
            event('response_item', dict(type='message', role='assistant', channel='final', phase='analysis', content=[dict(type='output_text', text='hidden')]))])
        self.write('a', rows)
        result, stats = self.run_cache()
        self.assertEqual(stats['parse_errors'], 2)
        self.assertEqual(result[0]['parts'][0]['exchanges'][0]['assistant'], 'world')

    def test_dedup_richest_with_correct_profile_and_db_title_refresh(self):
        other = self.base / 'profileB'
        (other / 'archived_sessions').mkdir(parents=True)
        self.write('a', rollout(tid='same', answer='short'))
        p = other / 'archived_sessions' / 'a.jsonl'
        p.write_text(''.join(json.dumps(x) + '\n' for x in rollout(tid='same', answer='longer answer')))
        result, _ = cache.refresh_cache(self.store, [self.root, other], now=NOW)
        self.assertEqual((len(result), result[0]['profile']), (1, str(other.resolve())))
        state = sqlite3.connect(self.root / 'state_5.sqlite')
        state.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, updated_at INTEGER, agent_path TEXT, source TEXT)')
        state.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)', ('same', str(self.root / 'sessions' / 'a.jsonl'), 'Fresh title', 1789920000, 'root', 'cli'))
        state.commit(); state.close()
        result, st = cache.refresh_cache(self.store, [self.root], now=NOW)
        self.assertEqual(st['parsed'], 0)
        self.assertEqual(result[0]['title'], 'Fresh title')


if __name__ == '__main__':
    unittest.main()
