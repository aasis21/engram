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

  # --query is optional — browse by time window instead of a keyword:
  python engram_search.py list --today                 # everything worked on today
  python engram_search.py list --days 7 --repo ModernOrder
  python engram_search.py list --query "817352353" --and retrospective --json

  # Deep-dive a session (id or 8-char prefix; searches both stores):
  python engram_search.py show --session 769739be
  python engram_search.py show --session 769739be --query upgrade
  python engram_search.py show --session 769739be --turn 5
  python engram_search.py show --session 769739be --full

  # Aggregate counts over a window (never truncated by --limit):
  python engram_search.py stats                    # full report, last 30 days
  python engram_search.py stats --days 7           # this week's activity
  python engram_search.py stats --today --by repo  # today, only the repo table
  python engram_search.py stats --query deployment --days 0 --json

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


def today_cutoff_iso():
    """UTC ISO cutoff for the start of *today* in the user's local timezone."""
    now_local = datetime.now().astimezone()
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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
def search_store(source, path, query, and_terms, regex, cutoff, repo, limit):
    con = connect_ro(path)
    con.row_factory = sqlite3.Row
    scols = table_columns(con, "sessions")
    results = []

    # 1) Determine candidate session_ids + match counts.
    match_counts = {}  # session_id -> int
    all_terms = [t for t in ([query] if query else []) + list(and_terms) if t]
    has_query = bool(all_terms)

    if has_query and regex:
        patterns = [re.compile(t, re.IGNORECASE) for t in all_terms]
        sql = "SELECT session_id, user_message, assistant_response FROM turns"
        for r in con.execute(sql):
            blob = f"{r['user_message'] or ''}\n{r['assistant_response'] or ''}"
            if all(p.search(blob) for p in patterns):
                match_counts[r["session_id"]] = match_counts.get(r["session_id"], 0) + 1
    elif has_query:
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

    if has_query and not match_counts:
        con.close()
        return results, 0

    # 2) Hydrate session metadata.
    sel = ["id", "repository", "branch", "summary", "created_at", "updated_at"]
    for opt in ("cwd", "host_type", "responder", "source_file", "initial_location"):
        if opt in scols:
            sel.append(opt)

    if has_query:
        placeholders = ",".join("?" * len(match_counts))
        rows = con.execute(
            f"SELECT {','.join(sel)} FROM sessions WHERE id IN ({placeholders})",
            tuple(match_counts.keys()),
        ).fetchall()
    else:
        # Browse mode: no keyword filter — list recent sessions in the window.
        sql = f"SELECT {','.join(sel)} FROM sessions"
        params = []
        if cutoff:
            sql += " WHERE updated_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY updated_at DESC"
        if not repo:
            sql += " LIMIT ?"
            params.append(limit)
        rows = con.execute(sql, tuple(params)).fetchall()

    # Per-session created/edited file counts. `read` is intentionally omitted:
    # the CLI store never records reads, so it can't be shown consistently.
    file_counts = {}
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 800):
        chunk = ids[i:i + 800]
        ph = ",".join("?" * len(chunk))
        try:
            for fr in con.execute(
                f"SELECT session_id, tool_name, COUNT(*) c FROM session_files "
                f"WHERE session_id IN ({ph}) AND tool_name IN ('create','edit') "
                f"GROUP BY session_id, tool_name",
                tuple(chunk),
            ):
                d = file_counts.setdefault(fr["session_id"], {"created": 0, "edited": 0})
                if fr["tool_name"] == "create":
                    d["created"] = fr["c"]
                else:
                    d["edited"] = fr["c"]
        except sqlite3.Error:
            break

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
                "cwd": rd.get("cwd") or "",
                "repo": rd.get("repository") or "",
                "branch": rd.get("branch") or "",
                "created": file_counts.get(rd["id"], {}).get("created", 0),
                "edited": file_counts.get(rd["id"], {}).get("edited", 0),
                "opened": clean(op[0] if op else "", 120),
            }
        )

    # True total in the window, before the display limit.
    # Query mode and repo-filtered browse already materialize the full set,
    # so len(results) is exact. Only browse-without-repo pushes a SQL LIMIT,
    # so there we add a single cheap COUNT(*) (no hydration, no N+1).
    if not has_query and not repo:
        csql = "SELECT COUNT(*) FROM sessions"
        cparams = []
        if cutoff:
            csql += " WHERE updated_at >= ?"
            cparams.append(cutoff)
        total = con.execute(csql, tuple(cparams)).fetchone()[0]
    else:
        total = len(results)
    con.close()
    return results, total


