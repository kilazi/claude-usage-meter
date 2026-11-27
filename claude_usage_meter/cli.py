"""claude-usage-meter: which session, project and model used your Claude Code / Codex weekly limit.

  claude-usage-meter                 report for the current week (Claude Code and Codex)
  claude-usage-meter --live          also read the Claude weekly % (one HTTPS call to the usage endpoint)
  claude-usage-meter watch           warn about any session burning the week right now (run it from cron)
"""
import argparse
import collections
import datetime
import json
import os
import shlex
import subprocess
import sys
import time

from . import __version__
from . import meter

DAY = 86400


def fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %m-%d %H:%M") if ts else "?"


def fmt_k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n // 1000}K"


def money(v):
    return f"${v:,.0f}" if v >= 10 else f"${v:,.2f}"


def window(args, now, weekly):
    """Start of the week to report on: --since, else the limit's own reset time minus 7 days, else 7 days ago."""
    if args.since:
        ts = meter.parse_ts(args.since)
        if ts is None:
            sys.exit(f"claude-usage-meter: cannot read --since {args.since!r} (use ISO, e.g. 2026-01-05T09:00)")
        return ts
    if weekly and weekly.get("resets_at") and weekly["resets_at"] > now:
        return weekly["resets_at"] - 7 * DAY
    return now - 7 * DAY


def short_id(sid):
    # Codex ids are time-ordered (UUIDv7), so their first characters repeat; the tail is the distinct part
    return sid[-8:]


def summarize(engine, sessions, since, weekly, top, now):
    total = sum(s["cost"] for s in sessions.values()) or 0.0
    stale = bool(weekly and weekly.get("resets_at") and weekly["resets_at"] < now)
    used = weekly.get("used") if weekly and not stale else None
    by_model, by_proj = collections.Counter(), collections.Counter()
    for s in sessions.values():
        fam = (meter.claude_family(s["model"]) if engine == "claude" else s["model"]) or "other"
        by_model[fam] += s["cost"]
        by_proj[s["project"]] += s["cost"]

    def share(v):
        return v / total if total else 0.0

    rows = sorted(sessions.values(), key=lambda s: -s["cost"])[:top]
    return {
        "engine": engine, "since": since, "weekly_used_percent": used, "weekly_stale": stale,
        "weekly_resets_at": weekly.get("resets_at") if weekly and not stale else None,
        "proxy_usd": round(total, 2), "sessions_count": len(sessions),
        "by_model": [{"model": k, "share": round(share(v), 4), "proxy_usd": round(v, 2)} for k, v in by_model.most_common()],
        "by_project": [{"project": k, "share": round(share(v), 4), "points": round(share(v) * used, 2) if used is not None else None,
                        "proxy_usd": round(v, 2)} for k, v in by_proj.most_common(top)],
        "sessions": [{"id": s["id"], "project": s["project"], "cwd": s["cwd"], "model": s["model"], "first_prompt": s["first"],
                      "started": s["start"], "last": s["last"], "turns": s["turns"], "last_context_tokens": s["ctx"],
                      "share": round(share(s["cost"]), 4), "points": round(share(s["cost"]) * used, 2) if used is not None else None,
                      "proxy_usd": round(s["cost"], 2)} for s in rows],
    }


def print_summary(r):
    name = "Claude Code" if r["engine"] == "claude" else "Codex"
    used = r["weekly_used_percent"]
    if used is not None:
        limit = f"weekly limit {used:.0f}% used"
    elif r["weekly_stale"]:
        limit = "weekly % unknown (the last reading is from a week that has reset)"
    else:
        limit = "weekly % unknown" + (" (add --live)" if r["engine"] == "claude" else "")
    if r["weekly_resets_at"]:
        limit += f", resets {fmt_time(r['weekly_resets_at'])}"
    print(f"\n{name}: since {fmt_time(r['since'])} | {limit}")
    if not r["sessions_count"]:
        where = os.path.join(meter.claude_home() if r["engine"] == "claude" else meter.codex_home(),
                             "projects" if r["engine"] == "claude" else "sessions")
        print(f"  no usage found in {where} (set {'CLAUDE_CONFIG_DIR' if r['engine'] == 'claude' else 'CODEX_HOME'} if your logs live elsewhere)")
        return
    print(f"  {r['sessions_count']} sessions, {money(r['proxy_usd'])} at API list prices (a proxy for comparing, not a bill)")
    print("  by model: " + "  ".join(f"{m['model']} {m['share'] * 100:.0f}%" for m in r["by_model"]))
    pts = used is not None
    print("\n  share" + ("   pts" if pts else "") + "   proxy  project")
    for p in r["by_project"]:
        print(f"  {p['share'] * 100:4.0f}%" + (f" {p['points']:5.1f}" if pts else "") + f" {money(p['proxy_usd']):>7}  {p['project'][:50]}")
    print("\n  share" + ("   pts" if pts else "") + "   proxy turns    ctx  started       model             project / first prompt")
    for s in r["sessions"]:
        print(f"  {s['share'] * 100:4.0f}%" + (f" {s['points']:5.1f}" if pts else "")
              + f" {money(s['proxy_usd']):>7} {s['turns']:5} {fmt_k(s['last_context_tokens']):>6}  {fmt_time(s['started'])}  "
              f"{(s['model'] or '?')[:16]:16}  {s['project'][:24]} {short_id(s['id'])}  {s['first_prompt'][:48]!r}")


