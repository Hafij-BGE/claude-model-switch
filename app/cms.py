#!/usr/bin/env python3
"""cms: report, tune, roll back and record lessons for claude-model-switch.

  cms.py status                 current policy, lessons, log size
  cms.py report [--days N]      usage and outcome indicators vs the pre-install baseline
  cms.py tune [--dry-run]       evaluate the log and apply evidence-backed policy changes
  cms.py rollback [VERSION]     restore an earlier policy (default: the previous one)
  cms.py baseline [--days N]    snapshot usage from transcripts (run once at install)
  cms.py lesson add [--verified] TEXT | list | verify ID | rm ID
"""
import argparse
import glob
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cms_lib as L  # noqa: E402

DAY = 86400
# Weights for "input-equivalent units": cache reads ~0.1x input, cache writes ~1.25x, output ~5x.
# These are the usual Anthropic ratios within one model; units are NOT comparable across model families.
W = {"in": 1.0, "cw": 1.25, "cr": 0.1, "out": 5.0}


def family(model):
    m = (model or "").lower()
    for f in ("opus", "sonnet", "haiku", "fable"):
        if f in m:
            return f
    return "other"


def units(r):
    return sum(W[k] * r.get(k, 0) for k in W)


# ---------- baseline from raw transcripts ----------

