"""Read local Claude Code and Codex session logs and add up what each session, project and model used.

Nothing here calls a model. Claude Code writes one JSON line per API response, with token counts, to
$CLAUDE_CONFIG_DIR/projects/**/*.jsonl (default ~/.claude). Codex writes rollout files to
$CODEX_HOME/sessions/**/*.jsonl (default ~/.codex); its token_count events also carry the weekly
limit percentage.

Tokens of different kinds cost the subscription very different amounts (a cache read is about a tenth of a
fresh input token), so every row is weighted with public API list prices. The dollar figure is a proxy used
to compare sessions, not a bill.
"""
import datetime
import glob
import json
import os

# USD per 1M tokens: (input, output, cache write, cache read). Matched by substring of the model id.
CLAUDE_PRICES = {
    "fable": (15, 75, 18.75, 1.5),
    "opus": (5, 25, 6.25, 0.5),
    "sonnet": (3, 15, 3.75, 0.3),
    "haiku": (1, 5, 1.25, 0.1),
}
# Codex: (uncached input, output, cached input). One generic row; override with --prices.
CODEX_PRICES = {"default": (1.25, 10, 0.125)}

WEEK_MINUTES = 7 * 24 * 60


def claude_home():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def codex_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")


def parse_ts(s):
    """ISO 8601 string (with or without Z) or epoch number -> epoch seconds, or None."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        s = str(s).replace("Z", "+00:00")
        d = datetime.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def load_prices(path):
    """Optional JSON file: {"claude": {"opus": [in, out, cw, cr]}, "codex": {"default": [in, out, cached]}}."""
    if not path:
        return
    with open(os.path.expanduser(path)) as f:
        data = json.load(f)
    for k, v in (data.get("claude") or {}).items():
        CLAUDE_PRICES[k.lower()] = tuple(v)
    for k, v in (data.get("codex") or {}).items():
        CODEX_PRICES[k.lower()] = tuple(v)


def claude_family(model):
    m = (model or "").lower()
    for k in CLAUDE_PRICES:
        if k in m:
            return k
    return None


def codex_family(model):
    m = (model or "").lower()
    for k in CODEX_PRICES:
        if k != "default" and k in m:
            return k
    return "default"


def short_dir(cwd):
    cwd = (cwd or "").rstrip("/")
    return os.path.basename(cwd) or cwd or "?"


def _new_session(engine, sid, project):
    return {"engine": engine, "id": sid, "project": project, "cwd": "", "model": "", "first": "",
            "start": None, "last": None, "cost": 0.0, "tokens": 0, "turns": 0, "ctx": 0,
            "recent_turns": 0}


def scan_claude(since, until, recent_since=None):
    """Sessions with usage in [since, until]. recent_since: also count turns after this moment."""
    root = os.path.join(claude_home(), "projects")
    sessions, seen = {}, set()
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) < since:
                continue
            fh = open(f, errors="ignore")
        except OSError:
            continue
        first, cwd = "", ""
        folder = os.path.relpath(f, root).split(os.sep)[0]  # the project folder, used when no cwd is logged
        with fh:
            for line in fh:
                if not first and '"user"' in line:
                    try:
                        d = json.loads(line)
                        c = (d.get("message") or {}).get("content")
                        if d.get("type") == "user" and isinstance(c, str) and not c.startswith("<"):
                            first, cwd = " ".join(c.split())[:70], d.get("cwd", "")
                    except ValueError:
                        pass
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                ts = parse_ts(d.get("timestamp"))
                if ts is None or ts < since or ts > until:
                    continue
                key = (msg.get("id") or d.get("uuid"), d.get("requestId"))
                if key in seen:  # one response can be logged on several lines
                    continue
                seen.add(key)
                fam = claude_family(msg.get("model"))
                if not fam:
                    continue
                p = CLAUDE_PRICES[fam]
                inp = u.get("input_tokens", 0) or 0
                out = u.get("output_tokens", 0) or 0
                cw = u.get("cache_creation_input_tokens", 0) or 0
                cr = u.get("cache_read_input_tokens", 0) or 0
                sid = d.get("sessionId") or os.path.basename(f)
                cwd_here = d.get("cwd") or cwd
                s = sessions.get(sid)
                if s is None:
                    s = sessions[sid] = _new_session("claude", sid, short_dir(cwd_here) if cwd_here else folder)
                s["cost"] += (inp * p[0] + out * p[1] + cw * p[2] + cr * p[3]) / 1e6
                s["tokens"] += inp + out + cw + cr
                s["turns"] += 1
                if recent_since is not None and ts >= recent_since:
                    s["recent_turns"] += 1
                s["start"] = ts if s["start"] is None else min(s["start"], ts)
                if s["last"] is None or ts >= s["last"]:
                    s["last"] = ts
                    s["ctx"] = inp + cw + cr
                    s["model"] = msg.get("model") or s["model"]
                if not s["cwd"] and cwd_here:
                    s["cwd"], s["project"] = cwd_here, short_dir(cwd_here)
                if not s["first"] and first:
                    s["first"] = first
    return sessions


def _weekly_window(rl):
    """The weekly window ({used_percent, resets_at}) out of a Codex rate_limits object."""
    for k in ("primary", "secondary"):
        w = (rl or {}).get(k)
        if isinstance(w, dict) and w.get("window_minutes") == WEEK_MINUTES:
            return w
    return None


def scan_codex(since, until, recent_since=None):
    """Codex sessions with usage in [since, until], plus the newest weekly-limit reading."""
    root = os.path.join(codex_home(), "sessions")
    sessions, weekly = {}, None
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) < since:
                continue
            fh = open(f, errors="ignore")
        except OSError:
            continue
        sid, cwd, model, first = os.path.basename(f)[:-6], "", "", ""
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t, p = d.get("type"), d.get("payload") or {}
                if t == "session_meta":
                    sid = p.get("id") or p.get("session_id") or sid
                    cwd = p.get("cwd") or cwd
                    continue
                if t == "turn_context":
                    model = p.get("model") or model
                    cwd = p.get("cwd") or cwd
                    continue
                if not first and t == "event_msg" and p.get("type") == "user_message":
                    m = p.get("message")
                    if isinstance(m, str) and not m.startswith("<"):
                        first = " ".join(m.split())[:70]
                    continue
                if not (t == "event_msg" and p.get("type") == "token_count"):
                    continue
                ts = parse_ts(d.get("timestamp"))
                if ts is None or ts > until:
                    continue
                w = _weekly_window(p.get("rate_limits"))
                if w and (weekly is None or ts > weekly["at"]):
                    weekly = {"used": float(w.get("used_percent") or 0), "resets_at": parse_ts(w.get("resets_at")), "at": ts}
                if ts < since:
                    continue
                last = (p.get("info") or {}).get("last_token_usage")
                if not isinstance(last, dict):
                    continue
                inp = last.get("input_tokens", 0) or 0
                cached = min(last.get("cached_input_tokens", 0) or 0, inp)
                out = last.get("output_tokens", 0) or 0
                pr = CODEX_PRICES[codex_family(model)]
                s = sessions.get(sid)
                if s is None:
                    s = sessions[sid] = _new_session("codex", sid, short_dir(cwd))
                s["cost"] += ((inp - cached) * pr[0] + out * pr[1] + cached * pr[2]) / 1e6
                s["tokens"] += inp + out
                s["turns"] += 1
                if recent_since is not None and ts >= recent_since:
                    s["recent_turns"] += 1
                s["start"] = ts if s["start"] is None else min(s["start"], ts)
                if s["last"] is None or ts >= s["last"]:
                    s["last"], s["ctx"] = ts, inp
                s["model"] = model or s["model"]
                if cwd:
                    s["cwd"], s["project"] = cwd, short_dir(cwd)
                if first and not s["first"]:
                    s["first"] = first
    return sessions, weekly


def claude_live_weekly(cache_seconds=240):
    """Claude weekly %, read from the same usage endpoint the Claude Code /usage screen uses.

    Opt-in (--live). Reads the Claude Code OAuth token from the macOS keychain or
    $CLAUDE_CONFIG_DIR/.credentials.json. The endpoint rate-limits fast polling, so the last good answer is
    cached for a few minutes. Returns {"used", "resets_at"} or None.
    """
    import subprocess
    import urllib.request

    cache = os.path.join(cache_dir(), "claude_weekly.json")
    try:
        c = json.load(open(cache))
        if c.get("at", 0) > datetime.datetime.now().timestamp() - cache_seconds:
            return c
    except (OSError, ValueError):
        c = None
    raw = None
    try:
        raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    if not raw:
        try:
            raw = open(os.path.join(claude_home(), ".credentials.json")).read()
        except OSError:
            return c
    try:
        token = json.loads(raw)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError, TypeError):
        return c
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        w = data.get("seven_day") or {}
        out = {"used": float(w.get("utilization") or 0), "resets_at": parse_ts(w.get("resets_at")),
               "at": datetime.datetime.now().timestamp()}
    except Exception:
        return c  # keep the last good value
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    json.dump(out, open(cache, "w"))
    return out


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "claude-usage-meter")
