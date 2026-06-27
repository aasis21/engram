#!/usr/bin/env python3
"""
Engram - index VS Code Copilot Chat sessions into a local SQLite database.

A reusable, zero-dependency (stdlib-only) indexer that mirrors how GitHub
Copilot CLI stores its `session-store.db`, so your editor chat history becomes
queryable with plain SQL / FTS5.

Runs incrementally: only files modified since the last run are reparsed, tracked
via a watermark stored in the database itself (the `index_state` table). A full
audit of every run is kept in `index_runs`.

Usage:
    python engram.py index            # incremental index (default)
    python engram.py index --full     # reparse everything, ignore watermark
    python engram.py reindex          # alias for `index --full`
    python engram.py status           # show state + recent runs
    python engram.py query "<text>"   # full-text search over indexed chats

Config resolution (first found wins):
    1. --config <path>
    2. ENGRAM_CONFIG env var
    3. config.json next to this script
    4. %LOCALAPPDATA%\\Engram\\config.json
Any individual value can also be overridden by env vars:
    ENGRAM_DB, ENGRAM_WORKSPACE_STORAGE, ENGRAM_MAX_FILE_MB
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import unquote

SCHEMA_VERSION = 1

# User-prompt / assistant-response truncation, mirroring Copilot CLI's caps.
DEFAULT_USER_TRUNC = 1000
DEFAULT_ASSISTANT_TRUNC = 5000

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def default_config() -> dict:
    appdata = os.environ.get("APPDATA", "")
    userprofile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    return {
        # Where VS Code stores per-workspace chat sessions.
        "workspace_storage": os.path.join(appdata, "Code", "User", "workspaceStorage"),
        # Output database. Lives next to Copilot CLI's session-store.db so both
        # session stores sit side by side under ~/.copilot.
        "db_path": os.path.join(userprofile, ".copilot", "session-store-vscode-chat.db"),
        # Skip files larger than this (memory safety for runaway agent logs).
        "max_file_mb": 120,
        # Truncation caps for stored text.
        "user_truncate": DEFAULT_USER_TRUNC,
        "assistant_truncate": DEFAULT_ASSISTANT_TRUNC,
        # Re-scan overlap (seconds) subtracted from the watermark to avoid
        # missing files written in the same second as the last run.
        "watermark_overlap_seconds": 3,
        # Extra VS Code variants to scan (e.g. Insiders). Each is a
        # workspaceStorage path. Leave empty to only scan workspace_storage.
        "extra_workspace_storage": [],
    }


def load_config(cli_path):
    cfg = default_config()
    candidates = []
    if cli_path:
        candidates.append(cli_path)
    if os.environ.get("ENGRAM_CONFIG"):
        candidates.append(os.environ["ENGRAM_CONFIG"])
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "config.json"))
    candidates.append(os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Engram", "config.json"))
    for c in candidates:
        if c and os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as fh:
                    user = json.load(fh)
                cfg.update({k: v for k, v in user.items() if v is not None})
                cfg["_config_source"] = c
            except Exception as e:  # defensive
                print(f"[engram] warning: failed to read config {c}: {e}", file=sys.stderr)
            break
    # Env overrides.
    if os.environ.get("ENGRAM_DB"):
        cfg["db_path"] = os.environ["ENGRAM_DB"]
    if os.environ.get("ENGRAM_WORKSPACE_STORAGE"):
        cfg["workspace_storage"] = os.environ["ENGRAM_WORKSPACE_STORAGE"]
    if os.environ.get("ENGRAM_MAX_FILE_MB"):
        try:
            cfg["max_file_mb"] = float(os.environ["ENGRAM_MAX_FILE_MB"])
        except ValueError:
            pass
    return cfg


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    cwd           TEXT,
    repository    TEXT,
    branch        TEXT,
    summary       TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    source_format TEXT,
    workspace_hash TEXT,
    source_file   TEXT,
    file_mtime    REAL,
    turn_count    INTEGER,
    responder     TEXT,
    initial_location TEXT,
    indexed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repository);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd  ON sessions(cwd);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);

CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_index    INTEGER NOT NULL,
    user_message  TEXT,
    assistant_response TEXT,
    timestamp     TEXT,
    request_id    TEXT,
    model_id      TEXT,
    agent         TEXT,
    UNIQUE(session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

CREATE TABLE IF NOT EXISTS session_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL,
    tool_name     TEXT,
    turn_index    INTEGER,
    first_seen_at TEXT,
    UNIQUE(session_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_session_files_path ON session_files(file_path);

CREATE TABLE IF NOT EXISTS session_refs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ref_type      TEXT NOT NULL,
    ref_value     TEXT NOT NULL,
    turn_index    INTEGER,
    created_at    TEXT,
    UNIQUE(session_id, ref_type, ref_value)
);
CREATE INDEX IF NOT EXISTS idx_session_refs_type_value ON session_refs(ref_type, ref_value);

-- Key/value bookkeeping: the "state" table the scheduler relies on.
CREATE TABLE IF NOT EXISTS index_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Per-run audit log (how many runs done, what each did).
CREATE TABLE IF NOT EXISTS index_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT,
    mode            TEXT,
    files_scanned   INTEGER,
    files_changed   INTEGER,
    files_indexed   INTEGER,
    files_skipped   INTEGER,
    files_failed    INTEGER,
    sessions_upserted INTEGER,
    turns_upserted  INTEGER,
    duration_ms     INTEGER,
    watermark_before REAL,
    watermark_after  REAL,
    error           TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    content,
    session_id UNINDEXED,
    source_type UNINDEXED,
    source_id UNINDEXED
);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn):
    conn.executescript(DDL)
    cur = conn.execute("SELECT value FROM index_state WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        state_set(conn, "schema_version", str(SCHEMA_VERSION))
        state_set(conn, "watermark_mtime", "0")
        state_set(conn, "runs_total", "0")
        state_set(conn, "sessions_indexed_total", "0")
        state_set(conn, "turns_indexed_total", "0")
        state_set(conn, "created_at", now_iso())
    conn.commit()


def state_get(conn, key, default=None):
    cur = conn.execute("SELECT value FROM index_state WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def state_set(conn, key, value):
    conn.execute(
        "INSERT INTO index_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_ms_to_iso(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def truncate(text, limit):
    if text is None:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\u2026"


def file_uri_to_path(uri):
    """Decode a VS Code file:/// URI (percent-encoded) to a Windows path."""
    if not isinstance(uri, str):
        return ""
    if uri.startswith("file:///"):
        p = unquote(uri[len("file:///"):])
        if len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        return p.replace("/", "\\")
    return uri


