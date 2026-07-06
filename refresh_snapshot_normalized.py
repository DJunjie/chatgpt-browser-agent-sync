from pathlib import Path

src = Path(r"D:\chatgpt-browser-agent")
dst = Path(r"D:\chatgpt-browser-agent-sync")

files = [
    "agent1.py",
    "agent2.py",
    "agent_common.py",
    "status_ui.py",
]

for name in files:
    sp = src / name
    dp = dst / name

    data = sp.read_bytes()
    print("SRC", name, "bytes=", len(data), "cr=", data.count(b"\r"), "lf=", data.count(b"\n"), "nul=", data.count(b"\x00"))

    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    dp.write_text(text, encoding="utf-8", newline="\n")

    out = dp.read_bytes()
    print("DST", name, "bytes=", len(out), "cr=", out.count(b"\r"), "lf=", out.count(b"\n"), "nul=", out.count(b"\x00"), "lines=", text.count("\n") + 1)
