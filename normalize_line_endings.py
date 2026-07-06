from pathlib import Path

files = [
    Path("agent1.py"),
    Path("agent2.py"),
    Path("agent_common.py"),
    Path("status_ui.py"),
    Path("README_CURRENT_STATE.md"),
    Path(".gitignore"),
]

for p in files:
    data = p.read_bytes()
    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    p.write_text(text, encoding="utf-8", newline="\n")
    print(str(p) + " lines=" + str(text.count("\n") + 1))
