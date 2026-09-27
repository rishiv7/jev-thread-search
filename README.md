# Jev Thread Search

Find a past Codex conversation by describing what you remember. Jev Thread Search scans local Codex conversation metadata and selected first and last exchanges, then ranks likely matches and gives you commands to resume them.

It searches **Codex conversation history only**. It does not search Claude Code transcripts.

## Install in Codex

Add this GitHub repository as a plugin marketplace source:

```sh
codex plugin marketplace add rishiv7/jev-thread-search
```

Then open the Plugins directory in the Codex app, select the **Jev Thread Search** marketplace, and install the plugin. Plugin availability and install controls may depend on the Codex surface and account.

## Configure your API key

Search ranking calls TypeSafe's Jev API. **This repository contains no API key.** Each person who runs searches needs their own TypeSafe API key, stored locally. Configure it from a terminal:

After installing, locate the bundled command and configure the key. The plugin cache path includes its version, so this finds the installed copy:

```sh
THREAD_SEARCH_CLI="$(find ~/.codex/plugins/cache/jev-thread-search/jev-thread-search -path '*/skills/jev-thread-search/scripts/thread_search.py' -print -quit)"
python3 "$THREAD_SEARCH_CLI" set-key
python3 "$THREAD_SEARCH_CLI" status
```

`set-key` prompts without echoing the key and saves it outside the project at `~/.config/codex-thread-search/api-key` with owner-only permissions. You can instead provide `TYPESAFE_API_KEY` or `TYPESAFE_API_KEY_FILE` in your local environment. Never commit a key or put it in the repository.

Search requests send the query, thread titles, model metadata, and selected first and last conversation excerpts to TypeSafe for ranking. Local excerpts and ranking results are cached outside the project in `~/.cache/codex-thread-search`.

## Search from a terminal

After installing the plugin, you can also run the bundled command directly:

```sh
python3 "$THREAD_SEARCH_CLI" search --query 'the project where I added export support'
```

The default search window is the last 30 days. Widen it or search all available local history with `--days N` or `--all`:

```sh
python3 "$THREAD_SEARCH_CLI" search --days 90 --query 'the project where I added export support'
python3 "$THREAD_SEARCH_CLI" search --all --query 'the project where I added export support'
```

Use `--top N` to change the number of results, `--exclude-thread ID` to hide a candidate, and `--root PATH` to scan another Codex profile. `refresh` updates the local cache without ranking. `status` reports setup without revealing credentials.

## What to expect

- Requires Python 3.10 or newer; uses only the standard library.
- Searches local Codex data. It cannot find conversations that are not stored on the machine.
- Uses titles and selected first and last exchanges, not every message.
- Returns a shortlist, not a guaranteed match. Candidates are ranked relative to one another.

## Development

The plugin package is in `plugins/jev-thread-search/`. The skill source and scripts are in `plugins/jev-thread-search/skills/jev-thread-search/`.

Run the test suite with:

```sh
python3 -m unittest discover -s tests
```
