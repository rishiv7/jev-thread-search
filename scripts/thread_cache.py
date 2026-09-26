"""Incremental, private cache of user-visible Codex thread excerpts."""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PARSER_VERSION = 6
MAX_PART_BYTES = 10000


def _date(v: Any) -> datetime | None:
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace('Z', '+00:00')).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _iso(v: Any) -> str | None:
    d = _date(v)
    return d.isoformat().replace('+00:00', 'Z') if d else None


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ''.join(map(_text, v))
    if isinstance(v, dict) and v.get('type') in (None, 'input_text', 'output_text', 'text'):
        return v.get('text', '') if isinstance(v.get('text'), str) else ''
    return ''


def _user_text(s: str) -> str:
    # Strip only recognized wrappers at the start; markers within the request
    # are ordinary user content and must remain intact.
    text = s
    while True:
        stripped = text.lstrip()
        pair = None
        if stripped.startswith('<recommended_plugins>'):
            pair = '</recommended_plugins>'
        elif stripped.startswith('# AGENTS.md instructions') and '<INSTRUCTIONS>' in stripped[:1000]:
            pair = '</INSTRUCTIONS>'
        elif stripped.startswith('<INSTRUCTIONS>'):
            pair = '</INSTRUCTIONS>'
        elif stripped.startswith('<environment_context>'):
            pair = '</environment_context>'
        elif stripped.startswith('<turn_aborted>'):
            pair = '</turn_aborted>'
        if pair is None:
            break
        end = stripped.find(pair)
        if end < 0:
            return ''
        text = stripped[end + len(pair):].lstrip()
    if text.startswith('## My request:'):
        return text[len('## My request:'):].lstrip()
    return text


def _json_bytes(v: Any) -> int:
    return len(json.dumps(v, ensure_ascii=True, separators=(',', ':')).encode('ascii'))


def _parts(title: str, model: str | None, exchanges: list[dict]) -> list[dict]:
    selected = [('first', exchanges[0])]
    if len(exchanges) > 1:
        selected.append(('last', exchanges[-1]))
    same = len(selected) == 1
    base = dict(title=title, initial_model=model, same_as_first=same)
    def entry(label, ex):
        return dict(label=label, user=ex['user'], assistant=ex['assistant'],
                    response_model=ex['response_model'])
    whole = dict(base, sequence=0, exchanges=[entry(label, ex) for label, ex in selected])
    if _json_bytes(whole) <= MAX_PART_BYTES:
        return [whole]
    result = []
    for label, ex in selected:
        one = entry(label, ex)
        part = dict(base, sequence=len(result), exchanges=[one])
        if _json_bytes(part) <= MAX_PART_BYTES:
            result.append(part)
            continue
        for speaker in ('user', 'assistant'):
            value = ex[speaker]
            pos = 0
            while pos < len(value):
                blank = dict(label=label, user='', assistant='', response_model=ex['response_model'])
                template = dict(base, sequence=999999, exchanges=[blank])
                budget = MAX_PART_BYTES - _json_bytes(template)
                if budget < 16:
                    raise ValueError('part metadata exceeds limit')
                lo, hi, best = pos + 1, len(value), pos
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if _json_bytes(value[pos:mid]) - 2 <= budget:
                        best, lo = mid, mid + 1
                    else:
                        hi = mid - 1
                if best == pos:
                    raise ValueError('character exceeds part limit')
                fragment = dict(blank)
                fragment[speaker] = value[pos:best]
                part = dict(base, sequence=len(result), exchanges=[fragment])
                assert _json_bytes(part) <= MAX_PART_BYTES
                result.append(part)
                pos = best
    return result