def cmd_list(args):
    stores = store_paths(args.cli_db, args.chat_db)
    if args.source != "all":
        stores = [s for s in stores if s[0] == args.source]
    if not stores:
        print("No session-store databases found.", file=sys.stderr)
        return 1

    cutoff = today_cutoff_iso() if args.today else cutoff_iso(args.days)

    merged = []
    grand_total = 0
    for source, path in stores:
        res, tot = search_store(
            source, path, args.query, args.and_, args.regex,
            cutoff, args.repo, args.limit,
        )
        merged += res
        grand_total += tot
    merged.sort(key=lambda r: r["time"], reverse=True)
    merged = merged[: args.limit]

    if args.json:
        print(json.dumps(merged, indent=2))
        return 0

    if not merged:
        print("No matching sessions.")
        return 0

    if args.query:
        scope = f"matching '{args.query}'"
    elif args.today:
        scope = "worked on today"
    elif args.days and args.days > 0:
        scope = f"updated in the last {args.days} day(s)"
    else:
        scope = "(all history)"
    print(f"\n{len(merged)} session(s) {scope} "
          f"(sources: {', '.join(s[0] for s in stores)}):\n")
    for r in merged:
        tag = r["source"].upper()
        meta = f"matches={r['matches']}  " if args.query else ""
        print(f"[{tag:4}] {r['time'][:19]}  {r['id'][:8]}  {meta}turns={r['turns']}")
        print(f"        title : {r['title']}")
        print(f"        cwd   : {clean(r['cwd'] or '(unknown)', 90)}")
        if r["repo"] or r["branch"]:
            print(f"        repo  : {r['repo'] or '\u2014'}   branch: {r['branch'] or '\u2014'}")
        if r["created"] or r["edited"]:
            print(f"        files : created {r['created']}, edited {r['edited']}")
        if r["opened"]:
            print(f"        opened: {r['opened']}")
        print()
    if grand_total > len(merged):
        hidden = grand_total - len(merged)
        print(f"(showing {len(merged)} of {grand_total} \u2014 {hidden} hidden. "
              f"Raise --limit, or run `stats` for the breakdown.)\n")
    return 0


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def _bar(n, mx, width=22):
    if mx <= 0:
        return ""
    return "\u2588" * max(1, round(n / mx * width))


