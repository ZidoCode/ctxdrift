"""Tests for ctxdrift — compaction loss auditor."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import ctxdrift as cd


def write_jsonl(events, tmpdir, name="session.jsonl"):
    """events: list of dicts; writes JSONL and returns path."""
    p = os.path.join(tmpdir, name)
    with open(p, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return p


def user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def assistant(content):
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def tool_use(name, inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": name, "input": inp}]}}


def tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": text}]}}


def compaction(summary_body="Summary of earlier conversation."):
    return user("[CONTEXT COMPACTION — REFERENCE ONLY] " + summary_body)


def to_events(raw_items):
    """Normalize raw dict/Event mixes the way parse_jsonl does."""
    out = []
    for i, item in enumerate(raw_items, 1):
        if isinstance(item, cd.Event):
            ev = item
        else:
            ev = cd._classify(item)
            if ev is None:
                continue
        ev.line_no = ev.line_no or i
        out.append(ev)
    return out


class TestParsing(unittest.TestCase):
    def test_parse_basic(self):
        with tempfile.TemporaryDirectory() as td:
            p = write_jsonl([user("hello"), assistant("hi"), tool_use("Read", {"file_path": "/a/b.py"}), tool_result("contents")], td)
            evs = cd.parse_jsonl(p)
            kinds = [e.kind for e in evs]
            self.assertEqual(kinds, ["user", "assistant", "tool_call", "tool_result"])

    def test_junk_lines_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s.jsonl")
            with open(p, "w") as fh:
                fh.write('{"type":"user","message":{"role":"user","content":"ok"}}\n')
                fh.write("NOT JSON AT ALL <<<garbage\n")
                fh.write('{"broken json\n')
                fh.write('{"type":"user","message":{"role":"user","content":"after"}}\n')
            evs = cd.parse_jsonl(p)
            self.assertEqual(len(evs), 2)

    def test_extract_text_variants(self):
        self.assertEqual(cd._extract_text("plain"), "plain")
        self.assertEqual(cd._extract_text([{"type": "text", "text": "a"}]), "a")
        self.assertEqual(cd._extract_text(None), "")
        self.assertEqual(cd._extract_text([{"type": "tool_use", "name": "X", "input": {"a": 1}}]), '{"a": 1}')


class TestCompactionDetection(unittest.TestCase):
    def test_marker_found(self):
        evs = to_events([cd.Event("user", "[CONTEXT COMPACTION — REFERENCE ONLY] summary here", line_no=3)])
        cs = cd.find_compactions(evs)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].line_no, 3)

    def test_no_marker(self):
        evs = to_events([cd.Event("user", "hello world", line_no=1)])
        self.assertEqual(cd.find_compactions(evs), [])

    def test_marker_variants(self):
        for m in ("This session is being continued from a previous conversation",
                  "<summary>", "# Compact Summary"):
            evs = to_events([cd.Event("user", "x " + m + " y", line_no=1)])
            self.assertEqual(len(cd.find_compactions(evs)), 1)


class TestRules(unittest.TestCase):
    def test_cd10_no_compaction(self):
        f = cd.analyze(to_events([cd.Event("user", "hi")]), [])
        self.assertEqual([x.rule for x in f], ["CD10"])
        self.assertEqual(f[0].severity, "info")

    def test_cd01_count(self):
        f = cd.analyze(to_events([cd.Event("user", "[CONTEXT COMPACTION] s", line_no=1)]), [cd.Compaction(1, "", "")])
        self.assertTrue(any(x.rule == "CD01" and x.severity == "info" for x in f))

    def test_cd04_file_absent_from_summary(self):
        evs = [
            tool_use("Read", {"file_path": "/proj/secret_impl.py"}),
            tool_result("def f(): ..."),
            compaction("The agent worked on tests only."),
        ]
        cs = cd.find_compactions(evs)
        f = cd.analyze(to_events(evs), cs)
        cd04 = [x for x in f if x.rule == "CD04"]
        self.assertEqual(len(cd04), 1)
        self.assertEqual(cd04[0].severity, "fail")
        self.assertIn("secret_impl.py", cd04[0].evidence[0])

    def test_cd04_file_present_in_summary_no_finding(self):
        evs = [
            tool_use("Read", {"file_path": "/proj/secret_impl.py"}),
            compaction("The agent read secret_impl.py and refactored it."),
        ]
        f = cd.analyze(to_events(evs), cd.find_compactions(evs))
        self.assertFalse(any(x.rule == "CD04" for x in f))

    def test_cd03_re_read_loop(self):
        evs = [
            tool_use("Read", {"file_path": "/x/main.py"}),
            tool_result("code"),
            compaction("Summary."),
            tool_use("Read", {"file_path": "/x/main.py"}),
            tool_result("code again"),
        ]
        f = cd.analyze(to_events(evs), cd.find_compactions(evs))
        cd03 = [x for x in f if x.rule == "CD03"]
        self.assertEqual(len(cd03), 1)
        self.assertIn("main.py", cd03[0].evidence[0])

    def test_cd08_rapid_recompaction(self):
        evs = [compaction("A"), user("one"), compaction("B")]
        evs[1] = {"type": "user", "message": {"role": "user", "content": "one"}}
        cs = cd.find_compactions(evs)
        f = cd.analyze(to_events(evs), cs)
        cd08 = [x for x in f if x.rule == "CD08"]
        self.assertEqual(len(cd08), 1)
        self.assertEqual(cd08[0].severity, "fail")

    def test_cd02_dropped_files(self):
        evs = [
            tool_use("Edit", {"file_path": "/w/feature.py", "old_string": "a", "new_string": "b"}),
            compaction("Summary without the file."),
        ]
        f = cd.analyze(to_events(evs), cd.find_compactions(evs))
        self.assertTrue(any(x.rule == "CD02" for x in f))

    def test_cd05_decision_loss(self):
        evs = [
            assistant("Decision: we will use SQLite for the cache layer and ship on Friday with the whole team reviewing the plan carefully"),
            compaction("Summary of some work."),
        ]
        f = cd.analyze(to_events(evs), cd.find_compactions(evs))
        self.assertTrue(any(x.rule == "CD05" for x in f))

    def test_cd07_oversized_tool_result(self):
        evs = [
            tool_result("x" * 60000),
            compaction("Summary."),
        ]
        f = cd.analyze(to_events(evs), cd.find_compactions(evs))
        self.assertTrue(any(x.rule == "CD07" for x in f))

    def test_cd09_env_var_loss(self):
        evs = [
            user("export NINE_ROUTER_API_KEY=abc and use ZIDO_DIR please"),
            compaction("Summary."),
        ]
        f = cd.analyze(to_events(evs), cd.find_compactions(evs))
        cd09 = [x for x in f if x.rule == "CD09"]
        self.assertEqual(len(cd09), 1)
        self.assertTrue(any("NINE_ROUTER_API_KEY" in e for e in cd09[0].evidence))

    def test_exit_codes(self):
        self.assertEqual(cd.exit_code([cd.Finding("CD01", "info", "m")]), cd.EXIT_CLEAN)
        self.assertEqual(cd.exit_code([cd.Finding("CD02", "warn", "m")]), cd.EXIT_WARN)
        self.assertEqual(cd.exit_code([cd.Finding("CD04", "fail", "m")]), cd.EXIT_FAIL)


class TestCLI(unittest.TestCase):
    def run_cli(self, *argv):
        return subprocess.run([sys.executable, cd.__file__, *argv],
                              capture_output=True, text=True)

    def test_text_report(self):
        with tempfile.TemporaryDirectory() as td:
            p = write_jsonl([
                tool_use("Read", {"file_path": "/p/alpha.py"}),
                compaction("Nothing about alpha."),
            ], td)
            r = self.run_cli(p)
            self.assertEqual(r.returncode, cd.EXIT_FAIL)
            self.assertIn("ctxdrift", r.stdout)
            self.assertIn("CD04", r.stdout)
            self.assertIn("alpha.py", r.stdout)

    def test_json_report(self):
        with tempfile.TemporaryDirectory() as td:
            p = write_jsonl([compaction("s"), user("hi")], td)
            r = self.run_cli(p, "--format", "json")
            doc = json.loads(r.stdout)
            self.assertEqual(doc["tool"], "ctxdrift")
            self.assertEqual(doc["compactions"], 1)
            self.assertTrue(any(f["rule"] == "CD01" for f in doc["findings"]))

    def test_clean_session_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            p = write_jsonl([user("hello"), assistant("hi there")], td)
            r = self.run_cli(p)
            self.assertEqual(r.returncode, cd.EXIT_CLEAN)
            self.assertIn("CD10", r.stdout)

    def test_rules_filter(self):
        with tempfile.TemporaryDirectory() as td:
            p = write_jsonl([
                tool_use("Read", {"file_path": "/p/beta.py"}),
                compaction("No mention."),
            ], td)
            r = self.run_cli(p, "--rules", "CD01")
            self.assertNotIn("CD04", r.stdout)

    def test_min_severity(self):
        with tempfile.TemporaryDirectory() as td:
            p = write_jsonl([
                tool_use("Read", {"file_path": "/p/gamma.py"}),
                compaction("Nothing."),
            ], td)
            r = self.run_cli(p, "--min-severity", "warn")
            self.assertIn("CD04", r.stdout)   # fail survives
            self.assertNotIn("CD01", r.stdout)  # info filtered

    def test_directory_scan(self):
        with tempfile.TemporaryDirectory() as td:
            write_jsonl([
                tool_use("Read", {"file_path": "/proj/alpha.py"}),
                compaction("s1"),
            ], td, "a.jsonl")
            write_jsonl([user("plain")], td, "b.jsonl")
            r = self.run_cli(td)
            self.assertEqual(r.returncode, cd.EXIT_FAIL)
            self.assertIn("a.jsonl", r.stdout)
            self.assertIn("b.jsonl", r.stdout)

    def test_version(self):
        r = self.run_cli("--version")
        self.assertIn("ctxdrift", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