def _parse(path: Path, title: str | None, profile: str):
    tid, created, latest, initial, current = path.stem, None, None, None, None
    source, agent_path, models, exchanges = None, None, [], []
    errors, incomplete = 0, False
    with path.open('rb') as f:
        lines = f.readlines()
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors += 1
            incomplete |= index == len(lines) - 1
            continue
        if not isinstance(event, dict) or not isinstance(event.get('payload'), (dict, type(None))):
            errors += 1
            incomplete |= index == len(lines) - 1
            continue
        kind, p = event.get('type'), event.get('payload') or {}
        ts = _iso(event.get('timestamp'))
        if kind == 'session_meta':
            tid = p.get('id') or tid
            created = _iso(p.get('timestamp')) or created
            source = p.get('source')
            agent_path = p.get('agent_path') or p.get('agentPath')
            provenance = ((p.get('base_instructions') or {}).get('provenance') or {}) if isinstance(p.get('base_instructions'), dict) else {}
            initial = initial or provenance.get('model')
            if initial and initial not in models:
                models.append(initial)
            title = title or p.get('title')
        elif kind == 'turn_context':
            current = p.get('model') or current
            if isinstance(current, str) and current:
                initial = initial or current
                if current not in models:
                    models.append(current)
        if kind == 'response_item' and p.get('type') == 'message':
            role = p.get('role')
            if role not in ('user', 'assistant') or (role == 'assistant' and (p.get('recipient') not in (None, 'all') or p.get('channel') not in (None, 'commentary', 'final') or p.get('phase') == 'analysis')):
                continue
            value = _text(p.get('content'))
        elif kind == 'event_msg' and p.get('type') in ('user_message', 'agent_message') and p.get('phase') != 'analysis':
            role = 'user' if p['type'] == 'user_message' else 'assistant'
            value = _text(p.get('message'))
        else:
            continue
        value = _user_text(value) if role == 'user' else value
        if not value:
            continue
        if ts and (not latest or ts > latest):
            latest = ts
        if role == 'user':
            if exchanges and exchanges[-1]['user'] == value and not exchanges[-1]['assistant']:
                continue
            exchanges.append(dict(user=value, assistant='', response_model=None))
        elif exchanges:
            ex = exchanges[-1]
            if value == ex['assistant'] or value in ex['assistant'].split('\n\n'):
                continue
            ex['assistant'] = (ex['assistant'] + '\n\n' if ex['assistant'] else '') + value
            ex['response_model'] = current or initial or ex['response_model']
    if isinstance(source, dict) or agent_path not in (None, '', 'root', '/root'):
        return None, errors, incomplete
    if not exchanges:
        return None, errors, incomplete
    created = created or _iso(path.stat().st_mtime)
    latest = latest or created
    title = title or tid or '(untitled)'
    parts = _parts(title, initial, exchanges)
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return dict(id=tid, title=title, profile=profile, source_file=str(path.resolve()),
                created_at=created, last_activity=latest, initial_model=initial,
                models=models, parts=parts, content_hash=digest), errors, incomplete


def _index_titles(root: Path) -> tuple[dict[str, str], int]:
    titles, errors = {}, 0
    jsonl = root / 'session_index.jsonl'
    if jsonl.is_file():
        try:
            with jsonl.open() as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        tid = entry.get('id') or entry.get('thread_id')
                        title = entry.get('thread_name') or entry.get('title')
                        if isinstance(tid, str) and isinstance(title, str) and title:
                            titles[tid] = title
                    except (ValueError, AttributeError):
                        errors += 1
        except OSError:
            errors += 1
    path = root / 'session_index.json'
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            entries = data.get('threads', data.get('sessions', data.get('entries', []))) if isinstance(data, dict) else data
            if isinstance(entries, dict):
                entries = entries.values()
            for entry in entries:
                if isinstance(entry, dict):
                    tid = entry.get('id') or entry.get('thread_id')
                    title = entry.get('thread_name') or entry.get('title') or entry.get('name')
                    if isinstance(tid, str) and isinstance(title, str) and title and tid not in titles:
                        titles[tid] = title
        except (OSError, ValueError, TypeError):
            errors += 1
    return titles, errors


def _discover(roots: list[Path]):
    paths, metadata = {}, {}
    errors = dict(root_errors=0, metadata_errors=0)
    for root in roots:
        root = Path(root).expanduser().resolve()
        profile = str(root)
        if not root.is_dir():
            errors['root_errors'] += 1
            continue
        titles, index_errors = _index_titles(root)
        errors['metadata_errors'] += index_errors
        for folder_name in ('sessions', 'archived_sessions'):
            folder = root / folder_name
            if folder.is_dir():
                try:
                    for path in folder.rglob('*.jsonl'):
                        paths[str(path.resolve())] = (path, profile)
                except OSError:
                    errors['root_errors'] += 1
        for dbpath in sorted(root.glob('state_*.sqlite')):
            try:
                db = sqlite3.connect(f'file:{dbpath}?mode=ro', uri=True)
                db.row_factory = sqlite3.Row
                try:
                    for row in db.execute('SELECT id,rollout_path,title,updated_at,agent_path,source FROM threads'):
                        if row['agent_path'] not in (None, '', 'root', '/root'):
                            continue
                        try:
                            if isinstance(json.loads(row['source']), dict):
                                continue
                        except (TypeError, ValueError):
                            pass
                        path = Path(row['rollout_path']).expanduser()
                        if path.is_file():
                            key = str(path.resolve())
                            paths[key] = (path, profile)
                            metadata[key] = dict(id=row['id'], title=titles.get(row['id']) or row['title'])
                finally:
                    db.close()
            except (sqlite3.Error, OSError):
                errors['metadata_errors'] += 1
        for key, (path, owner) in paths.items():
            if owner == profile:
                for tid, title in titles.items():
                    if tid in path.stem and key not in metadata:
                        metadata[key] = dict(id=tid, title=title)
                        break
    return paths, metadata, errors


