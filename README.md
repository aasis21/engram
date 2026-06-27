# Engram

**Index your VS Code Copilot Chat history into a local, queryable SQLite database.**

Engram turns the thousands of chat-session files VS Code writes to disk into a
single SQLite database shaped just like GitHub Copilot CLI's own
`session-store.db` — so your editor conversations become searchable with plain
SQL and full-text search (FTS5). It runs **incrementally** every few minutes via a
Windows Scheduled Task, reparsing only the files that changed since the last run.

> An *engram* is the physical trace a memory leaves in the brain. This is the
> trace your chats leave on disk — made searchable. Sibling project to
> [`aasis21/anya`](https://github.com/aasis21/anya).

---

## Why

VS Code stores Copilot Chat sessions as per-workspace files under
`%APPDATA%\Code\User\workspaceStorage\<hash>\chatSessions\`. There can be
thousands of them, totalling many GB, in two different on-disk formats. They're
effectively write-only: hard to search, impossible to query across workspaces.

Engram consolidates all of it into one DB you can `SELECT` from.

## Quick start

Requires **Python 3.8+** on PATH. From this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

That will:
1. Copy Engram to `%LOCALAPPDATA%\Engram`.
2. Run an initial full index (a few minutes the first time).
3. Register a hidden scheduled task **"Engram Indexer"** that re-indexes every 10 minutes.

Then query anytime:

```powershell
python "%LOCALAPPDATA%\Engram\engram.py" query "service bus retry"
python "%LOCALAPPDATA%\Engram\engram.py" status
```

### Install options

```powershell
.\install.ps1 -Interval 5                 # run every 5 minutes
.\install.ps1 -InstallDir D:\Tools\Engram # custom location
.\install.ps1 -NoSchedule                 # install + index, no task
.\install.ps1 -NoInitialIndex             # register task, skip first index
```

### Uninstall

```powershell
.\uninstall.ps1               # remove the scheduled task, keep the database
.\uninstall.ps1 -RemoveData   # remove the task AND delete the database/files
```

## Usage (CLI)

```text
python engram.py index            # incremental index (default; only changed files)
python engram.py index --full     # reparse everything, ignore the watermark
python engram.py reindex          # alias for `index --full`
python engram.py status           # show state + recent runs
python engram.py query "<text>"   # full-text search across all indexed chats
```

`query` ranks results with FTS5 and prints the session title, repository, date,
session id, and a highlighted snippet.

## How incremental indexing works

The database tracks its own state in an `index_state` table:

- `watermark_mtime` — the newest source-file modification time processed so far.
- Each run scans every chat file but only reparses those with `mtime` newer than
  the watermark (minus a small overlap), then advances the watermark.
- A full audit row per run is written to `index_runs` (scanned / changed /
  indexed / skipped / failed counts, duration, errors).

So the first run is a full pass; every run after that processes only what changed
— typically a handful of files in well under a second.

## Database schema

Mirrors Copilot CLI's `session-store.db`, plus state/audit tables:

| Table | Purpose |
|-------|---------|
| `sessions` | one row per chat session (id, repository, cwd, summary, timestamps, source format/file) |
| `turns` | one row per request/response turn (user_message, assistant_response, timestamp) |
| `session_files` | files referenced/edited within a session |
| `session_refs` | tool invocations and other references |
| `search_index` | FTS5 virtual table over user + assistant text |
| `index_state` | key/value state: schema version, watermark, totals, last-run info |
| `index_runs` | per-run audit log |

Open it with any SQLite tool:

```powershell
sqlite3 "%LOCALAPPDATA%\Engram\engram.db" "SELECT repository, COUNT(*) FROM sessions GROUP BY repository ORDER BY 2 DESC LIMIT 15;"
```

## Configuration

`config.json` (next to `engram.py`, or in `%LOCALAPPDATA%\Engram`) overrides
defaults. `null` means "use the built-in default".

| Key | Default | Meaning |
|-----|---------|---------|
| `db_path` | `%LOCALAPPDATA%\Engram\engram.db` | output database |
| `workspace_storage` | `%APPDATA%\Code\User\workspaceStorage` | VS Code chat root |
| `extra_workspace_storage` | `[]` | extra roots to scan (e.g. VS Code Insiders) |
| `max_file_mb` | `120` | skip files larger than this (memory safety) |
| `user_truncate` | `1000` | max chars stored per user message |
| `assistant_truncate` | `5000` | max chars stored per assistant response |
| `watermark_overlap_seconds` | `3` | re-scan overlap to avoid missing same-second writes |

Env-var overrides: `ENGRAM_DB`, `ENGRAM_WORKSPACE_STORAGE`, `ENGRAM_MAX_FILE_MB`,
`ENGRAM_CONFIG`.

## Notes

- **Read-only** with respect to VS Code: Engram only reads the chat files.
- Zero third-party dependencies — pure Python standard library.
- Sessions appearing in multiple files (e.g. both `.json` and `.jsonl`) are
  de-duplicated by session id; the newest copy wins.
- Very large agent logs above `max_file_mb` are skipped and counted in the run
  audit rather than blowing up memory.

## License

MIT