# --------------------------------------------------------------------------- #
# JSONL edit-log replay
# --------------------------------------------------------------------------- #

def _ensure_index(arr, idx):
    while len(arr) <= idx:
        arr.append(None)


def set_path(model, path, value):
    """kind==1: set value at the given key path (list of str/int keys)."""
    if not path:
        return value if isinstance(value, dict) else model
    cur = model
    for i, key in enumerate(path[:-1]):
        nxt = path[i + 1]
        want_list = isinstance(nxt, int)
        if isinstance(key, int):
            if not isinstance(cur, list):
                return model
            _ensure_index(cur, key)
            if cur[key] is None or not isinstance(cur[key], (dict, list)):
                cur[key] = [] if want_list else {}
            cur = cur[key]
        else:
            if not isinstance(cur, dict):
                return model
            if key not in cur or not isinstance(cur[key], (dict, list)):
                cur[key] = [] if want_list else {}
            cur = cur[key]
    last = path[-1]
    if isinstance(last, int):
        if isinstance(cur, list):
            _ensure_index(cur, last)
            cur[last] = value
    else:
        if isinstance(cur, dict):
            cur[last] = value
    return model


def append_path(model, path, value, idx):
    """kind==2: append/splice list `value` into the array at `path`."""
    cur = model
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or key >= len(cur):
                return model
            cur = cur[key]
        else:
            if not isinstance(cur, dict):
                return model
            if key not in cur:
                cur[key] = []
            cur = cur[key]
    if not isinstance(cur, list):
        return model
    items = value if isinstance(value, list) else [value]
    if isinstance(idx, int) and 0 <= idx <= len(cur):
        cur[idx:idx] = items
    else:
        cur.extend(items)
    return model


