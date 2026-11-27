#!/usr/bin/env python3
"""Smoke test: build fake Claude Code and Codex logs in a temp dir, run the CLI on them, check the numbers.

  python3 test/smoke.py
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime.datetime(2030, 1, 10, 12, 0, tzinfo=datetime.timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))


def claude_turn(sid, cwd, model, n, at, cache_read, out=200):
    return {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": iso(at), "requestId": f"req_{sid}_{n}",
            "message": {"id": f"msg_{sid}_{n}", "model": model, "usage": {
                "input_tokens": 10, "output_tokens": out, "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": cache_read}}}


def build(tmp):
    claude = os.path.join(tmp, "claude", "projects")
    # a loop: 40 opus turns in the last 2 h, each re-reading 500K tokens of context
    loop = [{"type": "user", "sessionId": "loop-1111", "cwd": "/work/app", "timestamp": iso(NOW - datetime.timedelta(hours=2)),
             "message": {"role": "user", "content": "check the build every few minutes"}}]
    for i in range(40):
        t = claude_turn("loop-1111", "/work/app", "claude-opus-5-5", i, NOW - datetime.timedelta(minutes=120 - 3 * i), 500_000)
        loop.append(t)
        if i == 0:
            loop.append(t)  # the same response logged twice must count once
    write_jsonl(os.path.join(claude, "-work-app", "loop-1111.jsonl"), loop)
    # a normal session: 5 sonnet turns two days ago
    small = [claude_turn("small-2222", "/work/site", "claude-sonnet-5-5", i, NOW - datetime.timedelta(days=2, minutes=i), 20_000)
             for i in range(5)]
    write_jsonl(os.path.join(claude, "-work-site", "small-2222.jsonl"), small)
    # usage older than the window is ignored
    old = [claude_turn("old-3333", "/work/old", "claude-opus-5-5", 0, NOW - datetime.timedelta(days=20), 900_000)]
    write_jsonl(os.path.join(claude, "-work-old", "old-3333.jsonl"), old)

    codex = os.path.join(tmp, "codex", "sessions", "2030", "01", "10")
    resets = (NOW + datetime.timedelta(days=3)).timestamp()
    rows = [{"type": "session_meta", "timestamp": iso(NOW - datetime.timedelta(hours=5)),
             "payload": {"id": "codex-session-api1", "cwd": "/work/api"}},
            {"type": "turn_context", "timestamp": iso(NOW - datetime.timedelta(hours=5)), "payload": {"model": "gpt-test", "cwd": "/work/api"}},
            {"type": "event_msg", "timestamp": iso(NOW - datetime.timedelta(hours=5)), "payload": {"type": "user_message", "message": "add an endpoint"}}]
    for i in range(10):
        rows.append({"type": "event_msg", "timestamp": iso(NOW - datetime.timedelta(hours=4, minutes=-i)), "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 100_000, "cached_input_tokens": 90_000, "output_tokens": 1000}},
            "rate_limits": {"primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": resets},
                            "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": resets}}}})
    write_jsonl(os.path.join(codex, "rollout-2030-01-10-api.jsonl"), rows)


def run(tmp, *args):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(tmp, "claude"), CODEX_HOME=os.path.join(tmp, "codex"),
               XDG_CACHE_HOME=os.path.join(tmp, "cache"), PYTHONPATH=ROOT)
    r = subprocess.run([sys.executable, "-m", "claude_usage_meter", *args, "--now", iso(NOW)],
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        check.failed = True


check.failed = False


def main():
    with tempfile.TemporaryDirectory() as tmp:
        build(tmp)
        code, out = run(tmp, "--json")
        check(code == 0, "report runs")
        claude, codex = json.loads(out)
        check(claude["sessions_count"] == 2, "claude: 2 sessions in the week (the 20-day-old one is out)")
        loop = claude["sessions"][0]
        check(loop["id"] == "loop-1111" and loop["turns"] == 40, "claude: duplicate lines count once (40 turns)")
        # opus: 40 * (10*5 + 200*25 + 1000*6.25 + 500000*0.5) / 1e6
        check(abs(loop["proxy_usd"] - 40 * (50 + 5000 + 6250 + 250000) / 1e6) < 0.01, "claude: opus price weighting")
        check(loop["first_prompt"] == "check the build every few minutes" and loop["project"] == "app", "claude: first prompt and project")
        check(claude["weekly_used_percent"] is None, "claude: weekly % unknown without --live")
        check(codex["weekly_used_percent"] == 40.0, "codex: weekly window picked by window_minutes, not position")
        s = codex["sessions"][0]
        check(s["turns"] == 10 and s["points"] == 40.0 and s["project"] == "api", "codex: one session holds all 40 points")
        check(abs(s["proxy_usd"] - 10 * (10_000 * 1.25 + 1000 * 10 + 90_000 * 0.125) / 1e6) < 0.01, "codex: cached input priced apart")

        code, out = run(tmp)
        check(code == 0 and "Claude Code:" in out and "Codex:" in out and "oop-1111" in out, "text report prints both engines")

        code, out = run(tmp, "watch", "--dry")
        check("WOULD ALERT: claude session loop-1111" in out and "small-2222" not in out, "watch: flags the loop only")

        sink = os.path.join(tmp, "alert.txt")
        code, out = run(tmp, "watch", "--notify", f"cat > {sink}", "--exit-code")
        check(code == 1 and os.path.exists(sink) and "loop-1111" in open(sink).read(), "watch: --notify gets the message on stdin")
        code, out = run(tmp, "watch", "--exit-code")
        check(code == 0 and "ALERT" not in out, "watch: alerts once per session, not every run")

        code, out = run(tmp, "watch", "--alert-usd", "100000", "--fat-ctx", "10000000")
        check(code == 0 and out.startswith("ok:"), "watch: thresholds are configurable")
    if check.failed:
        sys.exit(1)
    print("smoke: all passed")


if __name__ == "__main__":
    main()
