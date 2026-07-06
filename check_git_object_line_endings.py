from pathlib import Path
import subprocess

def count_bytes(label, data):
    print(label, "bytes=", len(data), "cr=", data.count(b"\r"), "lf=", data.count(b"\n"), "nul=", data.count(b"\x00"))

files = ["agent1.py", "agent2.py", "agent_common.py", "status_ui.py"]

print("WORKTREE_COUNTS_BEGIN")
for name in files:
    data = Path(name).read_bytes()
    count_bytes("WORKTREE " + name, data)
print("WORKTREE_COUNTS_END")

print("HEAD_OBJECT_COUNTS_BEGIN")
for name in files:
    r = subprocess.run(["git", "show", "HEAD:" + name], capture_output=True)
    count_bytes("HEAD " + name, r.stdout)
print("HEAD_OBJECT_COUNTS_END")

print("ORIGIN_OBJECT_COUNTS_BEGIN")
subprocess.run(["git", "fetch", "origin", "main"], check=False)
for name in files:
    r = subprocess.run(["git", "show", "origin/main:" + name], capture_output=True)
    count_bytes("ORIGIN " + name, r.stdout)
print("ORIGIN_OBJECT_COUNTS_END")
