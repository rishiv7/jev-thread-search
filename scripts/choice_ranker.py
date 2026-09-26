"""Cached, size-bounded Jev Choice shortlisting and final relative ranking."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable
from jev_ranker import RankError, _encoded, MAX_REQUEST_BYTES, PROMPT_VERSION

CHOICE_PROMPT_VERSION = 'batched-choice-v2+' + PROMPT_VERSION
CHOICE_QUESTION = 'Which thread best matches what the user is looking for in query?'

def _body(query, model, ids, evidence):
    return {'model': model, 'state': {'query': query, 'threads': {i: evidence[i] for i in ids}},
            'questions': {'q0': {'type': 'choice', 'instructions': CHOICE_QUESTION,
                                  'criteria': {i: None for i in ids}}}}

def _validate(response, ids, model):
    if not isinstance(response, dict) or response.get('model') != model:
        raise RankError('invalid_response')
    answers, usage = response.get('answers'), response.get('usage')
    if not isinstance(answers, dict) or set(answers) != {'q0'} or not isinstance(usage, dict):
        raise RankError('invalid_response')
    tokens, output = usage.get('input_tokens'), usage.get('output_tokens')
    if type(tokens) is not int or not 0 < tokens <= 32000 or type(output) is not int or output < 0:
        raise RankError('invalid_response')
    answer = answers['q0']
    if not isinstance(answer, dict) or answer.get('type') != 'choice' or answer.get('choice') not in ids:
        raise RankError('invalid_response')
    probs, confidence = answer.get('probabilities'), answer.get('confidence')
    if not isinstance(probs, dict) or set(probs) != set(ids):
        raise RankError('invalid_response')
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in probs.values()):
        raise RankError('invalid_response')
    if not math.isclose(sum(probs.values()), 1, abs_tol=.01) or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise RankError('invalid_response')
    return probs, tokens

def rank_by_choice(query: str, candidates: list[dict], evidence: dict[str, Any], cache_dir: Path,
                   key: str | None, model: str, top_k: int, workers: int, refresh: bool,
                   post: Callable) -> tuple[list[dict], dict]:
    stats = {'choice_api_calls': 0, 'choice_cache_hits': 0, 'choice_questions': 0,
             'choice_validated_tokens': 0, 'choice_max_input_tokens': 0, 'choice_request_bytes': 0}
    if not candidates or top_k == 0:
        return [], stats
    by_id = {c['id']: c for c in candidates}
    ids = sorted(by_id)
    count = min(top_k, len(ids))
    path = Path(cache_dir) / 'jev_choices.sqlite3'
    db = sqlite3.connect(path)
    path.chmod(0o600)
    db.execute('CREATE TABLE IF NOT EXISTS choice_batches (cache_key TEXT PRIMARY KEY, probabilities TEXT NOT NULL)')
    try:
        while True:
            groups, group = [], []
            for thread_id in ids:
                if len(_encoded(_body(query, model, [thread_id], evidence))) > MAX_REQUEST_BYTES:
                    raise RankError('choice_evidence_too_large')
                if group and len(_encoded(_body(query, model, group + [thread_id], evidence))) > MAX_REQUEST_BYTES:
                    groups.append(group)
                    group = []
                group.append(thread_id)
            if group:
                groups.append(group)
            requests, resolved, pending = [], {}, []
            for index, group in enumerate(groups):
                body = _body(query, model, group, evidence)
                digest = hashlib.sha256(CHOICE_PROMPT_VERSION.encode() + _encoded(body)).hexdigest()
                requests.append((index, digest, body, group))
                stats['choice_questions'] += 1
                cached = None if refresh else db.execute('SELECT probabilities FROM choice_batches WHERE cache_key=?', (digest,)).fetchone()
                if cached:
                    probs = json.loads(cached[0])
                    if set(probs) != set(group) or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in probs.values()):
                        raise RankError('invalid_cache')
                    resolved[index] = probs
                    stats['choice_cache_hits'] += 1
                else:
                    pending.append(requests[-1])
            if pending:
                if not key:
                    raise RankError('missing_api_key')
                def send(item):
                    index, digest, body, group = item
                    response, attempts = post(body, key)
                    probs, tokens = _validate(response, group, model)
                    return index, digest, probs, tokens, attempts, len(_encoded(body))
                with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as pool:
                    for index, digest, probs, tokens, attempts, size in pool.map(send, pending):
                        resolved[index] = probs
                        stats['choice_api_calls'] += attempts
                        stats['choice_request_bytes'] += size * attempts
                        stats['choice_validated_tokens'] += tokens
                        stats['choice_max_input_tokens'] = max(stats['choice_max_input_tokens'], tokens)
                        db.execute('INSERT OR REPLACE INTO choice_batches VALUES (?,?)', (digest, json.dumps(probs)))
                db.commit()
            orderings = [sorted(group, key=lambda i: (-resolved[n][i], i)) for n, group in enumerate(groups)]
            if len(groups) == 1:
                return [by_id[i] for i in orderings[0][:count]], stats
            quotas = [min(2, len(g)) for g in groups]
            while sum(quotas) < count:
                for n, group in enumerate(groups):
                    if quotas[n] < len(group) and sum(quotas) < count:
                        quotas[n] += 1
            next_ids = sorted(i for n, group in enumerate(orderings) for i in group[:quotas[n]])
            if len(next_ids) >= len(ids):
                raise RankError('choice_shortlist_too_large')
            ids = next_ids
    finally:
        db.close()
