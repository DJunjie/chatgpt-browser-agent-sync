from pathlib import Path
import hashlib
import json
import datetime

src = Path(r"D:\chatgpt-browser-agent")
repo = Path(r"D:\chatgpt-browser-agent-sync")

files = [
    "agent1.py",
    "agent2.py",
    "agent_common.py",
    "agent_update_manager.py",
    "status_ui.py",
    "README.md",
    "requirements.txt",
    "config.json",
]

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def read_text(path: Path) -> str:
    data = path.read_bytes()
    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text

def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")

manifest = {
    "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "source_root": str(src),
    "repo_root": str(repo),
    "files": [],
}

for name in files:
    sp = src / name
    if not sp.exists():
        manifest["files"].append({"path": name, "status": "missing"})
        continue

    text = read_text(sp)
    dp = repo / name
    write_text(dp, text)

    data = dp.read_bytes()
    manifest["files"].append({
        "path": name,
        "status": "copied",
        "bytes": len(data),
        "lines": text.count("\n") + 1,
        "sha256": sha256_bytes(data),
    })

report_paths = [
    "workspace/self_update_results/install_self_update_v1_report.json",
    "workspace/agent1_status.json",
    "workspace/agent2_status.json",
    "workspace/live_task/current.json",
    "workspace/live_task/heartbeat.json",
]

diag = repo / "diagnostics"
diag.mkdir(parents=True, exist_ok=True)

for rel in report_paths:
    sp = src / rel
    out = diag / (rel.replace("/", "__").replace("\\", "__") + ".txt")

    if not sp.exists():
        write_text(out, "missing\n")
        manifest["files"].append({
            "path": str(out.relative_to(repo)).replace("\\", "/"),
            "status": "missing",
            "source": rel,
        })
        continue

    text = read_text(sp)
    if len(text) > 20000:
        text = text[-20000:]

    write_text(out, text)
    data = out.read_bytes()

    manifest["files"].append({
        "path": str(out.relative_to(repo)).replace("\\", "/"),
        "status": "copied_tail",
        "source": rel,
        "bytes": len(data),
        "lines": text.count("\n") + 1,
        "sha256": sha256_bytes(data),
    })

write_text(repo / "SNAPSHOT_AFTER_SELF_UPDATE_V1.json", json.dumps(manifest, ensure_ascii=False, indent=2))

print("SYNC_AFTER_INSTALL_V1_DONE")
for item in manifest["files"]:
    print(item)