def cmd_stats(args):
    stores = store_paths(args.cli_db, args.chat_db)
    if args.source != "all":
        stores = [s for s in stores if s[0] == args.source]
    if not stores:
        print("No session-store databases found.", file=sys.stderr)
        return 1

    cutoff = today_cutoff_iso() if args.today else cutoff_iso(args.days)
    BIG = 10 ** 9  # stats counts the full matched set — never truncates
    rows = []
    for source, path in stores:
        res, _ = search_store(
            source, path, args.query, args.and_, args.regex,
            cutoff, args.repo, BIG,
        )
        rows += res

    total_sessions = len(rows)
    total_turns = sum(r["turns"] for r in rows)
    by_source, by_day, by_repo = {}, {}, {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        day = (r["time"] or "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + 1
        repo = clean(r["repo"] or r["cwd"], 60) or "(unknown)"
        by_repo[repo] = by_repo.get(repo, 0) + 1

    which = set(args.by) if args.by else {"day", "repo", "source"}

    if args.today:
        window = "today"
    elif args.days and args.days > 0:
        window = f"last {args.days} day(s)"
    else:
        window = "all history"

    if args.json:
        out = {
            "window": window,
            "sources": [s[0] for s in stores],
            "sessions": total_sessions,
            "turns": total_turns,
            "by_source": by_source,
        }
        if "day" in which:
            out["by_day"] = dict(sorted(by_day.items(), reverse=True))
        if "repo" in which:
            out["by_repo"] = dict(
                sorted(by_repo.items(), key=lambda kv: kv[1], reverse=True)
            )
        print(json.dumps(out, indent=2))
        return 0

    src_split = "  ".join(
        f"{k} {v}" for k, v in sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)
    )
    head = f"Window: {window}   sources: {', '.join(s[0] for s in stores)}"
    if args.query:
        head += f"   query: '{args.query}'"
    if args.repo:
        head += f"   repo~{args.repo}"
    print()
    print(head)
    print("\u2500" * 56)
    print(f"Sessions : {total_sessions:,}" + (f"   ({src_split})" if src_split else ""))
    print(f"Turns    : {total_turns:,}")
    if "day" in which:
        print(f"Active   : {len(by_day)} day(s) with activity")

    if total_sessions == 0:
        print("\n(no sessions in window)")
        return 0

    if "day" in which and by_day:
        print("\nBy day:")
        mx = max(by_day.values())
        for day in sorted(by_day, reverse=True):
            n = by_day[day]
            print(f"  {day}  {_bar(n, mx):<22} {n}")

    if "repo" in which and by_repo:
        print("\nBy repo / location:")
        mx = max(by_repo.values())
        for repo, n in sorted(by_repo.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {n:>5}  {_bar(n, mx):<22} {repo}")

    if "source" in which and by_source:
        print("\nBy source:")
        for k, v in sorted(by_source.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {k:<6} {v}")
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

    pl = sub.add_parser("list", help="List sessions by topic and/or time window.")
    pl.add_argument("--query", help="Search term (FTS keywords, or regex with --regex). "
                                    "Optional — omit to browse all sessions in the time window.")
    pl.add_argument("--and", dest="and_", action="append", default=[],
                    help="Extra term that must ALSO appear (repeatable).")
    pl.add_argument("--regex", action="store_true", help="Treat query/--and as regular expressions.")
    pl.add_argument("--source", choices=["cli", "chat", "all"], default="all")
    pl.add_argument("--repo", "-w", help="Only sessions whose location contains this substring.")
    pl.add_argument("--today", action="store_true",
                    help="Only sessions updated today (local time). Overrides --days.")
    pl.add_argument("--days", type=int, default=30, help="Only sessions updated within N days (default 30; 0 = all).")
    pl.add_argument("--limit", type=int, default=1000)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pt = sub.add_parser("stats", help="Aggregate session/turn counts over a window (never truncated).")
    pt.add_argument("--query", help="Optional search term — count only matching sessions.")
    pt.add_argument("--and", dest="and_", action="append", default=[],
                    help="Extra term that must ALSO appear (repeatable).")
    pt.add_argument("--regex", action="store_true", help="Treat query/--and as regular expressions.")
    pt.add_argument("--source", choices=["cli", "chat", "all"], default="all")
    pt.add_argument("--repo", "-w", help="Only sessions whose location contains this substring.")
    pt.add_argument("--today", action="store_true", help="Only today (local time). Overrides --days.")
    pt.add_argument("--days", type=int, default=30, help="Window in days (default 30; 0 = all).")
    pt.add_argument("--by", action="append", choices=["day", "repo", "source"],
                    help="Narrow which breakdown(s) to show (repeatable). Default: all three.")
    pt.add_argument("--json", action="store_true")
    pt.set_defaults(func=cmd_stats)

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
