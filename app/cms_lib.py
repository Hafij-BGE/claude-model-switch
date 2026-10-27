"""Shared state, transcript parsing and policy rendering for claude-model-switch.

Everything lives under ~/.claude/model-switch (override with CMS_HOME, used by tests).
Only metadata is stored: token counts, models, agent types, error counts, cwd.
Prompt text and file contents are never written.
"""
import json
import os
import re
import time
import hashlib

HOME = os.environ.get("CMS_HOME") or os.path.expanduser("~/.claude/model-switch")
APP = os.path.join(HOME, "app")
POLICY = os.path.join(HOME, "policy.json")
HISTORY = os.path.join(HOME, "policy-history")
LOG = os.path.join(HOME, "log.jsonl")
LESSONS = os.path.join(HOME, "lessons.json")
STATE = os.path.join(HOME, "state")
ERRORS = os.path.join(HOME, "errors.log")
AGENTS_DIR = os.environ.get("CMS_AGENTS_DIR") or os.path.expanduser("~/.claude/agents")

DEFAULT_POLICY = {
    "version": 1,
    "main_model": "sonnet",
    "agent_models": {"scout": "haiku", "expert": "opus"},
    "caps": {"agents_per_turn": 4, "expert_per_turn": 1},
    "escalate_after_failures": 3,
    "context_warn_tokens": 150000,
    "project_hints": {},
    "avoid": [],
    "tune_every_days": 7,
}

# Short prompts that usually mean the previous turn missed. Proxy only.
CORRECTION_RE = re.compile(
    r"\b(that'?s (wrong|not (it|right))|wrong|doesn'?t work|does not work|not working|"
    r"still (broken|fail\w*|wrong|not)|didn'?t (work|fix)|revert|undo that|try again)\b",
    re.I,
)
TEST_CMD_RE = re.compile(r"\b(pytest|unittest|npm (run )?test|pnpm test|yarn test|go test|cargo test|"
                         r"gradle\w* test|mvn test|ctest|make (check|test)|vitest|jest)\b")


def ensure_dirs():
    for d in (HOME, HISTORY, STATE):
        os.makedirs(d, exist_ok=True)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def append_log(rec):
    rec.setdefault("ts", int(time.time()))
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def read_log(days=None):
    cut = time.time() - days * 86400 if days else 0
    out = []
    try:
        with open(LOG) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= cut:
                    out.append(r)
    except OSError:
        pass
    return out


def load_policy():
    p = dict(DEFAULT_POLICY)
    p.update(read_json(POLICY, {}))
    return p


def save_policy(p, reason):
    """Archive the current policy, then write the new one with a bumped version."""
    ensure_dirs()
    old = read_json(POLICY, None)
    if old is not None:
        write_json(os.path.join(HISTORY, "v%04d.json" % old.get("version", 0)), old)
    p["version"] = (old or {}).get("version", 0) + 1
    p["changed"] = {"ts": int(time.time()), "reason": reason}
    write_json(POLICY, p)
    render_agents(p)
    return p


def sid_hash(sid):
    return hashlib.sha1((sid or "").encode()).hexdigest()[:10]


def session_state(sid):
    return os.path.join(STATE, sid_hash(sid) + ".json")


def load_state(sid):
    return read_json(session_state(sid), {})


def save_state(sid, st):
    ensure_dirs()
    write_json(session_state(sid), st)


def usage_ctx(u):
    return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
        (u.get("cache_creation_input_tokens") or 0)


def scan_transcript(path, offset=0):
    """Aggregate assistant usage, tool errors and Agent launches from byte `offset` on.

    Returns (summary, new_offset). Assistant messages are deduped by message id,
    because one API response is split over several transcript lines.
    """
    s = {"calls": 0, "in": 0, "out": 0, "cr": 0, "cw": 0, "max_ctx": 0, "last_ctx": 0,
         "models": {}, "tool_errors": 0, "test_fail": 0, "agents": [], "compacted": False}
    seen = set()
    test_ids = set()
    try:
        f = open(path, "rb")
    except (OSError, TypeError):
        return s, offset
    with f:
        size = os.fstat(f.fileno()).st_size
        if offset > size:
            offset = 0
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # partial line still being written
            offset += len(raw)
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if r.get("isCompactSummary") or r.get("subtype") == "compact_boundary":
                s["compacted"] = True
            m = r.get("message") or {}
            content = m.get("content")
            if r.get("type") == "assistant":
                u = m.get("usage")
                mid = m.get("id")
                model = m.get("model", "?")
                if u and mid not in seen and model != "<synthetic>":
                    seen.add(mid)
                    s["calls"] += 1
                    s["in"] += u.get("input_tokens") or 0
                    s["out"] += u.get("output_tokens") or 0
                    s["cr"] += u.get("cache_read_input_tokens") or 0
                    s["cw"] += u.get("cache_creation_input_tokens") or 0
                    c = usage_ctx(u)
                    s["max_ctx"] = max(s["max_ctx"], c)
                    s["last_ctx"] = c
                    s["models"][model] = s["models"].get(model, 0) + 1
                for b in content if isinstance(content, list) else []:
                    if b.get("type") != "tool_use":
                        continue
                    inp = b.get("input") or {}
                    if b.get("name") in ("Agent", "Task"):
                        s["agents"].append({"type": inp.get("subagent_type") or "general-purpose",
                                            "model": inp.get("model") or ""})
                    elif b.get("name") == "Bash" and TEST_CMD_RE.search(inp.get("command") or ""):
                        test_ids.add(b.get("id"))
            elif r.get("type") == "user" and isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result" and b.get("is_error"):
                        s["tool_errors"] += 1
                        if b.get("tool_use_id") in test_ids:
                            s["test_fail"] += 1
    return s, offset