def replay_jsonl(path):
    """Reconstruct the final chat model from a VS Code edit-log .jsonl file."""
    model = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = o.get("kind")
            if kind == 0:
                v = o.get("v")
                model = v if isinstance(v, dict) else {}
            elif kind == 1:
                model = set_path(model, o.get("k") or [], o.get("v"))
            elif kind == 2:
                model = append_path(model, o.get("k") or [], o.get("v"), o.get("i"))
    return model


# --------------------------------------------------------------------------- #
# Extraction from a chat model (common to both formats)
# --------------------------------------------------------------------------- #

def extract_user_text(message):
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            return "".join(
                p.get("text", "") for p in parts
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            )
        if isinstance(message.get("text"), str):
            return message["text"]
    return ""


def extract_assistant_text(response):
    """Pull human-readable prose out of the typed response-part list."""
    if isinstance(response, str):
        return response
    if not isinstance(response, list):
        return ""
    chunks = []
    for item in response:
        if not isinstance(item, dict):
            if isinstance(item, str):
                chunks.append(item)
            continue
        kind = item.get("kind")
        if kind is None and isinstance(item.get("value"), str):
            chunks.append(item["value"])            # markdown prose
        elif kind == "progressTaskSerialized" and isinstance(item.get("content"), dict):
            v = item["content"].get("value")
            if isinstance(v, str):
                chunks.append(v)
    return "\n".join(c for c in chunks if c)


def _collect_uri(obj, out):
    """Best-effort pull of a filesystem path out of various reference shapes."""
    if not isinstance(obj, dict):
        return
    if isinstance(obj.get("fsPath"), str):
        out.append(obj["fsPath"])
        return
    if isinstance(obj.get("external"), str) and obj["external"].startswith("file:"):
        out.append(file_uri_to_path(obj["external"]))
        return
    if isinstance(obj.get("path"), str) and ":" in obj.get("path", ""):
        out.append(obj["path"].lstrip("/").replace("/", "\\"))


def extract_files_and_refs(request):
    files = set()
    refs = set()

    agent = request.get("agent")
    if isinstance(agent, dict):
        name = agent.get("id") or agent.get("name")
        if name:
            refs.add(("agent", str(name)))
    if request.get("modelId"):
        refs.add(("model", str(request["modelId"])))

    for cr in request.get("contentReferences") or []:
        if isinstance(cr, dict):
            ref = cr.get("reference")
            tmp = []
            _collect_uri(ref if isinstance(ref, dict) else cr, tmp)
            for f in tmp:
                if f:
                    files.add(f)

    for item in request.get("response") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "toolInvocationSerialized":
            tid = item.get("toolId") or item.get("toolName")
            if tid:
                refs.add(("tool", str(tid)))
        elif kind == "prepareToolInvocation":
            if item.get("toolName"):
                refs.add(("tool", str(item["toolName"])))
        elif kind in ("codeblockUri", "textEditGroup"):
            uri = item.get("uri")
            tmp = []
            _collect_uri(uri if isinstance(uri, dict) else {"external": uri}, tmp)
            for f in tmp:
                if f:
                    files.add(f)
        elif kind == "inlineReference":
            ir = item.get("inlineReference")
            tmp = []
            _collect_uri(ir if isinstance(ir, dict) else {}, tmp)
            for f in tmp:
                if f:
                    files.add(f)
    return files, refs


