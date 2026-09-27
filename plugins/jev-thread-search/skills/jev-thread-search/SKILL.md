---
name: jev-thread-search
description: Find or recover a previous local Codex conversation by topic, request, or remembered work. Uses deterministic cached thread extraction and Jev to return five ranked threads with resume commands, without reading transcript contents into Codex context.
---

Use this capability whenever the user asks to find a previous Codex thread or says a returned thread was not the right one.

Run one command, passing the user's search wording unchanged:

```bash
python3 '<skill-directory>/scripts/thread_search.py' search --query 'the user’s search wording'
```

Replace `<skill-directory>` with this skill's installed directory. Quote shell arguments correctly. For difficult quoting, pass the query through stdin using `--query-stdin`, or write it to a temporary file and use `--query-file`. Below, `codex-thread-search` means this same Python command; no executable wrapper needs to be installed.

The command automatically refreshes a local incremental cache, considers threads active in the last 30 days from configured Codex profiles, and returns five matches ordered by Jev Choice comparisons. Every eligible thread enters a size-bounded Choice batch; Jev shortlists candidates within each batch and ranks the final shortlist. Independent Jev scores are not used to discard threads. Batch shortlisting can miss a relevant thread; the five results are candidates to inspect, not a guaranteed global ordering. For a thread whose selected exchanges require multiple parts, Jev evaluates all parts and picks its strongest evidence for comparisons. It excludes the calling thread using `CODEX_THREAD_ID`. Use `--days N` when the user requests another date range, `--top N` to change the result count, and `--exclude-thread ID` when a user rejects a candidate. It scans every eligible thread's title and first/last exchanges; middle messages and tool results are not searched. Repeat identical searches reuse cached Jev scores; changed threads are updated automatically.

Keep all transcript content inside this program and Jev. Do not open session files, cache databases, old export files, generated requests, or raw provider responses. Do not grep transcripts, manually shortlist by title, rewrite the query into keywords, or rank threads yourself. The CLI prints only result metadata and numeric coverage/usage statistics. Only inspect program source when maintaining this capability.

Present the returned ranking, normally all five results, with titles and resume commands. The ordering comes from relative Choice comparisons. The separately reported scores estimate evidence strength and need not decrease with rank; they are not a probability distribution over the corpus. If `no_clear_match` is true, say none is a clear match and still show the alternatives. Report incomplete coverage if `parse_errors`, `read_errors`, `incomplete`, `root_errors`, or `metadata_errors` are nonzero. `all_parse_errors` includes older excluded files and does not alone imply current coverage is incomplete. Never invent excerpts or reasons for a match.

The 30-day window is a default, not a limit. Honor a requested lookback with `--days N` (for example, `--days 90` for the last three months or `--days 365` for the last year). Use `--all` for all available history, or when the user asks to search further back without specifying a limit. These options also work with `refresh`; `--all` and `--days` are mutually exclusive. Older threads use the same extraction, caching, size-bounded Jev requests, and five-result workflow. Previously cached excerpts are reused across date windows.

If a query yields no useful match, reuse the command with user-supplied refinements or a wider date range. Do not silently expand to full transcripts.

Credentials are read from `TYPESAFE_API_KEY`, `TYPESAFE_API_KEY_FILE`, or the private local credential file. On an authentication error, ask the user to configure the key locally; never request or print it in chat. `codex-thread-search status` reports configuration without exposing credentials. `codex-thread-search set-key` accepts a key from stdin. If credentials are missing, explain the local setup below; do not request secrets in chat.


## Installation and API key

Requires Python 3.10 or newer on macOS or Linux; uses only the Python standard library. Copy this `jev-thread-search` folder into your Codex skills directory (normally `~/.codex/skills`, or `$CODEX_HOME/skills` if you use a custom profile). Load the skill in Codex, or use the Python command directly.

This package includes no API key, personal configuration, conversation files, or caches. Each recipient needs their own TypeSafe API key. Configure it in a terminal with:

```bash
python3 '<skill-directory>/scripts/thread_search.py' set-key
python3 '<skill-directory>/scripts/thread_search.py' status
```

`set-key` prompts without echoing the key and saves it with owner-only permissions in `~/.config/codex-thread-search/api-key`, outside this skill. It also accepts stdin for automation. Alternatively, provide `TYPESAFE_API_KEY` or `TYPESAFE_API_KEY_FILE` through your local environment. Never ask a user to paste a key into chat.

Searches send the query, thread titles, model metadata, and selected first/last exchanges to the TypeSafe API for Jev ranking. Local cached excerpts and ranking results stay in `~/.cache/codex-thread-search`, outside the package. A 30-day search is the default; `--days N` or `--all` searches further back. These searches cannot recover conversations that are absent from local storage.

By default the program scans `~/.codex` and the active `CODEX_HOME`, if set. Pass `--root /path/to/profile` for another profile; repeat for multiple profiles. This flag overrides the defaults.
