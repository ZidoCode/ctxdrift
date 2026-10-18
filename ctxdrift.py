#!/usr/bin/env python3
"""ctxdrift — what did your agent forget?

Parses AI coding-agent session transcripts (Claude Code, Codex CLI, generic
JSONL), locates context-compaction events, and reports what was lost when the
context window was summarized: files read but never mentioned again, tool
results silently dropped, decisions erased, and re-read loops caused by
forgetting. Zero dependencies. Offline. Deterministic.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

EXIT_CLEAN = 0
EXIT_WARN = 1
EXIT_FAIL = 2

# ---------------------------------------------------------------------------
# Compaction boundary markers — the summary headers various runtimes inject.
COMPACTION_MARKERS = [
    "[CONTEXT COMPACTION",
    "CONTEXT COMPACTION — REFERENCE ONLY",
    "This session is being continued from a previous conversation",
    "conversation history was compacted",
    "conversation history before this point was compacted",
    "Earlier turns were compacted into the summary below",
    "<summary>",
    "[COMPACTION]",
    "# Compact Summary",
]

# Tool names that carry file payload, per agent family.
READ_TOOLS = {"Read", "read_file", "read", "View", "view"}
WRITE_TOOLS = {"Write", "Edit", "write_file", "edit", "patch", "MultiEdit", "str_replace_editor", "create", "write"}
BASH_TOOLS = {"Bash", "bash", "terminal", "execute_command", "run_terminal_cmd"}

# Bounded path regex: no nested quantifiers, single pass. Word-run segments
# separated by / or . are matched linearly — immune to backtracking blowups
# on long non-path strings.
PATH_RE = re.compile(
    r"(?:[A-Za-z0-9_.\-]{1,64}/){0,8}[A-Za-z0-9_\-]{1,64}"
    r"\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|md|json|ya?ml|toml|sh|txt|c|cpp|h|css|html)\b"
)
ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")

# Claude Code / Codex transcript boundaries (JSONL line shapes).
CLAUDE_PATH_GLOB = os.path.join(".claude", "projects")
CODEX_PATH_GLOB = os.path.join(".codex", "sessions")


@dataclass
class Event:
    """One normalized transcript event."""
    kind: str            # "user" | "assistant" | "tool_call" | "tool_result" | "other"
    text: str
    tool: str = ""
    ts: str = ""
    line_no: int = 0
    meta: dict = field(default_factory=dict)


@dataclass
class Compaction:
    """One compaction boundary found in the transcript."""
    line_no: int
    ts: str
    marker: str
    events_before: int = 0
    events_after: int = 0


@dataclass
class Finding:
    """A single drift / loss finding."""
    rule: str
    severity: str        # "info" | "warn" | "fail"
    message: str
    evidence: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity,
                "message": self.message, "evidence": self.evidence[:20]}


RULES = {
    "CD01": ("compaction-count", "info", "How many compactions happened in this session"),
    "CD02": ("files-dropped", "warn", "Files the agent read/edit before a compaction but never touched again — the summary likely lost them"),
    "CD03": ("re-read-loop", "warn", "Same file re-read multiple times across a compaction boundary — classic forgetting signature"),
    "CD04": ("summary-missing-file", "fail", "File was read before compaction and is absent from the injected summary text"),
    "CD05": ("decision-loss", "warn", "Assistant stated a decision/plan before compaction that never reappears after"),
    "CD06": ("command-loss", "info", "Bash commands executed before compaction that never appear again"),
    "CD07": ("tool-result-dump", "info", "Oversized tool results right before compaction (likely the reason it triggered)"),
    "CD08": ("rapid-recompaction", "fail", "Compactions happening suspiciously close together — summary may be losing ground"),
    "CD09": ("env-var-loss", "info", "Environment variables / keys referenced pre-compaction that vanish after"),
    "CD10": ("uncompacted-tail", "info", "Session never compacted — nothing to audit here"),
}


# ---------------------------------------------------------------------------
# Transcript parsing

def _extract_text(content) -> str:
    """Content may be str, list of blocks, or dict — normalize to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "text" in content:
            return _extract_text(content["text"])
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input", {}), ensure_ascii=False))
                elif block.get("type") == "tool_result":
                    c = block.get("content")
                    parts.append(_extract_text(c) if c else "")
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _classify(obj: dict) -> Event | None:
    """Map one JSONL record (any agent family) to a normalized Event."""
    if not isinstance(obj, dict):
        return None
    ts = str(obj.get("timestamp") or obj.get("ts") or obj.get("created_at") or "")

    # Claude Code: {"type":"user"/"assistant","message":{"role","content"}}
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = msg.get("role") or obj.get("type") or obj.get("kind") or ""
    content = msg.get("content")

    if obj.get("type") == "tool_result" or (isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
        text = _extract_text(content)
        if text:
            return Event("tool_result", text, ts=ts)
        return None

    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool = str(block.get("name", ""))
                inp = block.get("input", {}) or {}
                argtxt = _extract_text(inp)
                return Event("tool_call", argtxt, tool=tool, ts=ts, meta={"input": inp})

    text = _extract_text(content)
    if not text and not tool_callish(obj):
        return None
    if role in ("user", "human"):
        return Event("user", text, ts=ts)
    if role in ("assistant", "ai", "model"):
        return Event("assistant", text, ts=ts)
    return Event("other", text, tool=str(obj.get("tool", "")), ts=ts)


def tool_callish(obj: dict) -> bool:
    return "tool" in obj or "toolName" in obj or "tool_name" in obj


def parse_jsonl(path: str) -> list[Event]:
    """Parse a transcript file into normalized events."""
    events: list[Event] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Codex older rollouts and some runtimes emit non-JSON lines.
                if line.startswith("{") is False:
                    continue
                # salvage: skip garbage silently, tolerate junk
                continue
            ev = _classify(obj)
            if ev is not None:
                ev.line_no = line_no
                events.append(ev)
    return events


def detect_format(path: str, events: list[Event]) -> str:
    """Best-effort transcript family detection."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        head = fh.read(4096)
    if '"sessionId"' in head or '"parentUuid"' in head or CLAUDE_PATH_GLOB in path:
        return "claude"
    if CODEX_PATH_GLOB in path or '"session_id"' in head and '"payload"' in head:
        return "codex"
    return "generic"


# ---------------------------------------------------------------------------
# Compaction detection

def _coerce_events(events) -> list[Event]:
    """Accept raw JSONL dicts mixed with Events (convenience for API users)."""
    out = []
    for i, item in enumerate(events, 1):
        if isinstance(item, Event):
            ev = item
        elif isinstance(item, dict):
            ev = _classify(item)
            if ev is None:
                continue
        else:
            continue
        if not ev.line_no:
            ev.line_no = i
        out.append(ev)
    return out


def find_compactions(events: list[Event]) -> list[Compaction]:
    """Locate compaction boundaries by summary-injection markers."""
    found = []
    for ev in _coerce_events(events):
        text = ev.text or ""
        for marker in COMPACTION_MARKERS:
            if marker.lower() in text.lower():
                found.append(Compaction(line_no=ev.line_no, ts=ev.ts, marker=marker))
                break
    return found


# ---------------------------------------------------------------------------
# Extraction helpers

def files_in(text: str) -> list[str]:
    return sorted(set(PATH_RE.findall(text or "")))


def envvars_in(text: str) -> list[str]:
    return sorted(set(ENV_VAR_RE.findall(text or "")))[:40]


def is_compaction_text(text: str) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in COMPACTION_MARKERS)


# ---------------------------------------------------------------------------
# Loss analysis

def analyze(events: list[Event], compactions: list[Compaction]) -> list[Finding]:
    findings: list[Finding] = []
    events = _coerce_events(events)
    if not compactions:
        findings.append(Finding("CD10", "info",
                                "Session never compacted — nothing to audit here."))
        return findings

    findings.append(Finding("CD01", "info",
                            f"{len(compactions)} compaction(s) found in this session."))

    # CD08 — rapid re-compaction: < 20 events between two compactions.
    boundaries = [c.line_no for c in compactions]
    for i in range(1, len(boundaries)):
        gap_events = sum(1 for e in events if boundaries[i - 1] < e.line_no <= boundaries[i])
        if gap_events < 20:
            findings.append(Finding(
                "CD08", "fail",
                f"Only {gap_events} events between compaction #{i} and #{i+1} — the "
                "summary may be losing ground (context inflating faster than work).",
                evidence=[f"line {boundaries[i-1]}", f"line {boundaries[i]}"]))

    for i, comp in enumerate(compactions):
        after_start = comp.line_no
        before_end = compactions[i + 1].line_no if i + 1 < len(compactions) else 10**12

        pre = [e for e in events if e.line_no < after_start]
        post = [e for e in events if after_start < e.line_no < before_end]
        comp_event = next((e for e in events if e.line_no == after_start), None)
        summary_text = comp_event.text if comp_event else ""
        # Some runtimes split the summary across user+assistant turns.
        summary_text += "\n" + "\n".join(e.text for e in post if is_compaction_text(e.text))

        # --- CD02 / CD04: files pre-compaction, never mentioned post. -------
        pre_files: Counter = Counter()
        for ev in pre:
            if ev.kind in ("tool_call",) and ev.tool in READ_TOOLS | WRITE_TOOLS:
                inp = ev.meta.get("input", {}) or {}
                fp = inp.get("file_path") or inp.get("path") or inp.get("filePath") or ""
                if fp:
                    pre_files[str(fp)] += 1
            else:
                for f in files_in(ev.text):
                    pre_files[f] += 1

        post_blob = "\n".join(e.text for e in post).lower()
        summary_low = summary_text.lower()
        dropped, absent = [], []
        for fp, cnt in sorted(pre_files.items(), key=lambda kv: -kv[1]):
            fname = Path(fp).name.lower()
            if fname and fname in post_blob:
                continue
            dropped.append(f"{fp} (touched {cnt}x pre-compaction)")
            if fname and fname not in summary_low:
                absent.append(fp)

        if dropped:
            sev = "warn"
            findings.append(Finding(
                "CD02", sev,
                f"{len(dropped)} file(s) touched before compaction #{i+1} never appear "
                "again afterwards — work likely erased by the summary.",
                evidence=dropped[:15]))
        if absent:
            findings.append(Finding(
                "CD04", "fail",
                f"{len(absent)} file(s) read before compaction #{i+1} are absent from "
                "the injected summary text itself.",
                evidence=absent[:15]))

        # --- CD03: re-read loop across the boundary. ------------------------
        reads_pre, reads_post = Counter(), Counter()
        for ev in pre:
            if ev.kind == "tool_call" and ev.tool in READ_TOOLS:
                inp = ev.meta.get("input", {}) or {}
                fp = inp.get("file_path") or inp.get("path") or ""
                if fp:
                    reads_pre[fp] += 1
        for ev in post:
            if ev.kind == "tool_call" and ev.tool in READ_TOOLS:
                inp = ev.meta.get("input", {}) or {}
                fp = inp.get("file_path") or inp.get("path") or ""
                if fp:
                    reads_post[fp] += 1
        reread = [f for f in reads_pre if f in reads_post]
        if reread:
            findings.append(Finding(
                "CD03", "warn",
                f"{len(reread)} file(s) re-read after compaction #{i+1} although they "
                "were already read before it — the agent forgot it had them.",
                evidence=[f"{f} (pre x{reads_pre[f]}, post x{reads_post[f]})" for f in reread][:15]))

        # --- CD05: decisions stated pre, never echoed post. -----------------
        pre_decisions = []
        for ev in pre:
            if ev.kind == "assistant" and len(ev.text) > 80:
                for kw in ("decision", "plan:", "we will", "approach:", "let's", "i'll"):
                    if kw in ev.text.lower():
                        pre_decisions.append(ev.text.strip()[:140])
                        break
        decision_lost = [d for d in pre_decisions if d[:60].lower() not in post_blob]
        if decision_lost:
            findings.append(Finding(
                "CD05", "warn",
                f"{len(decision_lost)} assistant decision/plan statement(s) before "
                f"compaction #{i+1} never reappear afterwards.",
                evidence=decision_lost[:10]))

        # --- CD06: bash commands. -------------------------------------------
        pre_cmds = [e.text.strip()[:100] for e in pre
                    if e.kind == "tool_call" and e.tool in BASH_TOOLS and e.text.strip()]
        cmd_lost = [c for c in pre_cmds if c[:50].lower() not in post_blob]
        if cmd_lost:
            findings.append(Finding(
                "CD06", "info",
                f"{len(cmd_lost)} shell command(s) run before compaction #{i+1} never "
                "appear again.",
                evidence=cmd_lost[:10]))

        # --- CD09: env vars. -------------------------------------------------
        pre_env = set()
        for ev in pre:
            pre_env |= set(envvars_in(ev.text))
        post_env = set()
        for ev in post:
            post_env |= set(envvars_in(ev.text))
        if summary_text:
            post_env |= set(envvars_in(summary_text))
        # only track plausible secrets/config vars, not ALL-CAPS prose
        noise = {"TODO", "FIXME", "NOTE", "WARNING", "ERROR", "INFO", "JSON", "URL", "API", "SDK", "OK"}
        env_lost = sorted(v for v in pre_env - post_env
                          if v not in noise and ("KEY" in v or "TOKEN" in v or "SECRET" in v or v.endswith("_URL") or v.startswith("ZIDO") or v.startswith("NINE")))
        if env_lost:
            findings.append(Finding(
                "CD09", "info",
                f"{len(env_lost)} env/config var(s) referenced before compaction "
                f"#{i+1} vanish afterwards.",
                evidence=env_lost[:15]))

    # --- CD07: oversized tool results right before compaction. -------------
    for comp in compactions:
        near = [e for e in events if 0 < comp.line_no - e.line_no <= 5 and e.kind == "tool_result"]
        for ev in near:
            if len(ev.text) > 50000:
                findings.append(Finding(
                    "CD07", "info",
                    f"Oversized tool result ({len(ev.text):,} chars) at line "
                    f"{ev.line_no}, right before compaction — probable trigger.",
                    evidence=[ev.text[:80] + "..."]))

    return findings


# ---------------------------------------------------------------------------
# Report

SEV_ORDER = {"fail": 0, "warn": 1, "info": 2}
SEV_ICON = {"fail": "✗", "warn": "!", "info": "·"}


def report(events, compactions, findings, fmt="text", path=""):
    n_events = len(events)
    if fmt == "json":
        return json.dumps({
            "tool": "ctxdrift", "version": __version__, "file": path,
            "events": n_events, "compactions": len(compactions),
            "boundaries": [c.line_no for c in compactions],
            "findings": [f.as_dict() for f in findings],
        }, ensure_ascii=False, indent=2)

    lines = []
    lines.append("ctxdrift v" + __version__)
    lines.append("=" * 58)
    lines.append(f"file        : {path}")
    lines.append(f"events      : {n_events}")
    lines.append(f"compactions : {len(compactions)}" +
                 (f"  (at lines {', '.join(str(c.line_no) for c in compactions)})" if compactions else ""))
    lines.append("")
    if not compactions:
        lines.append("· CD10  Session never compacted — nothing to audit here.")
        lines.append("")
        lines.append("=" * 58)
        return "\n".join(lines)

    counts = Counter(f.severity for f in findings)
    lines.append("findings")
    lines.append("-" * 58)
    for f in sorted(findings, key=lambda x: (SEV_ORDER[x.severity], x.rule)):
        lines.append(f"{SEV_ICON[f.severity]} {f.rule}  [{f.severity:<4}]  {f.message}")
        for ev in f.evidence[:5]:
            lines.append(f"        - {ev}")
    lines.append("")
    lines.append("-" * 58)
    lines.append(f"summary: {counts['fail']} fail / {counts['warn']} warn / "
                 f"{counts['info']} info across {len(compactions)} compaction(s)")
    lines.append("=" * 58)
    return "\n".join(lines)


def exit_code(findings) -> int:
    sev = {f.severity for f in findings}
    if "fail" in sev:
        return EXIT_FAIL
    if "warn" in sev:
        return EXIT_WARN
    return EXIT_CLEAN


# ---------------------------------------------------------------------------
# Auto-discovery

def discover_transcripts(root: str | None) -> list[str]:
    """Find agent transcript files in the default locations."""
    home = Path.home()
    roots = []
    if root:
        roots.append(Path(root))
    else:
        roots.append(home / ".claude" / "projects")
        roots.append(home / ".codex" / "sessions")
    out = []
    for r in roots:
        if r.is_dir():
            out.extend(str(p) for p in sorted(r.rglob("*.jsonl")))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ctxdrift",
        description="What did your agent forget? Audits context-compaction loss "
                    "in AI coding-agent session transcripts. Zero deps, offline.")
    ap.add_argument("target", nargs="?",
                    help="transcript file (.jsonl) or directory to scan")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--rules", help="only run these comma-separated rules (e.g. CD02,CD04)")
    ap.add_argument("--min-severity", choices=["info", "warn", "fail"], default="info")
    ap.add_argument("--version", action="version", version=f"ctxdrift {__version__}")
    args = ap.parse_args(argv)

    target = args.target
    paths: list[str]
    if not target:
        paths = discover_transcripts(None)
        if not paths:
            print("ctxdrift: no transcripts found in ~/.claude/projects or "
                  "~/.codex/sessions — pass a file or directory.", file=sys.stderr)
            return EXIT_CLEAN
    elif os.path.isdir(target):
        paths = [str(p) for p in sorted(Path(target).rglob("*.jsonl"))]
        if not paths:
            print(f"ctxdrift: no .jsonl transcripts under {target}", file=sys.stderr)
            return EXIT_CLEAN
    else:
        paths = [target]

    rules_filter = set(args.rules.split(",")) if args.rules else None
    min_rank = SEV_ORDER[args.min_severity]  # fail=0 warn=1 info=2; keep rank <= min_rank

    agg_findings: list[Finding] = []
    json_docs: list = []
    multi = len(paths) > 1
    code = EXIT_CLEAN
    for p in paths:
        try:
            events = parse_jsonl(p)
        except OSError as exc:
            print(f"ctxdrift: cannot read {p}: {exc}", file=sys.stderr)
            code = max(code, EXIT_WARN)
            continue
        compactions = find_compactions(events)
        findings = analyze(events, compactions)
        if rules_filter:
            findings = [f for f in findings if f.rule in rules_filter]
        findings = [f for f in findings if SEV_ORDER[f.severity] <= min_rank]
        agg_findings.extend(findings)
        if args.format == "json":
            json_docs.append(json.loads(report(events, compactions, findings, "json", p)))
        else:
            if multi:
                print(f"\n### {p}")
            print(report(events, compactions, findings, "text", p))
        code = max(code, exit_code(findings))

    if args.format == "json":
        payload = json_docs if multi else (json_docs[0] if json_docs else {})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
