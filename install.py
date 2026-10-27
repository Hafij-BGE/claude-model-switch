#!/usr/bin/env python3
"""Install or uninstall claude-model-switch into ~/.claude.

  python3 install.py                 install / upgrade (idempotent)
  python3 install.py --keep-model    do not change the default main model
  python3 install.py --uninstall     remove hooks and managed agents, restore model settings
  python3 install.py --uninstall --purge   also delete logs, lessons and policy history

settings.json is backed up before every change. Unrelated keys and hooks are left alone.
"""
import argparse
import json
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.abspath(__file__))
CLAUDE = os.environ.get("CMS_CLAUDE_DIR") or os.path.expanduser("~/.claude")
HOME = os.path.join(CLAUDE, "model-switch")
os.environ.setdefault("CMS_HOME", HOME)
os.environ.setdefault("CMS_AGENTS_DIR", os.path.join(CLAUDE, "agents"))
SETTINGS = os.path.join(CLAUDE, "settings.json")
META = os.path.join(HOME, "install-meta.json")
HOOK = os.path.join(HOME, "app", "cms_hook.py")
TAG = "model-switch/app/cms_hook.py"

EVENTS = [  # (event, matcher)
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "Agent|Task"),
    ("PostToolUseFailure", None),
    ("Stop", None),
    ("SubagentStop", None),
    ("PostModelSwitch", None),
]
MANAGED_KEYS = {"model": "sonnet", "autoCompactWindow": 200000}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def strip_ours(settings):
    hooks = settings.get("hooks", {})
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            g = dict(g, hooks=[h for h in g.get("hooks", []) if TAG not in h.get("command", "")])
            if g["hooks"]:
                groups.append(g)
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if not hooks:
        settings.pop("hooks", None)


def backup():
    os.makedirs(os.path.join(HOME, "backup"), exist_ok=True)
    if os.path.exists(SETTINGS):
        dst = os.path.join(HOME, "backup", "settings.%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(SETTINGS, dst)
        return dst


def install(a):
    if os.path.exists(SETTINGS) and load(SETTINGS, None) is None:
        sys.exit("settings.json is not valid JSON; fix it first: " + SETTINGS)
    os.makedirs(os.path.join(HOME, "app"), exist_ok=True)
    for f in ("cms_lib.py", "cms_hook.py", "cms.py"):
        shutil.copy2(os.path.join(SRC, "app", f), os.path.join(HOME, "app", f))
    print("backup:", backup())

    s = load(SETTINGS, {})
    meta = load(META, {})
    if "previous" not in meta:  # remember the user's own values once, for uninstall
        meta["previous"] = {k: s.get(k) for k in MANAGED_KEYS}
    meta["installed"] = int(time.time())
    managed = {k: v for k, v in MANAGED_KEYS.items() if not (a.keep_model and k == "model")}
    meta["managed"] = sorted(managed)
    s.update(managed)

    strip_ours(s)
    hooks = s.setdefault("hooks", {})
    cmd = 'python3 "%s"' % HOOK
    for ev, matcher in EVENTS:
        g = {"hooks": [{"type": "command", "command": cmd, "timeout": 15}]}
        if matcher:
            g["matcher"] = matcher
        hooks.setdefault(ev, []).append(g)
    save(SETTINGS, s)
    save(META, meta)

    sys.path.insert(0, os.path.join(HOME, "app"))
    import cms_lib as L
    import cms
    L.ensure_dirs()
    if not os.path.exists(L.POLICY):
        L.save_policy(dict(L.DEFAULT_POLICY), "initial install")
    else:
        L.render_agents(L.load_policy())
    if not os.path.exists(os.path.join(HOME, "baseline.json")):
        b = cms.baseline(14)
        print("baseline: %d turns, median ctx/call %d, opus share %.0f%%" % (
            b["turns"], b["median_ctx"], b["opus_call_share"] * 100))
    print("installed: %s" % ", ".join("%s=%s" % kv for kv in managed.items()))
    print("hooks: %s" % ", ".join(e for e, _ in EVENTS))
    print("agents: scout, expert in %s" % os.environ["CMS_AGENTS_DIR"])
    print("CLI: python3 %s/app/cms.py status|report|tune|rollback|lesson" % HOME)


def uninstall(a):
    print("backup:", backup())
    s = load(SETTINGS, {})
    meta = load(META, {})
    strip_ours(s)
    for k in meta.get("managed", []):
        prev = meta.get("previous", {}).get(k)
        if s.get(k) != MANAGED_KEYS[k]:
            continue  # the user changed it since install: leave their value
        if prev is None:
            s.pop(k, None)
        else:
            s[k] = prev
    save(SETTINGS, s)
    agents = os.environ["CMS_AGENTS_DIR"]
    for name in ("scout", "expert"):
        p = os.path.join(agents, name + ".md")
        if os.path.exists(p) and "managed by claude-model-switch" in open(p).read():
            os.remove(p)
    if a.purge:
        shutil.rmtree(HOME, ignore_errors=True)
        print("purged", HOME)
    else:
        os.remove(META) if os.path.exists(META) else None
        print("kept data in", HOME)
    print("uninstalled")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--keep-model", action="store_true")
    a = ap.parse_args()
    uninstall(a) if a.uninstall else install(a)
