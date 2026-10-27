"""Run: python3 -m unittest discover -s tests -v   (from the repo root)

Every test uses a throwaway CMS_HOME / agents dir / ~/.claude copy; the real ~/.claude is never touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")


def asst(mid, model="claude-sonnet-5-5", ctx=1000, out=50, tools=()):
    return {"type": "assistant", "message": {"id": mid, "model": model, "content": list(tools),
            "usage": {"input_tokens": 10, "cache_read_input_tokens": ctx - 10, "cache_creation_input_tokens": 0,
                      "output_tokens": out}}}


def tool_use(name, tid, **inp):
    return {"type": "tool_use", "name": name, "id": tid, "input": inp}


def err_result(tid):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": True}]}}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ, CMS_HOME=os.path.join(self.tmp, "ms"),
                        CMS_AGENTS_DIR=os.path.join(self.tmp, "agents"))
        self.tr = os.path.join(self.tmp, "t.jsonl")
        open(self.tr, "w").close()

    def hook(self, event, **h):
        h.setdefault("session_id", "S1")
        h.setdefault("transcript_path", self.tr)
        h.setdefault("cwd", "/proj")
        h["hook_event_name"] = event
        r = subprocess.run([sys.executable, os.path.join(APP, "cms_hook.py")], input=json.dumps(h),
                           capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def cms(self, *args):
        r = subprocess.run([sys.executable, os.path.join(APP, "cms.py")] + list(args), capture_output=True,
                           text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def write(self, *rows):
        with open(self.tr, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def log(self):
        p = os.path.join(self.env["CMS_HOME"], "log.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def policy(self):
        return json.load(open(os.path.join(self.env["CMS_HOME"], "policy.json")))

    def no_errors(self):
        p = os.path.join(self.env["CMS_HOME"], "errors.log")
        self.assertFalse(os.path.exists(p) and open(p).read().strip(), open(p).read() if os.path.exists(p) else "")


class TranscriptTest(Env):
    def test_scan_dedupes_counts_and_resumes(self):
        sys.path.insert(0, APP)
        import cms_lib as L
        a = asst("m1", ctx=5000, tools=[tool_use("Agent", "t1", subagent_type="scout")])
        b = asst("m1", ctx=5000, tools=[tool_use("Bash", "t2", command="python3 -m pytest -q")])
        self.write(a, b, err_result("t2"), asst("m2", model="claude-opus-5-5", ctx=9000))
        with open(self.tr, "a") as f:
            f.write('{"type":"assistant","partial')  # line still being written
        s, off = L.scan_transcript(self.tr, 0)
        self.assertEqual(s["calls"], 2)  # m1 split over two lines counts once
        self.assertEqual(s["max_ctx"], 9000)
        self.assertEqual((s["tool_errors"], s["test_fail"]), (1, 1))
        self.assertEqual(s["agents"], [{"type": "scout", "model": ""}])
        s2, off2 = L.scan_transcript(self.tr, off)
        self.assertEqual((s2["calls"], off2), (0, off))  # partial line not consumed


class HookTest(Env):
    def test_session_start_injects_short_policy_and_writes_agents(self):
        out = self.hook("SessionStart", source="startup")
        ctx = out["additionalContext"]
        self.assertIn("[model-switch policy v1]", ctx)
        self.assertLess(len(ctx) / 4, 450, "policy text must stay small: it is paid on every call")
        scout = open(os.path.join(self.env["CMS_AGENTS_DIR"], "scout.md")).read()
        self.assertIn("model: haiku", scout)
        self.assertIn("model: opus", open(os.path.join(self.env["CMS_AGENTS_DIR"], "expert.md")).read())
        self.no_errors()

    def test_agent_caps_reset_each_turn(self):
        self.hook("UserPromptSubmit", prompt="do x")
        for i in range(4):
            self.assertIsNone(self.hook("PreToolUse", tool_name="Agent", tool_input={"subagent_type": "scout"}))
        d = self.hook("PreToolUse", tool_name="Agent", tool_input={"subagent_type": "scout"})
        self.assertEqual(d["permissionDecision"], "deny")
        self.hook("UserPromptSubmit", prompt="next")
        self.assertIsNone(self.hook("PreToolUse", tool_name="Agent", tool_input={"subagent_type": "expert"}))
        d = self.hook("PreToolUse", tool_name="Agent", tool_input={"subagent_type": "general-purpose", "model": "opus"})
        self.assertEqual(d["permissionDecision"], "deny")  # second Opus-tier agent this turn
        self.assertIn("expert cap", d["permissionDecisionReason"])

    def test_no_nested_delegation(self):
        d = self.hook("PreToolUse", tool_name="Agent", agent_id="a1", tool_input={"subagent_type": "scout"})
        self.assertEqual(d["permissionDecision"], "deny")

    def test_failure_nudge_once_at_threshold(self):
        self.hook("UserPromptSubmit", prompt="fix it")
        outs = [self.hook("PostToolUseFailure", tool_name="Bash", error="exit 1") for _ in range(5)]
        self.assertEqual([o is not None for o in outs], [False, False, True, False, False])
        self.assertIn("expert", outs[2]["additionalContext"])
        self.assertIsNone(self.hook("PostToolUseFailure", tool_name="Bash", agent_id="a1", error="x"))

    def test_stop_logs_turn_and_correction_marks_it(self):
        self.hook("UserPromptSubmit", prompt="add feature")
        self.write(asst("m1", ctx=40000, tools=[tool_use("Agent", "t1", subagent_type="scout")]), asst("m2", ctx=42000))
        self.hook("Stop")
        self.hook("Stop")  # nothing new: no second record
        self.hook("UserPromptSubmit", prompt="that's wrong, it still fails")
        recs = self.log()
        self.assertEqual([r["kind"] for r in recs], ["turn", "correction"])
        t = recs[0]
        self.assertEqual((t["calls"], t["max_ctx"], t["agents"][0]["type"]), (2, 42000, "scout"))
        self.assertNotIn("prompt", json.dumps(recs))  # no prompt text stored
        self.no_errors()

    def test_context_warning_once_then_rearms(self):
        self.write(asst("m1", ctx=160000))
        self.hook("UserPromptSubmit", prompt="a")  # first prompt: state created
        self.assertIsNone(self.hook("UserPromptSubmit", prompt="b"))  # already warned on "a"
        st = [json.load(open(os.path.join(self.env["CMS_HOME"], "state", f)))
              for f in os.listdir(os.path.join(self.env["CMS_HOME"], "state"))][0]
        self.assertTrue(st["ctx_warned"])
        self.write(asst("m2", ctx=30000))  # after compaction
        self.hook("UserPromptSubmit", prompt="c")
        self.write(asst("m3", ctx=170000))
        out = self.hook("UserPromptSubmit", prompt="d")
        self.assertIn("fresh session", out["additionalContext"])

    def test_subagent_and_switch_records(self):
        sub = os.path.join(self.tmp, "agent.jsonl")
        with open(sub, "w") as f:
            f.write(json.dumps(asst("x1", model="claude-haiku-5-5", ctx=8000)) + "\n")
        self.hook("SubagentStop", agent_type="scout", agent_transcript_path=sub, last_assistant_message="found it")
        self.hook("PostModelSwitch", from_model="claude-sonnet-5-5", to_model="claude-opus-5-5", source="user")
        kinds = [(r["kind"], r.get("type") or r.get("to")) for r in self.log()]
        self.assertEqual(kinds, [("agent", "scout"), ("switch", "claude-opus-5-5")])

    def test_hook_survives_garbage(self):
        r = subprocess.run([sys.executable, os.path.join(APP, "cms_hook.py")], input="not json",
                           capture_output=True, text=True, env=self.env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class LearningTest(Env):
    def seed(self, n, bad, cwd="/proj", model="claude-sonnet-5-5", ts=None, trouble=0):
        os.makedirs(self.env["CMS_HOME"], exist_ok=True)
        ts = ts or int(time.time()) - 100
        with open(os.path.join(self.env["CMS_HOME"], "log.jsonl"), "a") as f:
            for i in range(n):
                agents = [{"type": "scout", "model": ""}] + ([{"type": "general-purpose", "model": ""}] if i < bad else [])
                rec = {"kind": "turn", "ts": ts, "sid": "s%d" % i, "cwd": cwd, "model": model, "calls": 4,
                       "agents": agents, "nudged": i < trouble, "test_fail": 0, "denied": 0, "max_ctx": 1000}
                f.write(json.dumps(rec) + "\n")

    def test_no_change_on_thin_evidence(self):
        self.hook("SessionStart")
        self.seed(5, 5)
        self.assertIn("no change", self.cms("tune"))
        self.assertEqual(self.policy()["version"], 1)

    def test_upgrade_verify_rollback_and_persist(self):
        self.hook("SessionStart")
        self.seed(15, 7)  # 47% of scout runs needed another agent
        out = self.cms("tune")
        self.assertIn("scout haiku -> sonnet", out)
        p = self.policy()
        self.assertEqual((p["version"], p["agent_models"]["scout"]), (2, "sonnet"))
        self.assertIn("model: sonnet", open(os.path.join(self.env["CMS_AGENTS_DIR"], "scout.md")).read())
        # a new session sees the new policy version: the lesson persisted and is applied
        self.assertIn("policy v2", self.hook("SessionStart", session_id="S2")["additionalContext"])
        # the upgrade did not help: still ~50% bad on 10 new runs -> roll back and avoid for 60 days
        self.seed(10, 5, ts=int(time.time()) + 5)
        out = self.cms("tune")
        self.assertIn("rolled back scout sonnet -> haiku", out)
        p = self.policy()
        self.assertEqual(p["agent_models"]["scout"], "haiku")
        self.assertTrue(any(a["item"] == "scout:sonnet" for a in p["avoid"]))
        out = self.cms("tune")  # avoid list prevents flip-flopping
        self.assertNotIn("haiku -> sonnet", out)
        self.assertIn("restored", self.cms("rollback"))
        self.assertEqual(self.policy()["agent_models"]["scout"], "sonnet")  # manual rollback restores prior version

    def test_project_hint_reaches_session_start(self):
        self.hook("SessionStart")
        self.seed(12, 0, cwd="/hard", trouble=6)
        self.cms("tune")
        self.assertIn("picks Opus", self.hook("SessionStart", session_id="S3", cwd="/hard")["additionalContext"])
        self.assertNotIn("This project", self.hook("SessionStart", session_id="S4", cwd="/easy")["additionalContext"])

    def test_only_verified_lessons_are_injected(self):
        self.hook("SessionStart")
        self.cms("lesson", "add", "guess about haiku")
        self.cms("lesson", "add", "--verified", "gradle builds need the expert tier")
        ctx = self.hook("SessionStart", session_id="S5")["additionalContext"]
        self.assertIn("gradle builds need the expert tier", ctx)
        self.assertNotIn("guess about haiku", ctx)
        self.cms("lesson", "verify", "1")
        self.assertIn("guess about haiku", self.hook("SessionStart", session_id="S6")["additionalContext"])

    def test_report_runs(self):
        self.hook("SessionStart")
        self.seed(3, 1)
        self.assertIn("SINCE INSTALL", self.cms("report"))


class InstallTest(unittest.TestCase):
    def run_install(self, claude, *args):
        env = dict(os.environ, CMS_CLAUDE_DIR=claude, HOME=os.path.dirname(claude))
        env.pop("CMS_HOME", None)
        env.pop("CMS_AGENTS_DIR", None)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "install.py")] + list(args), capture_output=True,
                           text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_install_idempotent_and_uninstall_restores(self):
        home = tempfile.mkdtemp()
        claude = os.path.join(home, ".claude")
        os.makedirs(os.path.join(claude, "agents"))
        original = {"theme": "dark", "model": "opus", "enabledPlugins": {"x@y": True},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
        json.dump(original, open(os.path.join(claude, "settings.json"), "w"))
        open(os.path.join(claude, "agents", "expert.md"), "w").write("my own expert\n")
        self.run_install(claude)
        self.run_install(claude)  # twice: no duplicate hooks
        s = json.load(open(os.path.join(claude, "settings.json")))
        self.assertEqual((s["model"], s["autoCompactWindow"], s["theme"]), ("sonnet", 200000, "dark"))
        self.assertEqual(len(s["hooks"]["Stop"]), 2)  # user's + ours
        self.assertEqual(len(s["hooks"]["SessionStart"]), 1)
        self.assertEqual(open(os.path.join(claude, "agents", "expert.md")).read(), "my own expert\n")
        self.assertTrue(os.path.exists(os.path.join(claude, "agents", "scout.md")))
        self.run_install(claude, "--uninstall")
        self.assertEqual(json.load(open(os.path.join(claude, "settings.json"))), original)
        self.assertFalse(os.path.exists(os.path.join(claude, "agents", "scout.md")))
        self.assertEqual(open(os.path.join(claude, "agents", "expert.md")).read(), "my own expert\n")


if __name__ == "__main__":
    unittest.main()
