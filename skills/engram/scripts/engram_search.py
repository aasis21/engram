#!/usr/bin/env python3
"""
engram_search.py — Unified search across BOTH Copilot session-store databases.

Two SQLite stores live under ~/.copilot/ on this machine:

  * session-store.db                  -> Copilot CLI / Forge sessions   (source: "cli")
  * session-store-vscode-chat.db      -> VS Code Copilot Chat sessions  (source: "chat",
                                          built and maintained by Engram)

Both share the same core shape (sessions / turns) and an FTS5 full-text index
`search_index(content, session_id, source_type, source_id)`. This tool queries
them together, tags every hit with its origin, and returns one merged, ranked list.

Usage
-----
  # List sessions matching a topic across both stores (last 30 days by default):
  python engram_search.py list --query "upgrade net8"
  python engram_search.py list --query "SNAT|socket" --regex --days 90
  python engram_search.py list --query "recon" --source chat --repo ModernOrder
  python engram_search.py list --query "817352353" --and retrospective --json

  # Deep-dive a session (id or 8-char prefix; searches both stores):
  python engram_search.py show --session 769739be
  python engram_search.py show --session 769739be --query upgrade
  python engram_search.py show --session 769739be --turn 5
  python engram_search.py show --session 769739be --full

Read-only: opens every database with ?mode=ro and never writes.
Standard library only (argparse, sqlite3, json, re).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Database discovery
# ---------------------------------------------------------------------------
def default_db_dir():
    return os.environ.get(
        "COPILOT_SESSION_DIR",
        os.path.join(os.path.expanduser("~"), ".copilot"),
    )


def store_paths(cli_db=None, chat_db=None):
    """Return list of (source, path) for stores that exist on disk."""
    d = default_db_dir()
    cli = cli_db or os.path.join(d, "session-store.db")
    chat = chat_db or os.path.join(d, "session-store-vscode-chat.db")
    stores = []
    if os.path.isfile(cli):
        stores.append(("cli", cli))
    if os.path.isfile(chat):
        stores.append(("chat", chat))
    return stores


def connect_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_columns(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def cutoff_iso(days):
    if not days or days <= 0:
        return None
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fts_query(terms):
    """Build a safe FTS5 MATCH expression: all word-tokens ANDed."""
    tokens = []
    for t in terms:
        tokens += re.findall(r"\w+", t)
    if not tokens:
        return None
    return " AND ".join(f'"{tok}"' for tok in tokens)


def clean(s, n=None):
    if s is None:
        return ""
    s = re.sub(r"\s+", " ", str(s)).strip()
    if n and len(s) > n:
        s = s[: n - 1] + "\u2026"
    return s


def workspace_label(row):
    """Best human label for where a session lived."""
    for key in ("repository", "cwd", "branch", "source_file", "initial_location"):
        v = row.get(key)
        if v:
            return v
    return "(unknown)"


# ---------------------------------------------------------------------------
# Per-store search
# ---------------------------------------------------------------------------
def search_store(source, path, query, and_terms, regex, days, repo, limit):
    con = connect_ro(path)
    con.row_factory = sqlite3.Row
    scols = table_columns(con, "sessions")
    cutoff = cutoff_iso(days)
    results = []

    # 1) Determine candidate session_ids + match counts.
    match_counts = {}  # session_id -> int
    all_terms = [query] + list(and_terms)

    if regex:
        patterns = [re.compile(t, re.IGNORECASE) for t in all_terms]
        sql = "SELECT session_id, user_message, assistant_response FROM turns"
        for r in con.execute(sql):
            blob = f"{r['user_message'] or ''}\n{r['assistant_response'] or ''}"
            if all(p.search(blob) for p in patterns):
                match_counts[r["session_id"]] = match_counts.get(r["session_id"], 0) + 1
    else:
        # FTS prefilter: a session must match EVERY term (AND across terms),
        # match count = rows in the index for the combined expression.
        per_term_sets = []
        try:
            for term in all_terms:
                expr = fts_query([term])
                if not expr:
                    continue
                ids = {}
                for row in con.execute(
                    "SELECT session_id, COUNT(*) c FROM search_index "
                    "WHERE search_index MATCH ? GROUP BY session_id",
                    (expr,),
                ):
                    ids[row["session_id"]] = row["c"]
                per_term_sets.append(ids)
            if per_term_sets:
                common = set(per_term_sets[0])
                for s in per_term_sets[1:]:
                    common &= set(s)
                for sid in common:
                    match_counts[sid] = per_term_sets[0].get(sid, 0)
        except sqlite3.Error:
            # Fallback: LIKE scan over turns.
            like = f"%{query}%"
            for r in con.execute(
                "SELECT session_id, COUNT(*) c FROM turns "
                "WHERE user_message LIKE ? OR assistant_response LIKE ? "
                "GROUP BY session_id",
                (like, like),
            ):
                match_counts[r["session_id"]] = r["c"]

    if not match_counts:
        con.close()
        return results

    # 2) Hydrate session metadata.
    sel = ["id", "repository", "branch", "summary", "created_at", "updated_at"]
    for opt in ("cwd", "host_type", "responder", "source_file", "initial_location"):
        if opt in scols:
            sel.append(opt)
    placeholders = ",".join("?" * len(match_counts))
    rows = con.execute(
        f"SELECT {','.join(sel)} FROM sessions WHERE id IN ({placeholders})",
        tuple(match_counts.keys()),
    ).fetchall()

    for row in rows:
        rd = dict(row)
        updated = rd.get("updated_at") or ""
        if cutoff and updated and updated < cutoff:
            continue
        label = workspace_label(rd)
        if repo and repo.lower() not in (label or "").lower():
            # also check other location-ish fields
            hay = " ".join(
                str(rd.get(k) or "") for k in ("repository", "cwd", "branch", "source_file")
            ).lower()
            if repo.lower() not in hay:
                continue
        # opening prompt = first user turn
        op = con.execute(
            "SELECT user_message FROM turns WHERE session_id=? "
            "ORDER BY turn_index ASC LIMIT 1",
            (rd["id"],),
        ).fetchone()
        total = con.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id=?", (rd["id"],)
        ).fetchone()[0]
        results.append(
            {
                "source": source,
                "id": rd["id"],
                "time": updated or rd.get("created_at") or "",
                "title": clean(rd.get("summary"), 80) or "(no summary)",
                "matches": match_counts.get(rd["id"], 0),
                "turns": total,
                "workspace": label,
                "opened": clean(op[0] if op else "", 120),
            }
        )
    con.close()
    return results


def cmd_list(args):
    stores = store_paths(args.cli_db, args.chat_db)
    if args.source != "all":
        stores = [s for s in stores if s[0] == args.source]
    if not stores:
        print("No session-store databases found.", file=sys.stderr)
        return 1

    merged = []
    for source, path in stores:
        merged += search_store(
            source, path, args.query, args.and_, args.regex,
            args.days, args.repo, args.limit,
        )
    merged.sort(key=lambda r: r["time"], reverse=True)
    merged = merged[: args.limit]

    if args.json:
        print(json.dumps(merged, indent=2))
        return 0

    if not merged:
        print("No matching sessions.")
        return 0

    print(f"\n{len(merged)} session(s) matching '{args.query}' "
          f"(sources: {', '.join(s[0] for s in stores)}):\n")
    for r in merged:
        tag = r["source"].upper()
        print(f"[{tag:4}] {r['time'][:19]}  {r['id'][:8]}  matches={r['matches']}  turns={r['turns']}")
        print(f"        title : {r['title']}")
        print(f"        where : {clean(r['workspace'], 90)}")
        if r["opened"]:
            print(f"        opened: {r['opened']}")
        print()
    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------
def find_session(stores, sid):
    """Return (source, path, full_id) for the first store containing sid prefix."""
    for source, path in stores:
        con = connect_ro(path)
        row = con.execute(
            "SELECT id FROM sessions WHERE id = ? OR id LIKE ? LIMIT 1",
            (sid, sid + "%"),
        ).fetchone()
        con.close()
        if row:
            return source, path, row[0]
    return None, None, None


def cmd_show(args):
    stores = store_paths(args.cli_db, args.chat_db)
    if args.source != "all":
        stores = [s for s in stores if s[0] == args.source]
    source, path, full_id = find_session(stores, args.session)
    if not full_id:
        print(f"Session '{args.session}' not found.", file=sys.stderr)
        return 1

    con = connect_ro(path)
    con.row_factory = sqlite3.Row
    meta = con.execute("SELECT * FROM sessions WHERE id=?", (full_id,)).fetchone()
    print(f"\n=== [{source.upper()}] {full_id} ===")
    md = dict(meta)
    print(f"title    : {clean(md.get('summary'), 200)}")
    print(f"where    : {workspace_label(md)}")
    print(f"created  : {md.get('created_at')}   updated: {md.get('updated_at')}")
    if md.get("source_file"):
        print(f"jsonl    : {md.get('source_file')}")
    print()

    turns = con.execute(
        "SELECT turn_index, user_message, assistant_response FROM turns "
        "WHERE session_id=? ORDER BY turn_index ASC",
        (full_id,),
    ).fetchall()
    con.close()

    pat = re.compile(args.query, re.IGNORECASE) if args.query else None
    trunc = None if (args.full or args.turn is not None) else 400

    shown = 0
    for t in turns:
        if args.turn is not None and t["turn_index"] != args.turn:
            continue
        um = t["user_message"] or ""
        am = t["assistant_response"] or ""
        if pat and not (pat.search(um) or pat.search(am)):
            continue
        print(f"--- turn {t['turn_index']} ---")
        print(f"USER: {clean(um, trunc)}")
        if am:
            print(f"ASSISTANT: {clean(am, trunc)}")
        print()
        shown += 1
    if shown == 0:
        print("(no matching turns)")
    return 0


# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Search Copilot CLI + VS Code Chat session stores.")
    p.add_argument("--cli-db", help="Override path to session-store.db")
    p.add_argument("--chat-db", help="Override path to session-store-vscode-chat.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="List sessions matching a topic.")
    pl.add_argument("--query", required=True, help="Search term (FTS keywords, or regex with --regex).")
    pl.add_argument("--and", dest="and_", action="append", default=[],
                    help="Extra term that must ALSO appear (repeatable).")
    pl.add_argument("--regex", action="store_true", help="Treat query/--and as regular expressions.")
    pl.add_argument("--source", choices=["cli", "chat", "all"], default="all")
    pl.add_argument("--repo", "-w", help="Only sessions whose location contains this substring.")
    pl.add_argument("--days", type=int, default=30, help="Only sessions updated within N days (default 30; 0 = all).")
    pl.add_argument("--limit", type=int, default=25)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="Render a session transcript.")
    ps.add_argument("--session", required=True, help="Session id or 8-char prefix.")
    ps.add_argument("--query", help="Only show turns matching this regex.")
    ps.add_argument("--turn", type=int, help="Show only this turn index, untruncated.")
    ps.add_argument("--full", action="store_true", help="Show every turn untruncated.")
    ps.add_argument("--source", choices=["cli", "chat", "all"], default="all")
    ps.set_defaults(func=cmd_show)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
