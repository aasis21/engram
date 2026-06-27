---
name: engram
description: Search your VS Code Copilot Chat history — every chat you've ever had with Copilot inside VS Code, across every workspace, in one place. Use when the user wants to find, recall, recover, or deep-dive a past chat — "which chat did I discuss X in", "find the session about <topic>", "search my Copilot Chat history", "what did I ask Copilot about <feature>", "show me chats that touched <file>". Read-only, FTS5-backed, ranks by relevance and recency. Requires Engram (https://aasis21.github.io/engram/) to be installed; it builds and maintains the database from VS Code's per-workspace chat files.
---

# Engram — Search VS Code Copilot Chat history

Engram consolidates every VS Code Copilot Chat session (across every workspace) into a
single local SQLite database with full-text search. This skill is the recall layer on top.

## What you're querying

| | |
|---|---|
| **Database** | `%USERPROFILE%\.copilot\session-store-vscode-chat.db` (Windows) · `~/.copilot/session-store-vscode-chat.db` (POSIX) |
| **Built by** | [Engram](https://github.com/aasis21/engram) — a Windows Scheduled Task that re-indexes every ~10 min |
| **Source files** | `%APPDATA%\Code\User\workspaceStorage\<hash>\chatSessions\*` |
| **Schema** | `sessions`, `turns`, `session_files`, `session_refs`, FTS5 `search_index` (mirrors Copilot CLI's `session-store.db`) |

If the database is missing, Engram isn't installed. Direct the user to:

```powershell
irm https://raw.githubusercontent.com/aasis21/engram/main/install.ps1 | iex
```

## When to use

- "Which chat / session did I discuss `<topic>` in?"
- "Find my chat about the `<feature>` upgrade / a specific incident / a file path."
- "Search my Copilot Chat history for `<keywords>`."
- "Show me what I asked Copilot in session `<id-prefix>`."
- "What workspaces have I used Copilot Chat in this month?"

## How to use

Two layers. Start with the CLI for keyword search; drop to SQL for anything richer.

### 1. Keyword search — `engram.py query`

```powershell
python "$env:LOCALAPPDATA\Engram\engram.py" query "<keywords>"
python "$env:LOCALAPPDATA\Engram\engram.py" query "<keywords>" --limit 10
```

Returns ranked FTS5 hits with the session title, repository, date, 8-char id, and a
highlighted snippet. Words are ANDed by default; use FTS syntax for `OR`, phrases, or
prefix matching:

```powershell
python "$env:LOCALAPPDATA\Engram\engram.py" query "service bus retry"
python "$env:LOCALAPPDATA\Engram\engram.py" query "SNAT OR socket"
python "$env:LOCALAPPDATA\Engram\engram.py" query "\"managed identity\""
python "$env:LOCALAPPDATA\Engram\engram.py" query "kusto*"
```

Check indexer health any time:

```powershell
python "$env:LOCALAPPDATA\Engram\engram.py" status
```

(Shows watermark, last-run mode/status, totals, and recent run audit rows.)

### 2. Anything richer — direct SQL (read-only)

The CLI covers ~80% of use cases. For aggregations, joins, time bucketing, file-graph
queries, or "all chats that touched X" — open the SQLite directly. **Always open
read-only** (`?mode=ro` URI in Python, `-readonly` with the CLI). Engram's scheduled
indexer writes to this DB; never `INSERT`/`UPDATE`/`DELETE`/`VACUUM` it.

```python
import sqlite3, os
db = os.path.expandvars(r"%USERPROFILE%\.copilot\session-store-vscode-chat.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

# FTS keyword search joined back to sessions, most recent first
for sid, repo, ts, hits in con.execute("""
    SELECT s.id, s.repository, s.updated_at, COUNT(*) AS hits
    FROM search_index i
    JOIN sessions s ON s.id = i.session_id
    WHERE search_index MATCH ?
    GROUP BY s.id ORDER BY s.updated_at DESC LIMIT 20
""", ("kusto AND alert",)):
    print(ts, repo, sid[:8], hits)
```

```powershell
# Top repositories by chat count
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" `
  "SELECT repository, COUNT(*) FROM sessions GROUP BY repository ORDER BY 2 DESC LIMIT 15;"

# Every chat that referenced a given file
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" `
  "SELECT DISTINCT s.id, s.summary, s.updated_at
   FROM session_files f JOIN sessions s ON s.id = f.session_id
   WHERE f.file_path LIKE '%ReconProcessor%'
   ORDER BY s.updated_at DESC;"

# Chat activity by day in the last 30 days
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" `
  "SELECT substr(updated_at,1,10) AS day, COUNT(*)
   FROM sessions
   WHERE updated_at > datetime('now','-30 days')
   GROUP BY day ORDER BY day DESC;"
```

When unsure of columns, inspect first:

```powershell
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" ".schema sessions"
sqlite3 -readonly "$env:USERPROFILE\.copilot\session-store-vscode-chat.db" `
  "SELECT name FROM sqlite_master WHERE type='table';"
```

## Recommended flow

1. Run `engram.py query "<topic>"`. Start narrow; widen with `OR` or wildcard if no hits.
2. Present the ranked matches — repository, date, snippet. Ask which to open, or pick the top hit.
3. Pull the full session with SQL by id:
   ```sql
   SELECT user_message, assistant_response, timestamp
   FROM turns WHERE session_id = '<full-id>' ORDER BY turn_index;
   ```
4. Summarize what that chat covered.

## Notes

- **Read-only**: never write to `session-store-vscode-chat.db`. Engram's scheduled task owns it.
- **Watermark indexing**: changes appear in the DB within ~10 minutes (configurable on install).
- **Truncation**: user messages and assistant responses are stored truncated by default
  (1000 / 5000 chars). For long answers, also inspect the source `.jsonl` under
  `%APPDATA%\Code\User\workspaceStorage\<hash>\chatSessions\` if needed.
- **Dedup**: sessions appearing in multiple on-disk formats are de-duplicated by id — newest wins.
- Sibling: Copilot CLI's own `session-store.db` lives next to this one and shares the
  schema; querying both stores at once is a small extension of the SQL above (`UNION ALL`).
