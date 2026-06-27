---
name: engram
description: Search your past Copilot conversations across BOTH local stores in one go — the VS Code Copilot Chat store (session-store-vscode-chat.db, built by Engram) and the Copilot CLI / Forge store (session-store.db). Use when the user asks to find, recall, or recover a previous Copilot conversation — "which chat did I discuss X in", "find the session about <topic>", "search my Copilot history", "what did I work on for <feature>", "show me chats that touched <file>" — or wants to deep-dive a session by id. Read-only, FTS5-backed, ranked by recency, tags every hit as CHAT or CLI. Engram (https://aasis21.github.io/engram/) builds and maintains the Chat database; the CLI store is maintained by Copilot CLI itself.
---

# Engram — Search Copilot history (Chat + CLI, unified)

One read-only recall layer over **both** local Copilot session-store SQLite
databases. Every result is tagged `CHAT` or `CLI` so you know where it came from.

## The two stores

Both live under `~/.copilot/` (override with `COPILOT_SESSION_DIR`):

| Tag | File | Contents | Maintained by |
|-----|------|----------|---------------|
| `chat` | `session-store-vscode-chat.db` | VS Code Copilot Chat sessions (indexed mirror of `%APPDATA%\Code\User\workspaceStorage\<hash>\chatSessions\*`) | **[Engram](https://github.com/aasis21/engram)** — scheduled task, re-indexes every ~10 min |
| `cli` | `session-store.db` | Copilot CLI / Forge sessions + checkpoints | Copilot CLI itself |

Both share the same shape: `sessions`, `turns`, FTS5 `search_index(content, session_id, source_type, source_id)`.

**If the Chat DB is missing**, Engram isn't installed yet:

```powershell
irm https://raw.githubusercontent.com/aasis21/engram/main/install.ps1 | iex
```

The CLI DB needs no setup — Copilot CLI writes it as you use it.

## When to use

- "Which chat / session did I discuss `<topic>` in?"
- "Find my session about the `<feature>` upgrade / a specific incident / a file path."
- "Search my Copilot history for `<keywords>`."
- "Show me what I did in session `769739be`."
- "What did I work on this week in `<repo>`?"

## How to use

The bundled script `scripts/engram_search.py` (Python 3, stdlib only,
read-only) is the recommended entry point. It queries both stores in one pass
using their shared FTS5 index and merges the hits.

When installed via `install.ps1`, this skill lives at
`~\.copilot\skills\engram\` — so the script is at
`~\.copilot\skills\engram\scripts\engram_search.py`.

**Pick the command that matches the question — don't default to `list`:**

| Question | Command |
|----------|---------|
| "How many / how much / what did I do this week?" (reports, standups, totals) | **`stats`** — true totals, never truncated |
| "Which chat did I discuss `<topic>` in?" (topic recall) | **`list`** — ranked matches |
| "Open / what happened in session `<id>`?" | **`show`** — full transcript |

### 1. Find sessions — `list`

```powershell
python "$env:USERPROFILE\.copilot\skills\engram\scripts\engram_search.py" list --query "<keywords>"
```

`--query` is **optional** — omit it to *browse* every session in the time window
(handy for "what did I work on today?"). Add filters to narrow the list:

| Option | Meaning |
|--------|---------|
| `--query "<text>"` | FTS keyword search (words are ANDed). Optional — omit to list all sessions in the window. |
| `--and "<term>"` | Extra term that must ALSO appear (repeatable). |
| `--regex` | Treat `--query`/`--and` as regular expressions (scans turns directly). |
| `--source cli\|chat\|all` | Which store(s) to search (default `all`). |
| `--repo`, `-w "<substr>"` | Keep only sessions whose location (repo/cwd/branch/file) contains the substring. |
| `--today` | Only sessions updated **today** (local time). Overrides `--days`. |
| `--days N` | Only sessions updated within N days (default `30`; `0` = all history). |
| `--limit N` | Max results (default 1000). |
| `--json` | Machine-readable output. |

Examples:

```powershell
# Keyword search
python engram_search.py list --query "net8 upgrade dual targeting"
python engram_search.py list --query "SNAT|socket exhaustion" --regex --days 90
python engram_search.py list --query "recon" -w ModernOrder --source chat

# No keyword — just browse by time
python engram_search.py list --today                  # everything worked on today
python engram_search.py list --today --source chat    # today, Chat store only
python engram_search.py list --days 7 -w ModernOrder  # this week in one repo
python engram_search.py list --limit 10               # 10 most recent (last 30 days)
```

Each hit shows: source tag (`CHAT`/`CLI`), updated time, 8-char id, match count, turn count, title, location, the opening prompt, and a `files` line with the count of files **created** and **edited** in that session (shown only when nonzero).

### 2. Deep-dive a session — `show`

```powershell
python engram_search.py show --session 769739be
```

Accepts a full id or 8-char prefix and searches both stores. Options:

| Option | Meaning |
|--------|---------|
| `--query "<regex>"` | Only show turns matching this regex. |
| `--turn N` | Show just turn N, untruncated. |
| `--full` | Show every turn untruncated. |
| `--source cli\|chat\|all` | Limit the lookup to one store. |

### 3. Count activity — `stats`

```powershell
python engram_search.py stats              # full report, last 30 days, both stores
```

Aggregates how many **sessions** and **turns** match a window/filter and breaks
them down by day, repo/location, and source. Unlike `list`, it is **never**
truncated by `--limit` — it counts the full matched set, so use it whenever you
need an accurate total (e.g. the `list` footer says "limit reached").

| Option | Meaning |
|--------|---------|
| `--query "<text>"` | Count only sessions matching the term (optional). |
| `--and "<term>"` | Extra term that must ALSO appear (repeatable). |
| `--regex` | Treat `--query`/`--and` as regular expressions. |
| `--source cli\|chat\|all` | Which store(s) to count (default `all`). |
| `--repo`, `-w "<substr>"` | Only sessions whose location contains the substring. |
| `--today` | Only today (local time). Overrides `--days`. |
| `--days N` | Window in days (default `30`; `0` = all history). |
| `--by day\|repo\|source` | Narrow which breakdown(s) to show (repeatable). Default: all three. |
| `--json` | Machine-readable output. |

Examples:

```powershell
python engram_search.py stats --days 7              # this week's totals + breakdowns
python engram_search.py stats --today --by repo     # today, just the repo table
python engram_search.py stats --query recon --days 0  # all-time count of "recon" sessions
python engram_search.py stats --days 30 --by day --json
```

**Weekly report / standup recipe** — shape first, then themes:

```powershell
python engram_search.py stats --days 7              # counts by day / repo / source (never truncated)
python engram_search.py list  --days 7 --limit 100  # session titles for the narrative
```

## Recommended flow

1. **Match intent to command first** (see the table under *How to use*):
   `stats` for counts/reports, `list` for topic recall, `show` to read a session.
2. **Reports / "how much / how many" →** run `stats` (`--days N` or `--today`)
   for the true day/repo/source shape, then `list` over the same window to pull
   the titles that become your narrative. Never hand-count `list` output.
3. **Topic recall →** `list --query "<topic>"` at the default 30 days; widen with
   `--days 0` if nothing turns up. Note `CHAT` vs `CLI`; pick the top hit or ask
   which to open.
4. **Deep-dive →** `show --session <id>` (add `--query` to jump to relevant
   turns) and summarize what that session covered.

## Going beyond the script — direct SQL (escape hatch)

The three commands above cover ~80% of recall needs — reach for SQL only for
queries they can't express (custom joins, file-graph lookups, ad-hoc grouping).
Both stores share the same schema: `sessions`, `turns`, `session_files`,
`session_refs`, FTS5 `search_index`.

`session_files` rows are deduped per session (`UNIQUE(session_id, file_path)`),
with `tool_name` recording how the file was touched (`create`/`edit`/`read`,
one row per file at its strongest action).

> **Never write to these files.** Engram's scheduled task owns the Chat DB and
> Copilot CLI owns the CLI DB. Always open with `?mode=ro` (Python) or
> `-readonly` (CLI). Never `INSERT`/`UPDATE`/`DELETE`/`VACUUM`.

```python
import sqlite3, os
db = os.path.expandvars(r"%USERPROFILE%\.copilot\session-store-vscode-chat.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

# FTS keyword search joined back to sessions, most recent first
for sid, repo, ts, hits in con.execute("""
    SELECT s.id, s.repository, s.updated_at, COUNT(*) AS hits
    FROM search_index i JOIN sessions s ON s.id = i.session_id
    WHERE search_index MATCH ?
    GROUP BY s.id ORDER BY s.updated_at DESC LIMIT 20
""", ("kusto AND alert",)):
    print(ts, repo, sid[:8], hits)
```

```powershell
# Top repositories by chat count (Chat DB)
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" `
  "SELECT repository, COUNT(*) FROM sessions GROUP BY repository ORDER BY 2 DESC LIMIT 15;"

# Every chat that referenced a given file
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" `
  "SELECT DISTINCT s.id, s.summary, s.updated_at
   FROM session_files f JOIN sessions s ON s.id = f.session_id
   WHERE f.file_path LIKE '%ReconProcessor%'
   ORDER BY s.updated_at DESC;"

# Union both stores for a single ranked list
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store.db" `
  "ATTACH 'file:$env:USERPROFILE\.copilot\session-store-vscode-chat.db?mode=ro' AS chat;
   SELECT 'cli' AS src, id, updated_at FROM main.sessions
   UNION ALL
   SELECT 'chat' AS src, id, updated_at FROM chat.sessions
   ORDER BY updated_at DESC LIMIT 20;"
```

When unsure of columns, inspect first:

```powershell
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" ".schema sessions"
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" `
  "SELECT name FROM sqlite_master WHERE type='table';"
```

## Notes

- **Read-only**: every database is opened with `?mode=ro`; the tool never writes.
- **Requires** Python 3 and at least one of the two `.db` files present.
- The Chat store's `summary` is often generic ("VS Code Copilot session store");
  rely on the **opening prompt** and match counts to identify the right session.
- FTS keyword search can't do phrases/regex — use `--regex` for exact patterns
  or word boundaries (it scans `turns` directly, slightly slower but precise).
- Dates are UTC ISO-8601; results are ranked most-recent first.
- **Truncation** (Chat only): user messages and assistant responses are stored
  truncated by default (1000 / 5000 chars) to keep the DB compact. For long
  answers, also inspect the raw `.jsonl` under
  `%APPDATA%\Code\User\workspaceStorage\<hash>\chatSessions\` if needed.
