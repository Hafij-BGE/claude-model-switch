#!/usr/bin/env python3
"""Claude Code hook entry point. One script, dispatched on hook_event_name.

Never blocks the session on its own failure: any exception is logged to
errors.log and the hook exits 0 with no output.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cms_lib as L  # noqa: E402


def out(event, **fields):
    print(json.dumps({"hookSpecificOutput": dict(hookEventName=event, **fields)}))


def session_start(h):
    if not os.path.exists(L.POLICY):
        L.save_policy(dict(L.DEFAULT_POLICY), "initial")
    p = L.load_policy()
    last = L.read_json(os.path.join(L.HOME, "last_tune.json"), {}).get("ts", 0)
    if time.time() - last > p.get("tune_every_days", 7) * 86400:
        import cms
        cms.tune(apply=True, quiet=True)
        p = L.load_policy()
    out("SessionStart", additionalContext=L.render_context(p, h.get("cwd", "")))


def prompt_submit(h):
    sid = h.get("session_id")
    st = L.load_state(sid)
    prompt = h.get("prompt") or ""
    if st.get("turns") and len(prompt) < 400 and L.CORRECTION_RE.search(prompt):
        L.append_log({"kind": "correction", "sid": L.sid_hash(sid)})
    st["turn"] = {"agents": 0, "expert": 0, "fails": 0, "nudged": False, "denied": 0}
    st["turns"] = st.get("turns", 0) + 1
    p = L.load_policy()
    ctx = L.last_context(h.get("transcript_path"))
    msg = None
    if ctx >= p["context_warn_tokens"] and not st.get("ctx_warned"):
        st["ctx_warned"] = True
        msg = ("[model-switch] Context is ~%dk tokens and every call re-reads it. If this prompt starts a task "
               "unrelated to the work so far, tell the user a fresh session would be cheaper." % (ctx // 1000))
    elif ctx and ctx < p["context_warn_tokens"] // 2:
        st["ctx_warned"] = False  # re-arm after a compaction
    L.save_state(sid, st)
    if msg:
        out("UserPromptSubmit", additionalContext=msg)


def pre_agent(h):
    p = L.load_policy()
    caps = p["caps"]
    if h.get("agent_id"):
        out("PreToolUse", permissionDecision="deny",
            permissionDecisionReason="model-switch: subagents must not delegate further. Do the work yourself or report back.")
        return
    sid = h.get("session_id")
    st = L.load_state(sid)
    t = st.setdefault("turn", {"agents": 0, "expert": 0, "fails": 0, "nudged": False, "denied": 0})
    inp = h.get("tool_input") or {}
    is_expert = inp.get("subagent_type") == "expert" or inp.get("model") == "opus"
    reason = None
    if t["agents"] >= caps["agents_per_turn"]:
        reason = "agent cap reached (%d this turn). Finish with the results you have, or ask the user." % caps["agents_per_turn"]
    elif is_expert and t["expert"] >= caps["expert_per_turn"]:
        reason = ("expert cap reached (%d this turn). The expert already tried: stop and report the blocker "
                  "to the user instead of escalating again." % caps["expert_per_turn"])
    if reason:
        t["denied"] += 1
        L.save_state(sid, st)
        out("PreToolUse", permissionDecision="deny", permissionDecisionReason="model-switch: " + reason)
        return
    t["agents"] += 1
    t["expert"] += int(is_expert)
    L.save_state(sid, st)


def tool_failure(h):
    if h.get("agent_id") or h.get("is_interrupt"):
        return
    sid = h.get("session_id")
    st = L.load_state(sid)
    t = st.setdefault("turn", {"agents": 0, "expert": 0, "fails": 0, "nudged": False, "denied": 0})
    t["fails"] += 1
    p = L.load_policy()
    msg = None
    if t["fails"] >= p["escalate_after_failures"] and not t["nudged"]:
        t["nudged"] = True
        msg = ("[model-switch] %d tool failures this turn. Do not repeat the same approach. Either change approach "
               "once with a stated reason, or escalate: Agent subagent_type=expert with a full brief." % t["fails"])
    L.save_state(sid, st)
    if msg:
        out("PostToolUseFailure", additionalContext=msg)


def main_model(models):
    return max(models, key=models.get) if models else ""


def stop(h):
    sid = h.get("session_id")
    st = L.load_state(sid)
    s, st["offset"] = L.scan_transcript(h.get("transcript_path"), st.get("offset", 0))
    t = st.get("turn") or {}
    L.save_state(sid, st)
    if not s["calls"]:
        return
    L.append_log({"kind": "turn", "sid": L.sid_hash(sid), "cwd": h.get("cwd", ""),
                  "model": main_model(s["models"]), "calls": s["calls"], "in": s["in"], "out": s["out"],
                  "cr": s["cr"], "cw": s["cw"], "max_ctx": s["max_ctx"], "tool_errors": s["tool_errors"],
                  "test_fail": s["test_fail"], "agents": s["agents"], "nudged": bool(t.get("nudged")),
                  "denied": t.get("denied", 0), "compacted": s["compacted"]})


def subagent_stop(h):
    s, _ = L.scan_transcript(h.get("agent_transcript_path"), 0)
    if not s["calls"]:
        return
    L.append_log({"kind": "agent", "sid": L.sid_hash(h.get("session_id")), "cwd": h.get("cwd", ""),
                  "type": h.get("agent_type") or "?", "model": main_model(s["models"]), "calls": s["calls"],
                  "in": s["in"], "out": s["out"], "cr": s["cr"], "cw": s["cw"], "max_ctx": s["max_ctx"],
                  "tool_errors": s["tool_errors"], "empty": not (h.get("last_assistant_message") or "").strip()})


def model_switch(h):
    L.append_log({"kind": "switch", "sid": L.sid_hash(h.get("session_id")), "cwd": h.get("cwd", ""),
                  "from": h.get("from_model"), "to": h.get("to_model"), "source": h.get("source")})


HANDLERS = {
    "SessionStart": session_start,
    "UserPromptSubmit": prompt_submit,
    "PreToolUse": pre_agent,
    "PostToolUseFailure": tool_failure,
    "Stop": stop,
    "SubagentStop": subagent_stop,
    "PostModelSwitch": model_switch,
}


def main():
    try:
        h = json.load(sys.stdin)
        fn = HANDLERS.get(h.get("hook_event_name"))
        if fn:
            L.ensure_dirs()
            fn(h)
    except Exception as e:  # never break the user's session
        L.log_error("hook", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
