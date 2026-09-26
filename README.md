# Jev Thread Search

Find a previous Codex conversation by describing what you remember. The tool scans local Codex conversation metadata and selected first and last exchanges, then asks TypeSafe's Jev API to rank likely matches. It returns thread titles, dates, scores, and commands to resume them.

## Requirements

- Python 3.10 or newer
- A TypeSafe API key for searches (the key is not included in this project)

The project uses only Python's standard library. It does not include a key, personal configuration, conversation data, or cache files. To run a search, you must supply your own key locally. The key can be read from `TYPESAFE_API_KEY`, `TYPESAFE_API_KEY_FILE`, or `~/.config/codex-thread-search/api-key`; `set-key` saves it outside this project with owner-only permissions. Search requests send the query and selected conversation excerpts to TypeSafe for ranking.

## Install as a Codex skill

Copy this folder into your Codex skills directory, usually `~/.codex/skills/jev-thread-search` (or `$CODEX_HOME/skills/jev-thread-search` for a custom profile). Restart or reload Codex so it can discover the skill.

## Configure credentials

Run the following command in a terminal. The key prompt does not echo what you type:

```sh
python3 scripts/thread_search.py set-key
python3 scripts/thread_search.py status
```

Alternatively, set `TYPESAFE_API_KEY` or `TYPESAFE_API_KEY_FILE` in your local environment. Never commit credentials or put them in this directory.

## Search

```sh
python3 scripts/thread_search.py search --query 'the project where I added export support'
```

By default, searches include conversations active in the last 30 days. Widen the range or search all local history with:

```sh
python3 scripts/thread_search.py search --days 90 --query 'the project where I added export support'
python3 scripts/thread_search.py search --all --query 'the project where I added export support'
```

Use `--top N` to return a different number of candidates, `--exclude-thread ID` to hide a candidate, and `--root PATH` to scan another Codex profile. `refresh` updates the local cache without ranking; `status` reports local configuration without revealing credentials.

The search is limited to titles and selected first and last exchanges; it does not search every message. Candidates are ranked relative to one another, so the result is a shortlist rather than a guarantee. Local excerpts and ranking data are cached under `~/.cache/codex-thread-search`, outside this project.

## Project layout

- `scripts/thread_search.py`: command line interface and local credential handling
- `scripts/thread_cache.py`: local conversation discovery and cache
- `scripts/jev_ranker.py`: Jev API requests and relevance scores
- `scripts/choice_ranker.py`: pairwise ranking of candidates
- `tests/`: automated tests
- `SKILL.md`: Codex skill instructions

## Tests

Run the test suite with:

```sh
python3 -m unittest discover -s tests
```