def parse_chat_model(model):
    """Turn a reconstructed chat model into a normalized session record."""
    if not isinstance(model, dict):
        return None
    session_id = model.get("sessionId")
    requests = model.get("requests")
    if not session_id or not isinstance(requests, list):
        return None

    turns = []
    files = {}        # file_path -> first turn index
    refs = {}
    last_ts_iso = None
    for idx, req in enumerate(requests):
        if not isinstance(req, dict):
            continue
        user = extract_user_text(req.get("message"))
        assistant = extract_assistant_text(req.get("response"))
        ts = epoch_ms_to_iso(req.get("timestamp"))
        if ts:
            last_ts_iso = ts
        model_id = req.get("modelId")
        agent = req.get("agent")
        agent_name = agent.get("id") if isinstance(agent, dict) else None
        turns.append({
            "turn_index": idx,
            "user_message": user,
            "assistant_response": assistant,
            "timestamp": ts,
            "request_id": req.get("requestId"),
            "model_id": model_id,
            "agent": agent_name,
        })
        f, r = extract_files_and_refs(req)
        for fp in f:
            files.setdefault(fp, idx)
        for rk in r:
            refs.setdefault(rk, idx)

    return {
        "id": session_id,
        "summary": model.get("customTitle"),
        "created_at": epoch_ms_to_iso(model.get("creationDate")),
        "updated_at": epoch_ms_to_iso(model.get("lastMessageDate")) or last_ts_iso,
        "responder": model.get("responderUsername"),
        "initial_location": model.get("initialLocation"),
        "turns": turns,
        "files": files,
        "refs": refs,
    }


# --------------------------------------------------------------------------- #
# Workspace mapping
# --------------------------------------------------------------------------- #

def build_workspace_map(storage_root):
    """hash folder -> {cwd, repository}."""
    out = {}
    if not os.path.isdir(storage_root):
        return out
    for entry in os.scandir(storage_root):
        if not entry.is_dir():
            continue
        wj = os.path.join(entry.path, "workspace.json")
        cwd = None
        if os.path.isfile(wj):
            try:
                with open(wj, "r", encoding="utf-8") as fh:
                    wd = json.load(fh)
                uri = wd.get("folder") or wd.get("workspace")
                if uri:
                    cwd = file_uri_to_path(uri)
            except Exception:
                cwd = None
        repo = os.path.basename(cwd.rstrip("\\/")) if cwd else None
        out[entry.name] = {"cwd": cwd, "repository": repo}
    return out


def iter_chat_files(storage_roots):
    """Yield (hash, full_path, mtime, size, ext) for every chat session file."""
    for root in storage_roots:
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            if not entry.is_dir():
                continue
            chat_dir = os.path.join(entry.path, "chatSessions")
            if not os.path.isdir(chat_dir):
                continue
            for f in os.scandir(chat_dir):
                if not f.is_file():
                    continue
                if not (f.name.endswith(".json") or f.name.endswith(".jsonl")):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                ext = ".jsonl" if f.name.endswith(".jsonl") else ".json"
                yield entry.name, f.path, st.st_mtime, st.st_size, ext


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #

