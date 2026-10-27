# claude-model-switch

A lightweight routing, escalation and learning layer for Claude Code. It uses only mechanisms Claude Code
(2.1.304) supports: settings keys, user agents with a `model:` field, and command hooks. It needs Python 3
and nothing else.

## Why this design

Measured from 14 days of local transcripts before install (5,158 prompts):

| Indicator | Value |
|---|---|
| Calls on Opus | 94–97% |
| Median context re-read per call | 333–366k tokens |
| Calls with more than 200k context | 77% (92% of all context tokens) |
| Cache-read tokens vs output tokens | ~17B vs ~45M |
| Fresh-session baseline (CLI) | ~48k tokens, of which user plugins add only ~5k |

Re-read context, not model choice, dominated cost. So the system attacks four things, biggest first:

1. **Context cap**: `autoCompactWindow: 200000`. Sessions compact near 200k instead of growing towards 1M.
2. **Cheaper main model**: `model: sonnet` by default. Opus is used through the `expert` agent, or when the user picks it.
3. **Cheap delegation**: a Haiku `scout` agent handles reconnaissance with a ~8k context instead of the main ~50k+.
4. **Learning loop**: the hooks log outcomes, and `cms tune` adjusts routing only on evidence.

Trimming plugins was measured and rejected as a main lever. Disabling sciagent-skills saved ~0 tokens,
superpowers ~1.3k and caveman ~2.5k, out of ~48k.

## Components

| Path | Role |
|---|---|
| `install.py` | Installs into `~/.claude` (idempotent). Backs up `settings.json` first, merges keys and leaves other hooks alone. `--uninstall` restores. |
| `app/cms_hook.py` | One hook script for 7 events. It never blocks the session: errors go to `errors.log` and the hook exits 0. |
| `app/cms_lib.py` | State, transcript parsing, policy text, agent templates |
| `app/cms.py` | CLI: `status`, `report`, `tune`, `rollback`, `baseline`, `lesson` |
| `~/.claude/agents/scout.md` | Read-only recon agent, model from policy (default `haiku`) |
| `~/.claude/agents/expert.md` | Escalation agent, `opus` |
| `~/.claude/model-switch/` | `policy.json`, `policy-history/`, `log.jsonl`, `lessons.json`, `baseline.json`, `backup/` |

## How it works

**Routing.** At `SessionStart` the hook injects a policy block of about 250 tokens. It routes recon to
`scout`, hard reasoning to `expert`, and small lookups stay inline. The block also includes a per-project
hint and up to 5 *verified* lessons.

**Escalation.**
- *Subagent tier*: automatic. Sonnet escalates to the Opus `expert` agent with a full brief.
- *Failure detector*: `PostToolUseFailure` counts failed tool calls in the main thread. At the threshold
  (default 3) it injects one nudge: change approach once, or escalate to `expert`.
- *Main session*: cannot switch itself. The desktop app refuses self-switching, and hooks cannot change the
  running model. The policy tells Claude to ask the user to pick Opus. `PostModelSwitch` logs those
  switches as escalation evidence.
- *Guards*: the `PreToolUse` hook enforces 4 agents per turn, 1 expert per turn, and no delegation from
  inside a subagent.
- *Retry rule*: retry once, escalate one tier, then stop and ask the user.

**Learning.**
- `Stop` and `SubagentStop` parse the transcript incrementally and log metadata per turn and per agent:
  models, token counts, peak context, tool errors, failed test runs, agents used, nudges, cap hits. No
  prompt text or file content is stored.
- A short prompt that reads like a correction ("still fails", "that's wrong") marks the previous turn as
  `corrected`.
- `cms tune` runs automatically at session start every 7 days. It changes policy only past minimum
  sample sizes:
  - **scout tier**: haiku to sonnet if ≥35% of ≥15 scout runs needed another agent or a correction.
    Sonnet back to haiku if ≤10% of ≥20 runs did.
  - **verification**: after any tier change, the next ≥10 samples are compared with the before-rate. If
    the change did not help, it is rolled back and that option is blocked for 60 days (no flip-flopping).
  - **project hints**: "pick Opus here" when ≥40% of ≥10 Sonnet turns in that project needed escalation
    or correction. "Sonnet suffices" when ≥15 Opus turns were routine. Hints expire after 30 days.
  - **nudge threshold**: moves between 2 and 5 depending on whether nudged turns still got corrected.
  - **hypotheses**: written to `lessons.json` as `hypothesis` and never injected until verified.
- Every policy change archives the previous version. `cms rollback [N]` restores one.

## Install / uninstall

```bash
python3 install.py
```

```bash
python3 install.py --uninstall
```

`--keep-model` leaves your default model alone. `--uninstall --purge` also deletes the logs. Uninstall
restores your earlier `model` and `autoCompactWindow`, unless you changed them yourself after install.

## Measuring

```bash
python3 ~/.claude/model-switch/app/cms.py report --days 14
```

The report compares against `baseline.json` (14 days before install): call share per model family,
mean context per call, input-equivalent units per turn, and outcome rates (corrections, nudges, test
failures, cap hits, compactions).

Units weight cache reads at 0.1, cache writes at 1.25 and output at 5. Those ratios hold *within* a model
family; compare families by call share, not by units. Exact claude.ai usage-limit weighting is not public,
so savings against the limit are estimates.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

15 tests cover transcript parsing (dedupe, partial lines, resume offsets), caps, the nudge, logging,
correction marking, context warnings, tune / verify / rollback / no-flip-flop, project hints reaching a new
session, verified-only lesson injection, and an install / uninstall round trip.

Live checks with real Claude Code passed:
- The policy was injected.
- `scout` ran on `claude-haiku-5-5` (about 7.8k context against 59k for the main thread).
- The cap hook denied the 2nd agent with cap=1.
- The failure nudge fired at 3 failures.
- `model: sonnet` selected `claude-sonnet-5-5`.

## Limitations

- The active session's model cannot switch itself. Opus for a whole task is the user's pick in the model
  menu or `/model opus`. The desktop app may keep its own per-session model choice instead of `settings.json`.
- Outcome signals are proxies (corrections, failures, extra agents), not ground truth.
- Learning is operational: policy and routing change, the models are not retrained.
- A 200k cap means more compactions in long sessions. Each one costs a summary call, and detail from early
  in the session can be lost.
- A Sonnet default trades some first-try quality for cost. The escalation path and project hints exist to
  catch that.