def last_context(path, tail=400000):
    """Context size of the newest assistant call, read from the transcript tail."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - tail))
            lines = f.read().splitlines()
    except (OSError, TypeError):
        return 0
    for raw in reversed(lines):
        if b'"usage"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        u = (r.get("message") or {}).get("usage")
        if r.get("type") == "assistant" and u:
            return usage_ctx(u)
    return 0


def load_lessons():
    return read_json(LESSONS, [])


def render_context(p, cwd):
    """The policy text injected at session start. Keep it short: it is paid on every call."""
    am = p["agent_models"]
    lines = [
        "[model-switch policy v%d]" % p.get("version", 0),
        "Routing: recon (find files/usages, read logs, check a fact) -> Agent subagent_type=scout (%s); "
        "do it yourself if it is 1-3 tool calls. Hard reasoning (architecture, root cause after a failed fix, "
        "subtle security/concurrency, final review of a risky change) -> subagent_type=expert (%s) with a full brief: "
        "goal, what failed, evidence, files." % (am["scout"], am["expert"]),
        "Escalation: retry a failing approach once, then escalate one tier; if expert also fails, stop and ask the user. "
        "This session cannot switch its own model: when the whole task needs Opus, tell the user to pick it in the model menu.",
        "Caps (hook-enforced): %d agents/turn, %d expert/turn, no nested delegation." % (
            p["caps"]["agents_per_turn"], p["caps"]["expert_per_turn"]),
        "Context: auto-compact near 200k. For an unrelated new task suggest a fresh session.",
        "Record a lesson only when tests or the user confirm it: `python3 %s/cms.py lesson add --verified \"...\"`." % APP,
    ]
    hint = p.get("project_hints", {}).get(cwd)
    if hint:
        lines.append("This project: %s" % hint["text"])
    verified = [l for l in load_lessons() if l.get("status") == "verified"][-5:]
    if verified:
        lines.append("Lessons: " + " | ".join(l["text"] for l in verified))
    return "\n".join(lines)


AGENT_TEMPLATES = {
    "scout": """---
name: scout
description: Cheap read-only reconnaissance. Use for finding files, locating definitions and usages, reading logs or configs, mapping a directory, or checking one fact in the code. Not for design, root-cause debugging, or edits.
model: {model}
tools: Read, Grep, Glob, Bash
---
You are a read-only scout. Never modify files, install packages, or run commands that change state.
Answer the question you were given and nothing else.
Output: at most 200 words. Use `path:line` references. Quote only the decisive lines.
If you could not find or confirm something, say NOT FOUND or UNSURE. Never guess.
<!-- managed by claude-model-switch -->
""",
    "expert": """---
name: expert
description: Escalation tier for hard problems. Use after a fix attempt failed, or for architecture decisions, subtle concurrency or security reasoning, contradictory evidence, or a final review of a risky change. Give it a full brief: goal, what was tried, evidence, files.
model: {model}
---
You are the escalation tier. The caller is a cheaper model that got stuck or needs a high-stakes judgement.
Work from evidence: read the code and run checks before concluding.
Return: the diagnosis or decision, the evidence for it, a concrete fix or patch plan, and your confidence.
If the brief lacks information you cannot obtain yourself, say exactly what is missing.
Do not delegate to other agents.
<!-- managed by claude-model-switch -->
""",
}


MARKER = "<!-- managed by claude-model-switch -->"


def render_agents(p):
    """Write the managed agents. An agent file the user wrote themselves is never overwritten."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    for name, tpl in AGENT_TEMPLATES.items():
        path = os.path.join(AGENTS_DIR, name + ".md")
        if os.path.exists(path) and MARKER not in open(path).read():
            log_error("render_agents", "kept user-owned %s" % path)
            continue
        with open(path, "w") as f:
            f.write(tpl.format(model=p["agent_models"][name]))


def log_error(where, exc):
    try:
        ensure_dirs()
        with open(ERRORS, "a") as f:
            f.write("%s %s %r\n" % (time.strftime("%F %T"), where, exc))
    except OSError:
        pass
