"""ctxdrift stress test — 50 malformed transcripts + edge cases, zero crash required."""
import json, random, subprocess, sys, os, tempfile, string

random.seed(42)
td = tempfile.mkdtemp()

def rnd_msg(i):
    r = random.random()
    if r < 0.1:
        return None  # garbage line
    if r < 0.2:
        return "TRUNC"  # non-json
    if r < 0.3:
        return json.dumps(None)
    if r < 0.4:
        return json.dumps({"type": random.choice(["user", "assistant", "tool_result", "weird"]),
                           "message": random.choice([
                               {"role": "user", "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/a.py"}}]},
                               {"content": None},
                               "notadict",
                               42,
                           ])})
    if r < 0.5:
        return json.dumps({"type": "user", "message": {"role": "user",
                           "content": "[CONTEXT COMPACTION — REFERENCE ONLY] " + "x" * random.randint(0, 5000)}})
    if r < 0.7:
        return json.dumps({"type": "assistant", "message": {"role": "assistant",
                           "content": "A" * random.randint(1, 200000)}})
    if r < 0.8:
        return json.dumps({"type": "user", "message": {"role": "user",
                           "content": [{"type": "tool_result", "content": "Y" * random.randint(1, 120000)}]}})
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": random.choice(["Read", "Bash", "Edit", "unknown$tool"]),
         "input": {"file_path": f"/p/f{i}_{random.randint(0, 5)}.py",
                   "command": "ls " * random.randint(0, 50)}}]}})

paths = []
for i in range(50):
    p = os.path.join(td, f"s{i}.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for _ in range(random.randint(0, 80)):
            m = rnd_msg(i)
            fh.write(("<<<GARBAGE " + "".join(random.choices(string.printable, k=200)) + "\n")
                     if m is None else (m + "\n"))
    paths.append(p)

fails = 0
for p in paths:
    r = subprocess.run([sys.executable, "ctxdrift.py", p], capture_output=True, text=True, timeout=60)
    if r.returncode not in (0, 1, 2) or "Traceback" in r.stderr:
        fails += 1
        print("CRASH:", p, r.returncode, r.stderr[:300])
print(f"single-file stress: {50 - fails}/50 clean")

r = subprocess.run([sys.executable, "ctxdrift.py", td], capture_output=True, text=True, timeout=120)
print("dir mode RC:", r.returncode, "clean" if "Traceback" not in r.stderr else "CRASH")
empty = tempfile.mkdtemp()
r = subprocess.run([sys.executable, "ctxdrift.py", empty], capture_output=True, text=True)
print("empty dir RC:", r.returncode, "clean" if "Traceback" not in r.stderr else "CRASH")
r = subprocess.run([sys.executable, "ctxdrift.py", "/nonexistent/file.jsonl"], capture_output=True, text=True)
print("missing file RC:", r.returncode, "clean" if "Traceback" not in r.stderr else "CRASH")
r = subprocess.run([sys.executable, "ctxdrift.py"], capture_output=True, text=True)
print("no-arg RC:", r.returncode, "clean" if "Traceback" not in r.stderr else "CRASH")

# unicode / null bytes / bom
weird = os.path.join(td, "weird.jsonl")
with open(weird, "wb") as fh:
    fh.write(b"\xef\xbb\xbf")
    fh.write(b'{"type":"user","message":{"role":"user","content":"emoji \xf0\x9f\x98\x80 null \\u0000 byte"}}\n')
    fh.write(b"\x00\x01\x02 binary junk\n")
r = subprocess.run([sys.executable, "ctxdrift.py", weird], capture_output=True, text=True)
print("unicode/binary RC:", r.returncode, "clean" if "Traceback" not in r.stderr else "CRASH")