def refresh_cache(cache_dir: Path, roots: list[Path], days: int | None = 30,
                  now: datetime | None = None, exclude_ids: set[str] | None = None):
    """Return (candidates, scalar stats); caller coordinates the global fcntl lock."""
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 1):
        raise ValueError('days must be a positive integer or None')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    max_span = (now - earliest).days + 1
    if days is None or days >= max_span:
        cutoff, coverage = earliest, -1
    else:
        cutoff, coverage = now - timedelta(days=days), days
    paths, metadata, discovery_errors = _discover(roots)
    stats = dict(discovered=len(paths), parsed=0, cache_hits=0, candidates=0,
                 parse_errors=0, all_parse_errors=0, read_errors=0, incomplete=0,
                 root_errors=discovery_errors['root_errors'], metadata_errors=discovery_errors['metadata_errors'],
                 deleted=0, excluded=0)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cache_dir, 0o700)
    dbfile = cache_dir / 'thread_cache.sqlite3'
    db = sqlite3.connect(dbfile)
    os.chmod(dbfile, 0o600)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, inode INTEGER, size INTEGER, mtime_ns INTEGER, version INTEGER, candidate TEXT, errors INTEGER, incomplete INTEGER, seen INTEGER, window_days INTEGER)')
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE files SET seen=0')
        for key, (path, profile) in sorted(paths.items()):
            try:
                st = path.stat()
            except OSError:
                stats['read_errors'] += 1
                continue
            fp = (st.st_ino, st.st_size, st.st_mtime_ns, PARSER_VERSION)
            if any(ident == metadata.get(key, {}).get('id') or ident in path.stem for ident in (exclude_ids or set())):
                db.execute('''INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET inode=excluded.inode,size=excluded.size,mtime_ns=excluded.mtime_ns,version=excluded.version,candidate=NULL,errors=0,incomplete=0,seen=1,window_days=0''', (key, *fp, None, 0, 0, 1, 0))
                stats['excluded'] += 1
                continue
            row = db.execute('SELECT inode,size,mtime_ns,version,candidate,errors,incomplete,window_days FROM files WHERE path=?', (key,)).fetchone()
            if row and tuple(row[:4]) == fp and row[4]:
                candidate, errors, incomplete = json.loads(row[4]), row[5], row[6]
                stats['cache_hits'] += 1
            elif row and tuple(row[:4]) == fp and (row[7] == -1 or (coverage != -1 and row[7] >= coverage)) and not row[4]:
                candidate, errors, incomplete = None, row[5], row[6]
                stats['cache_hits'] += 1
            else:
                try:
                    candidate, errors, incomplete = _parse(path, metadata.get(key, {}).get('title'), profile)
                    stats['parsed'] += 1
                except (OSError, UnicodeError, ValueError):
                    stats['read_errors'] += 1
                    continue
            if candidate:
                meta = metadata.get(key, {})
                if meta.get('title') and candidate['title'] != meta['title']:
                    candidate['title'] = meta['title']
                    for part in candidate['parts']:
                        part['title'] = meta['title']
                    candidate['content_hash'] = hashlib.sha256(json.dumps(candidate['parts'], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            stats['all_parse_errors'] += errors
            activity = _date(candidate['last_activity']) if candidate else None
            eligible = bool(activity and cutoff <= activity <= now)
            if eligible:
                stats['parse_errors'] += errors
                stats['incomplete'] += int(incomplete)
            # Keep payload admitted by a prior broader search when narrowing.
            # Initial narrow scans still retain only in-window payloads.
            previously_cached = bool(row and tuple(row[:4]) == fp and row[4])
            cached = candidate if activity and activity <= now and (eligible or previously_cached) else None
            prior_coverage = row[7] if row and tuple(row[:4]) == fp else 0
            stored_coverage = -1 if -1 in (coverage, prior_coverage) else max(coverage, prior_coverage)
            db.execute('''INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET inode=excluded.inode,size=excluded.size,mtime_ns=excluded.mtime_ns,version=excluded.version,candidate=excluded.candidate,errors=excluded.errors,incomplete=excluded.incomplete,seen=1,window_days=excluded.window_days''',
                       (key, *fp, json.dumps(cached, ensure_ascii=False) if cached else None, errors, int(incomplete), 1, stored_coverage))
        stats['deleted'] = db.execute('SELECT count(*) FROM files WHERE seen=0').fetchone()[0]
        db.execute('DELETE FROM files WHERE seen=0')
        selected = {}
        for (raw,) in db.execute('SELECT candidate FROM files WHERE candidate IS NOT NULL'):
            item = json.loads(raw)
            if not (_date(item['last_activity']) and cutoff <= _date(item['last_activity']) <= now):
                continue
            if item['id'] in (exclude_ids or set()):
                stats['excluded'] += 1
                continue
            old = selected.get(item['id'])
            rank = lambda v: (sum(len(e['user']) + len(e['assistant']) for p in v['parts'] for e in p['exchanges']), v['last_activity'], v['source_file'])
            if old is None or rank(item) > rank(old):
                selected[item['id']] = item
        db.commit()
        result = sorted(selected.values(), key=lambda x: (x['last_activity'], x['id']), reverse=True)
        stats['candidates'] = len(result)
        return result, stats
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