def cmd_report(args):
    now = meter.parse_ts(args.now) if args.now else time.time()
    out = []
    if args.engine in ("all", "claude"):
        weekly = meter.claude_live_weekly() if args.live else None
        since = window(args, now, weekly)
        out.append(summarize("claude", meter.scan_claude(since, now), since, weekly, args.top, now))
    if args.engine in ("all", "codex"):
        _, weekly = meter.scan_codex(now - 8 * DAY, now)
        since = window(args, now, weekly)
        sessions, _ = meter.scan_codex(since, now)
        out.append(summarize("codex", sessions, since, weekly, args.top, now))
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for r in out:
            print_summary(r)
    return 0


def notify(target, text):
    if target == "macos":
        script = f"display notification {json.dumps(text[:240])} with title \"claude-usage-meter\""
        r = subprocess.run(["osascript", "-e", script], capture_output=True)
        return r.returncode == 0
    r = subprocess.run(target, shell=True, input=text, text=True, capture_output=True,
                       env=dict(os.environ, CUM_MESSAGE=text))
    return r.returncode == 0


def cmd_watch(args):
    now = meter.parse_ts(args.now) if args.now else time.time()
    since, recent = now - args.hours * 3600, now - 3 * 3600
    sessions = {}
    if args.engine in ("all", "claude"):
        sessions.update(meter.scan_claude(since, now, recent))
    weekly_codex = None
    if args.engine in ("all", "codex"):
        cs, weekly_codex = meter.scan_codex(since, now, recent)
        sessions.update(cs)
    state_path = os.path.join(meter.cache_dir(), "alerted.json")
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {}
    alerts = 0
    for s in sorted(sessions.values(), key=lambda s: -s["cost"]):
        heavy = s["cost"] >= args.alert_usd
        fat = s["ctx"] >= args.fat_ctx and s["recent_turns"] >= args.fat_turns
        if not (heavy or fat):
            continue
        if not args.dry and now - state.get(s["id"], 0) < args.quiet_hours * 3600:
            continue
        why = (f"{money(s['cost'])} at API list prices in the last {args.hours:g} h" if heavy else
               f"{s['recent_turns']} turns in 3 h, each re-reading {fmt_k(s['ctx'])} tokens of context")
        text = (f"{s['engine']} session {s['id']} in {s['project']} is burning your weekly limit: {why} "
                f"({s['model'] or '?'}, {s['turns']} turns, context now {fmt_k(s['ctx'])}). "
                f"Started with: {s['first'][:60]!r}. A long loop in one session re-reads its whole context every turn; "
                f"a fresh short session per run costs a fraction.")
        alerts += 1
        print(("WOULD ALERT: " if args.dry else "ALERT: ") + text)
        if args.dry:
            continue
        if args.notify and not notify(args.notify, text):
            print("claude-usage-meter: --notify command failed", file=sys.stderr)
            continue
        state[s["id"]] = now
    if not args.dry:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        json.dump({k: v for k, v in state.items() if now - v < 7 * DAY}, open(state_path, "w"))
    if not alerts and not args.quiet:
        print(f"ok: no session above {money(args.alert_usd)} in {args.hours:g} h or re-reading {fmt_k(args.fat_ctx)}+ "
              f"for {args.fat_turns}+ turns in 3 h ({len(sessions)} active)")
    return 1 if alerts and args.exit_code else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = "report"
    if argv and argv[0] in ("report", "watch"):
        cmd = argv.pop(0)
    ap = argparse.ArgumentParser(prog="claude-usage-meter", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--engine", choices=("all", "claude", "codex"), default="all")
    ap.add_argument("--prices", default=os.environ.get("CUM_PRICES"), help="JSON file overriding the per-model prices")
    ap.add_argument("--now", help=argparse.SUPPRESS)  # replay the logs as of a past moment (tests)
    if cmd == "report":
        ap.add_argument("--since", help="week start (ISO); default: the limit's reset time minus 7 days, else 7 days ago")
        ap.add_argument("--live", action="store_true", help="read the Claude weekly %% from its usage endpoint")
        ap.add_argument("--top", type=int, default=15, help="rows per table (default 15)")
        ap.add_argument("--json", action="store_true")
    else:
        env = os.environ.get
        ap.add_argument("--hours", type=float, default=float(env("CUM_HOURS", 6)), help="look-back window (default 6)")
        ap.add_argument("--alert-usd", type=float, default=float(env("CUM_ALERT_USD", 100)),
                        help="alert when one session's proxy cost in the window reaches this (default 100)")
        ap.add_argument("--fat-ctx", type=int, default=int(env("CUM_FAT_CTX", 400_000)),
                        help="...or when it re-reads this many context tokens per turn (default 400000)")
        ap.add_argument("--fat-turns", type=int, default=int(env("CUM_FAT_TURNS", 15)),
                        help="...for at least this many turns in 3 h (default 15)")
        ap.add_argument("--quiet-hours", type=float, default=12, help="alert once per session per this many hours")
        ap.add_argument("--notify", default=env("CUM_NOTIFY"),
                        help="'macos' for a desktop notification, or a shell command that gets the message on stdin")
        ap.add_argument("--dry", action="store_true", help="print what would alert; ignore and keep the alert state")
        ap.add_argument("--quiet", action="store_true", help="print nothing when no session alerts")
        ap.add_argument("--exit-code", action="store_true", help="exit 1 when something alerted")
    args = ap.parse_args(argv)
    meter.load_prices(args.prices)
    try:
        return cmd_report(args) if cmd == "report" else cmd_watch(args)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