def upsert_session(conn, rec, meta, source_format, ws_hash, source_file, mtime, cfg):
    sid = rec["id"]
    # Replace child rows so a re-parsed (grown) session stays clean.
    conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM session_files WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM session_refs WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM search_index WHERE session_id=?", (sid,))

    conn.execute(
        """INSERT INTO sessions
           (id, cwd, repository, branch, summary, created_at, updated_at,
            source_format, workspace_hash, source_file, file_mtime, turn_count,
            responder, initial_location, indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             cwd=excluded.cwd, repository=excluded.repository,
             summary=excluded.summary, created_at=excluded.created_at,
             updated_at=excluded.updated_at, source_format=excluded.source_format,
             workspace_hash=excluded.workspace_hash, source_file=excluded.source_file,
             file_mtime=excluded.file_mtime, turn_count=excluded.turn_count,
             responder=excluded.responder, initial_location=excluded.initial_location,
             indexed_at=excluded.indexed_at""",
        (sid, meta.get("cwd"), meta.get("repository"), None, rec.get("summary"),
         rec.get("created_at"), rec.get("updated_at"), source_format, ws_hash,
         source_file, mtime, len(rec["turns"]), rec.get("responder"),
         rec.get("initial_location"), now_iso()),
    )

    ut, at = cfg["user_truncate"], cfg["assistant_truncate"]
    n_turns = 0
    for t in rec["turns"]:
        um = truncate(t["user_message"], ut)
        ar = truncate(t["assistant_response"], at)
        conn.execute(
            """INSERT INTO turns
               (session_id, turn_index, user_message, assistant_response,
                timestamp, request_id, model_id, agent)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sid, t["turn_index"], um, ar, t["timestamp"], t["request_id"],
             t["model_id"], t["agent"]),
        )
        n_turns += 1
        blob = "\n".join(x for x in (um, ar) if x)
        if blob:
            conn.execute(
                "INSERT INTO search_index(content, session_id, source_type, source_id) "
                "VALUES (?,?,?,?)", (blob, sid, "turn", str(t["turn_index"])))

    for fp, ti in rec["files"].items():
        conn.execute(
            "INSERT OR IGNORE INTO session_files(session_id, file_path, tool_name, "
            "turn_index, first_seen_at) VALUES (?,?,?,?,?)",
            (sid, fp, None, ti, now_iso()))
    for (rt, rv), ti in rec["refs"].items():
        conn.execute(
            "INSERT OR IGNORE INTO session_refs(session_id, ref_type, ref_value, "
            "turn_index, created_at) VALUES (?,?,?,?,?)",
            (sid, rt, rv, ti, now_iso()))
    if rec.get("summary"):
        conn.execute(
            "INSERT INTO search_index(content, session_id, source_type, source_id) "
            "VALUES (?,?,?,?)", (rec["summary"], sid, "title", sid))
    return n_turns


def load_model(path, ext):
    if ext == ".jsonl":
        return replay_jsonl(path)
    with open(path, "r", encoding="utf-8") as fh:
        obj = json.load(fh)
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- #
# Index command
# --------------------------------------------------------------------------- #

def cmd_index(cfg, full):
    started = now_iso()
    t0 = time.time()
    conn = connect(cfg["db_path"])
    init_db(conn)

    roots = [cfg["workspace_storage"]] + list(cfg.get("extra_workspace_storage") or [])
    watermark_before = 0.0 if full else float(state_get(conn, "watermark_mtime", "0") or 0)
    overlap = float(cfg.get("watermark_overlap_seconds", 3))
    threshold = 0.0 if full else max(0.0, watermark_before - overlap)
    max_bytes = float(cfg["max_file_mb"]) * 1024 * 1024

    ws_map = {}
    for root in roots:
        ws_map.update(build_workspace_map(root))

    scanned = changed = indexed = skipped = failed = 0
    sessions_upserted = turns_upserted = 0
    max_mtime = watermark_before
    error_summary = None

    try:
        for ws_hash, path, mtime, size, ext in iter_chat_files(roots):
            scanned += 1
            if mtime > max_mtime:
                max_mtime = mtime
            if mtime <= threshold:
                continue
            changed += 1
            if size > max_bytes:
                skipped += 1
                continue
            try:
                model = load_model(path, ext)
                rec = parse_chat_model(model) if model else None
                if not rec:
                    skipped += 1
                    continue
                meta = ws_map.get(ws_hash, {"cwd": None, "repository": None})
                n = upsert_session(conn, rec, meta, ext.lstrip("."), ws_hash,
                                   path, mtime, cfg)
                sessions_upserted += 1
                turns_upserted += n
                indexed += 1
                if indexed % 200 == 0:
                    conn.commit()
            except (MemoryError, json.JSONDecodeError, OSError, ValueError):
                failed += 1
            except Exception:
                failed += 1
        conn.commit()
        status = "ok" if failed == 0 else "partial"
    except Exception:
        status = "error"
        error_summary = traceback.format_exc()[-2000:]

    watermark_after = max(max_mtime, watermark_before)
    state_set(conn, "watermark_mtime", repr(watermark_after))
    state_set(conn, "runs_total", str(int(state_get(conn, "runs_total", "0") or 0) + 1))
    state_set(conn, "sessions_indexed_total",
              str(int(state_get(conn, "sessions_indexed_total", "0") or 0) + sessions_upserted))
    state_set(conn, "turns_indexed_total",
              str(int(state_get(conn, "turns_indexed_total", "0") or 0) + turns_upserted))
    state_set(conn, "last_run_started", started)
    state_set(conn, "last_run_finished", now_iso())
    state_set(conn, "last_run_status", status)
    state_set(conn, "last_run_mode", "full" if full else "incremental")

    duration_ms = int((time.time() - t0) * 1000)
    conn.execute(
        """INSERT INTO index_runs
           (started_at, finished_at, status, mode, files_scanned, files_changed,
            files_indexed, files_skipped, files_failed, sessions_upserted,
            turns_upserted, duration_ms, watermark_before, watermark_after, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (started, now_iso(), status, "full" if full else "incremental",
         scanned, changed, indexed, skipped, failed, sessions_upserted,
         turns_upserted, duration_ms, watermark_before, watermark_after,
         error_summary),
    )
    conn.commit()
    conn.close()

    print(f"[engram] {status}: scanned={scanned} changed={changed} indexed={indexed} "
          f"skipped={skipped} failed={failed} sessions={sessions_upserted} "
          f"turns={turns_upserted} in {duration_ms} ms -> {cfg['db_path']}")
    if error_summary:
        print(error_summary, file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Status / query commands
# --------------------------------------------------------------------------- #

def cmd_status(cfg):
    db = cfg["db_path"]
    if not os.path.isfile(db):
        print(f"[engram] no database yet at {db}. Run: python engram.py index --full")
        return 0
    conn = connect(db)
    init_db(conn)
    print(f"Engram database : {db}")
    print(f"Config source   : {cfg.get('_config_source', '(defaults)')}")
    for k in ("schema_version", "runs_total", "sessions_indexed_total",
              "turns_indexed_total", "last_run_started", "last_run_finished",
              "last_run_status", "last_run_mode", "watermark_mtime"):
        v = state_get(conn, k)
        if k == "watermark_mtime" and v:
            try:
                v = f"{v}  ({datetime.fromtimestamp(float(v)).strftime('%Y-%m-%d %H:%M:%S')})"
            except Exception:
                pass
        print(f"  {k:<24}: {v}")
    n_sess = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_turn = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    n_file = conn.execute("SELECT COUNT(*) FROM session_files").fetchone()[0]
    print(f"  sessions in db          : {n_sess}")
    print(f"  turns in db             : {n_turn}")
    print(f"  file references in db   : {n_file}")
    print("\nRecent runs:")
    rows = conn.execute(
        "SELECT started_at, mode, status, files_changed, files_indexed, "
        "files_skipped, files_failed, turns_upserted, duration_ms "
        "FROM index_runs ORDER BY id DESC LIMIT 8").fetchall()
    for r in rows:
        print(f"  {r[0]}  {r[1]:<11} {r[2]:<7} changed={r[3]} indexed={r[4]} "
              f"skipped={r[5]} failed={r[6]} turns+={r[7]} ({r[8]} ms)")
    conn.close()
    return 0


def cmd_query(cfg, text, limit):
    db = cfg["db_path"]
    if not os.path.isfile(db):
        print(f"[engram] no database yet at {db}.")
        return 1
    conn = connect(db)
    try:
        rows = conn.execute(
            """SELECT s.id, s.summary, s.repository, s.updated_at,
                      snippet(search_index, 0, '[', ']', ' ... ', 12) AS snip
               FROM search_index
               JOIN sessions s ON s.id = search_index.session_id
               WHERE search_index MATCH ?
               ORDER BY rank LIMIT ?""",
            (text, limit)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[engram] FTS query error: {e}")
        return 1
    if not rows:
        print("(no matches)")
        return 0
    for r in rows:
        title = r[1] or "(untitled)"
        repo = r[2] or "?"
        print(f"\n# {title}  [{repo}]  {r[3]}\n  session {r[0]}\n  {r[4]}")
    conn.close()
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    p = argparse.ArgumentParser(prog="engram", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to config.json")
    sub = p.add_subparsers(dest="command")

    pi = sub.add_parser("index", help="incremental index (default)")
    pi.add_argument("--full", action="store_true", help="reparse everything")

    sub.add_parser("reindex", help="alias for `index --full`")
    sub.add_parser("status", help="show state and recent runs")

    pq = sub.add_parser("query", help="full-text search indexed chats")
    pq.add_argument("text")
    pq.add_argument("--limit", type=int, default=15)

    args = p.parse_args(argv)
    cfg = load_config(args.config)

    cmd = args.command or "index"
    if cmd == "index":
        return cmd_index(cfg, full=getattr(args, "full", False))
    if cmd == "reindex":
        return cmd_index(cfg, full=True)
    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "query":
        return cmd_query(cfg, args.text, args.limit)
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
