<div align="center">

# ctxdrift

### Your agent read 40 files. Then the context window filled up. What survived?

**ctxdrift audits the moment every AI coding agent is afraid to talk about:
compaction.** It parses your session transcripts, finds every context-window
summary injection, and tells you exactly what your agent forgot — files erased,
decisions lost, work re-done, secrets dropped from the record.

[![CI](https://github.com/ZidoCode/ctxdrift/actions/workflows/ci.yml/badge.svg)](https://github.com/ZidoCode/ctxdrift/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ctxdrift?cacheSeconds=300)](https://pypi.org/project/ctxdrift/)
[![Python](https://img.shields.io/pypi/pyversions/ctxdrift?cacheSeconds=300)](https://pypi.org/project/ctxdrift/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](#why)

`pip install ctxdrift`

</div>

---

## The problem nobody measures

Every AI coding agent — Claude Code, Codex, OpenCode, your own agent loop —
eventually hits the context window limit. When it does, the runtime silently
**summarizes everything that came before and throws the original away.**

Nobody audits that moment. Until now:

- Your agent read `/payments/refund.py` 4 times before compaction. Does the
  summary mention it? **ctxdrift checks.**
- Your agent declared *"Decision: we will route refunds through the gateway
  adapter"* — then the summary crushed it to *"made some edits."* **ctxdrift catches
  the decision loss.**
- Your agent re-read the same file after compaction that it already read before.
  That's a forgetting signature. **ctxdrift counts it.**
- One 120K-token tool result right before compaction — the probable trigger.
  **ctxdrift points at it.**

This is not a token-counting dashboard. Those tell you what you **spent**.
ctxdrift tells you what you **lost**.

## Quick start

```bash
pip install ctxdrift

# audit one transcript
ctxdrift ~/.claude/projects/myproject/abc123.jsonl

# audit every session, worst first
ctxdrift ~/.claude/projects/

# machine-readable, gate your CI on it
ctxdrift ./transcripts --format json --min-severity warn
```

## What a run looks like

```
ctxdrift v0.1.0
==========================================================
file        : /tmp/demo/session.jsonl
events      : 12
compactions : 1  (at line 10)

findings
----------------------------------------------------------
✗ CD04  [fail]  1 file(s) read before compaction #1 are absent from the
                injected summary text itself.
        - /app/payments/refund.py
! CD03  [warn]  1 file(s) re-read after compaction #1 although they were
                already read before it — the agent forgot it had them.
        - /app/payments/checkout.py (pre x1, post x1)
! CD05  [warn]  1 assistant decision/plan statement(s) before compaction #1
                never reappear afterwards.
        - Decision: we will route refunds through the gateway adapter...
```

## The rules

| Rule | Severity | What it catches |
|------|----------|-----------------|
| **CD01** | info | Compaction count and boundary lines |
| **CD02** | warn | Files touched pre-compaction, never seen again post |
| **CD03** | warn | Re-read loop across a boundary — the classic forgetting signature |
| **CD04** | fail | File read pre-compaction, **absent from the summary text itself** |
| **CD05** | warn | Stated decisions/plans that vanish after compaction |
| **CD06** | info | Shell commands erased from the record |
| **CD07** | info | Oversized tool results right before compaction (probable trigger) |
| **CD08** | fail | Rapid re-compaction — summary losing ground |
| **CD09** | info | Env vars / config keys that vanish from the record |
| **CD10** | info | Session never compacted (clean) |

Exit codes are CI-gate ready: `0` clean · `1` warnings · `2` failures.

## Works with

- **Claude Code** — `~/.claude/projects/**/*.jsonl` ✅
- **Codex CLI** — `~/.codex/sessions/**/*.jsonl` ✅
- **Any agent** that writes JSONL with user/assistant/tool messages — generic mode ✅
- Compaction markers auto-detected across runtimes, including
  `[CONTEXT COMPACTION]`, `This session is being continued from a previous
  conversation`, and `<summary>` blocks.

## Why ctxdrift

- **Zero dependencies.** One Python file, stdlib only. No telemetry, no API
  keys, no cloud, no model calls. Everything stays on your machine.
- **Offline and deterministic.** Same transcript in, same report out. Twice.
- **Fast and robust.** Fuzz-tested against 50 adversarial transcripts —
  binary junk, truncated JSON, 200K-char blobs, unicode garbage — zero crashes.
  ReDoS-hardened regexes.
- **Honest.** A finding cites its evidence. If the summary mentions the file,
  ctxdrift doesn't flag it. Exit codes map to real severities, not marketing.

## Use cases

- **Before you resume a compacted session** — check what the summary dropped
  before you trust "continue from where we left off."
- **Post-mortems** — "the agent forgot the constraint we set two hours ago"
  now has a receipt, with line numbers.
- **Agent engineering** — measure whether your system prompt, memory files, or
  summary prompts actually survive compaction. Iterate with evidence.
- **CI gate** — run it over recorded agent sessions in your pipeline; fail the
  build when work gets silently erased.

## Python API

```python
import ctxdrift as cd

events = cd.parse_jsonl("~/.claude/projects/app/session.jsonl")
compactions = cd.find_compactions(events)
findings = cd.analyze(events, compactions)

for f in findings:
    print(f.rule, f.severity, f.message)
```

## Part of the ZidoCode agent-audit suite

ctxdrift is one of a family of zero-dependency agent-audit tools:

- **[a2a-doctor](https://github.com/ZidoCode/a2a-doctor)** — health checks for A2A agent cards
- **[mcp-doctor](https://github.com/ZidoCode/mcp-doctor)** — MCP server diagnostics (PyPI: `mcpmedic`)
- **[mcp-session-audit](https://github.com/ZidoCode/mcp-session-audit)** — MCP session lifecycle auditing
- **[tokencache](https://github.com/ZidoCode/tokencache)** — local caching proxy for LLM APIs

## License

MIT — see [LICENSE](LICENSE).

<div align="center">

**If ctxdrift caught something your summary dropped, that's a star.**
⭐ It means the audit worked.

</div>