def baseline(days=14, before=None, save=True):
    before = before or time.time()
    cut = before - days * DAY
    fam = {}
    turns = 0
    ctxs = []
    for f in glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True):
        if os.path.getmtime(f) < cut:
            continue
        is_sub = "/subagents/" in f
        seen = set()
        for line in open(f, errors="ignore"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = r.get("message") or {}
            if not is_sub and r.get("type") == "user" and isinstance(m.get("content"), str) and not r.get("isMeta"):
                turns += 1
            u = m.get("usage")
            if r.get("type") != "assistant" or not u or m.get("id") in seen or m.get("model") == "<synthetic>":
                continue
            seen.add(m.get("id"))
            a = fam.setdefault(family(m.get("model")), {"calls": 0, "in": 0, "out": 0, "cr": 0, "cw": 0})
            a["calls"] += 1
            a["in"] += u.get("input_tokens") or 0
            a["out"] += u.get("output_tokens") or 0
            a["cr"] += u.get("cache_read_input_tokens") or 0
            a["cw"] += u.get("cache_creation_input_tokens") or 0
            ctxs.append(L.usage_ctx(u))
    calls = sum(a["calls"] for a in fam.values())
    b = {"ts": int(before), "days": days, "turns": turns, "calls": calls, "families": fam,
         "median_ctx": int(statistics.median(ctxs)) if ctxs else 0,
         "opus_call_share": round(fam.get("opus", {}).get("calls", 0) / calls, 3) if calls else 0}
    if save:
        L.ensure_dirs()
        L.write_json(os.path.join(L.HOME, "baseline.json"), b)
    return b


# ---------- evidence from the hook log ----------

def collect(days=30):
    recs = L.read_log(days)
    turns = [r for r in recs if r.get("kind") == "turn"]
    by_sid = {}
    for t in turns:
        by_sid.setdefault(t["sid"], []).append(t)
    for c in (r for r in recs if r.get("kind") == "correction"):
        prev = [t for t in by_sid.get(c["sid"], []) if t["ts"] <= c["ts"]]
        if prev:
            prev[-1]["corrected"] = True
    for t in turns:
        t["trouble"] = bool(t.get("corrected") or t.get("nudged") or t.get("test_fail") or t.get("denied")
                            or any(a["type"] == "expert" for a in t.get("agents", [])))
    agents = [r for r in recs if r.get("kind") == "agent"]
    switches = [r for r in recs if r.get("kind") == "switch"]
    return turns, agents, switches


def scout_stats(turns, since=0):
    n = bad = 0
    for t in turns:
        if t["ts"] < since:
            continue
        seq = t.get("agents", [])
        for i, a in enumerate(seq):
            if a["type"] != "scout":
                continue
            n += 1
            # Proxy for "scout was not enough": another agent was needed later in the same turn,
            # or the user corrected the turn.
            if i < len(seq) - 1 or t.get("corrected"):
                bad += 1
    return n, (bad / n if n else 0.0)


def rate(xs, key):
    return sum(1 for x in xs if x.get(key)) / len(xs) if xs else 0.0


def avoided(p, item):
    return any(a["item"] == item and a["until"] > time.time() for a in p.get("avoid", []))


def tune(apply=True, quiet=False, days=30):
    p = L.load_policy()
    turns, agents, switches = collect(days)
    changes, notes = [], []

    # A. verify the last tier change, roll back if it did not hold up
    pend = p.get("pending_check")
    if pend:
        n, r = scout_stats(turns, since=pend["since"])
        if n >= 10:
            worse = (pend["dir"] == "up" and r > pend["before"] - 0.10) or \
                    (pend["dir"] == "down" and r > pend["before"] + 0.15)
            if worse:
                p["agent_models"]["scout"] = pend["prev"]
                p.setdefault("avoid", []).append({"item": "scout:" + pend["new"], "until": time.time() + 60 * DAY,
                                                 "why": "bad rate %.2f -> %.2f (n=%d)" % (pend["before"], r, n)})
                changes.append("rolled back scout %s -> %s (bad %.2f, n=%d)" % (pend["new"], pend["prev"], r, n))
            else:
                notes.append("scout on %s held up (bad %.2f, n=%d): kept" % (pend["new"], r, n))
            p.pop("pending_check")

    # B. scout tier from evidence
    if "pending_check" not in p and not changes:
        n, r = scout_stats(turns)
        cur = p["agent_models"]["scout"]
        new = None
        if cur == "haiku" and n >= 15 and r >= 0.35 and not avoided(p, "scout:sonnet"):
            new, d = "sonnet", "up"
        elif cur == "sonnet" and n >= 20 and r <= 0.10 and not avoided(p, "scout:haiku"):
            new, d = "haiku", "down"
        if new:
            p["agent_models"]["scout"] = new
            p["pending_check"] = {"since": int(time.time()), "before": r, "prev": cur, "new": new, "dir": d}
            changes.append("scout %s -> %s (bad rate %.2f, n=%d)" % (cur, new, r, n))

    # C. per-project start-model hints (the user switches the main model; the hook only advises)
    hints = {k: v for k, v in p.get("project_hints", {}).items() if v["ts"] > time.time() - 30 * DAY}
    by_cwd = {}
    for t in turns:
        by_cwd.setdefault(t.get("cwd", ""), []).append(t)
    for cwd, ts in by_cwd.items():
        son = [t for t in ts if family(t["model"]) == "sonnet"]
        opu = [t for t in ts if family(t["model"]) == "opus"]
        tr = rate(son, "trouble")
        old = hints.get(cwd, {}).get("start")
        if len(son) >= 10 and tr >= 0.4 and old != "opus":
            hints[cwd] = {"ts": int(time.time()), "start": "opus",
                          "text": "%d%% of recent Sonnet turns here needed escalation or correction (n=%d). "
                                  "Suggest the user picks Opus for substantial work here." % (tr * 100, len(son))}
        elif old != "sonnet" and len(opu) >= 15 and rate(opu, "trouble") <= 0.10 and \
                statistics.median(t["calls"] for t in opu) <= 6:
            hints[cwd] = {"ts": int(time.time()), "start": "sonnet",
                          "text": "Recent Opus turns here were routine (n=%d, median %d calls). Sonnet should "
                                  "suffice; mention it if the user is on Opus." % (
                                      len(opu), statistics.median(t["calls"] for t in opu))}
    if {k: v["start"] for k, v in hints.items()} != {k: v["start"] for k, v in p.get("project_hints", {}).items()}:
        changes.append("project hints: %s" % (", ".join("%s=%s" % (os.path.basename(k) or k, v["start"])
                                                        for k, v in hints.items()) or "cleared"))
        p["project_hints"] = hints

    # D. failure-nudge threshold
    nud = [t for t in turns if t.get("nudged")]
    thr = p["escalate_after_failures"]
    if len(nud) >= 10 and rate(nud, "corrected") >= 0.5 and thr > 2:
        p["escalate_after_failures"] = thr - 1
        changes.append("escalate_after_failures %d -> %d (nudged turns still corrected %.0f%%)" % (
            thr, thr - 1, rate(nud, "corrected") * 100))
    elif len(nud) >= 15 and rate(nud, "corrected") <= 0.10 and thr < 5:
        p["escalate_after_failures"] = thr + 1
        changes.append("escalate_after_failures %d -> %d (nudges rarely needed)" % (thr, thr + 1))

    # E. hypotheses worth a human look; never injected until verified
    exp_turns = [t for t in turns if any(a["type"] == "expert" for a in t.get("agents", []))]
    if len(exp_turns) >= 10 and rate(exp_turns, "corrected") >= 0.5:
        add_lesson("Expert escalations were followed by a user correction in %.0f%% of %d turns: briefs may lack "
                   "evidence." % (rate(exp_turns, "corrected") * 100, len(exp_turns)), "hypothesis", dedupe="expert-brief")

    if apply:
        L.ensure_dirs()
        L.write_json(os.path.join(L.HOME, "last_tune.json"), {"ts": int(time.time()), "changes": changes})
        if changes:
            L.save_policy(p, "; ".join(changes))
    if not quiet:
        print("evidence: %d turns, %d agent runs, %d model switches (last %d days)" % (
            len(turns), len(agents), len(switches), days))
        for c in changes:
            print(("applied: " if apply else "would apply: ") + c)
        for n_ in notes:
            print("note: " + n_)
        if not changes and not notes:
            print("no change: not enough evidence or nothing outside thresholds")
    return changes


# ---------- lessons ----------

def add_lesson(text, status="hypothesis", dedupe=None):
    ls = L.load_lessons()
    key = dedupe or text
    if any(l.get("key") == key for l in ls):
        return None
    lid = max([l["id"] for l in ls] or [0]) + 1
    ls.append({"id": lid, "key": key, "text": text.strip()[:240], "status": status, "ts": int(time.time())})
    L.ensure_dirs()
    L.write_json(L.LESSONS, ls)
    return lid


def lesson_cmd(a):
    ls = L.load_lessons()
    if a.action == "add":
        lid = add_lesson(" ".join(a.text), "verified" if a.verified else "hypothesis")
        print("added #%s" % lid if lid else "duplicate, not added")
    elif a.action == "list":
        for l in ls:
            print("#%d [%s] %s" % (l["id"], l["status"], l["text"]))
    elif a.action in ("verify", "rm"):
        lid = int(a.text[0])
        if a.action == "rm":
            ls = [l for l in ls if l["id"] != lid]
        else:
            for l in ls:
                if l["id"] == lid:
                    l["status"] = "verified"
        L.write_json(L.LESSONS, ls)
        print("ok")


# ---------- reporting ----------

def report(days):
    b = L.read_json(os.path.join(L.HOME, "baseline.json"), None)
    turns, agents, switches = collect(days)
    fam = {}
    for r in turns + agents:
        a = fam.setdefault(family(r.get("model")), {"calls": 0, "in": 0, "out": 0, "cr": 0, "cw": 0})
        for k in a:
            a[k] += r.get(k, 0)
    calls = sum(a["calls"] for a in fam.values())

    def fmt(f, nturns):
        rows = []
        for k, a in sorted(f.items()):
            per = units(a) / nturns if nturns else 0
            rows.append("  %-7s calls=%-7d mean ctx/call=%7.0f  units/turn=%9.0f" % (
                k, a["calls"], (a["in"] + a["cr"] + a["cw"]) / max(a["calls"], 1), per))
        return "\n".join(rows)

    if b:
        print("BASELINE (%d days before %s): %d turns, %d calls, median ctx/call %d, opus share %.0f%%" % (
            b["days"], time.strftime("%F", time.localtime(b["ts"])), b["turns"], b["calls"], b["median_ctx"],
            b["opus_call_share"] * 100))
        print(fmt(b["families"], b["turns"]))
    ctxs = [t["max_ctx"] for t in turns]
    print("SINCE INSTALL (last %d days): %d turns, %d calls, median peak ctx/turn %d, opus share %.0f%%" % (
        days, len(turns), calls, statistics.median(ctxs) if ctxs else 0,
        100 * fam.get("opus", {}).get("calls", 0) / calls if calls else 0))
    print(fmt(fam, len(turns)))
    if turns:
        print("outcomes: corrected %.0f%%, nudged %.0f%%, test failures in %.0f%% of turns, caps hit %d, "
              "compactions %d, model switches %d" % (
                  rate(turns, "corrected") * 100, rate(turns, "nudged") * 100, rate(turns, "test_fail") * 100,
                  sum(t.get("denied", 0) for t in turns), sum(1 for t in turns if t.get("compacted")), len(switches)))
    by = {}
    for a in agents:
        by.setdefault(a["type"], []).append(a)
    for k, xs in sorted(by.items()):
        print("  agent %-16s runs=%-4d model=%s mean calls=%.1f errors/run=%.1f" % (
            k, len(xs), family(xs[-1]["model"]), statistics.mean(x["calls"] for x in xs),
            statistics.mean(x["tool_errors"] for x in xs)))
    n, r = scout_stats(turns)
    if n:
        print("  scout insufficient-rate %.2f (n=%d)" % (r, n))
    print("note: units weight in=1, cache write=1.25, cache read=0.1, output=5 within a family; "
          "compare families by call share, not units.")


def status():
    p = L.load_policy()
    print(json.dumps({k: p[k] for k in p if k != "avoid"}, indent=1))
    if p.get("avoid"):
        print("avoid:", [a for a in p["avoid"] if a["until"] > time.time()])
    print("lessons: %d (%d verified)" % (len(L.load_lessons()),
                                         sum(1 for l in L.load_lessons() if l["status"] == "verified")))
    print("log records: %d; history versions: %s" % (
        len(L.read_log()), sorted(os.listdir(L.HISTORY)) if os.path.isdir(L.HISTORY) else []))
    lt = L.read_json(os.path.join(L.HOME, "last_tune.json"), {})
    if lt:
        print("last tune: %s %s" % (time.strftime("%F %T", time.localtime(lt["ts"])), lt.get("changes")))


def rollback(version):
    files = sorted(os.listdir(L.HISTORY)) if os.path.isdir(L.HISTORY) else []
    if not files:
        print("no earlier policy")
        return 1
    name = "v%04d.json" % version if version is not None else files[-1]
    old = L.read_json(os.path.join(L.HISTORY, name), None)
    if old is None:
        print("not found:", name)
        return 1
    old.pop("pending_check", None)
    p = L.save_policy(old, "manual rollback to %s" % name)
    print("restored %s as v%d" % (name, p["version"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cms")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=30)
    t = sub.add_parser("tune")
    t.add_argument("--dry-run", action="store_true")
    rb = sub.add_parser("rollback")
    rb.add_argument("version", nargs="?", type=int)
    b = sub.add_parser("baseline")
    b.add_argument("--days", type=int, default=14)
    le = sub.add_parser("lesson")
    le.add_argument("action", choices=["add", "list", "verify", "rm"])
    le.add_argument("--verified", action="store_true")
    le.add_argument("text", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        status()
    elif a.cmd == "report":
        report(a.days)
    elif a.cmd == "tune":
        tune(apply=not a.dry_run)
    elif a.cmd == "rollback":
        return rollback(a.version)
    elif a.cmd == "baseline":
        print(json.dumps(baseline(a.days), indent=1))
    elif a.cmd == "lesson":
        lesson_cmd(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
