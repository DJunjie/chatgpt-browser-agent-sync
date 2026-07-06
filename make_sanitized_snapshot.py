from pathlib import Path
import json
import hashlib
import datetime

src = Path(r"D:\chatgpt-browser-agent")
repo = Path(r"D:\chatgpt-browser-agent-sync")

copy_files = [
    "agent1.py",
    "agent2.py",
    "agent_common.py",
    "status_ui.py",
]

optional_files = [
    "README.md",
    "requirements.txt",
    "config.json",
    "start_agent1.bat",
    "start_agent2.bat",
    "start_status_ui.bat",
    "run_agent1.ps1",
    "run_agent2.ps1",
    "run_status_ui.ps1",
]

safe_status_files = [
    "workspace/agent1_status.json",
    "workspace/agent2_status.json",
    "workspace/live_task/current.json",
    "workspace/live_task/heartbeat.json",
    "workspace/chat_send_lock.json",
    "workspace/delayed_messages.json",
]

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def read_text_safely(path: Path) -> str:
    data = path.read_bytes()
    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text

def write_normalized(dst: Path, text: str):
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8", newline="\n")

manifest = {
    "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "source_root": str(src),
    "repo_root": str(repo),
    "files": [],
}

for name in copy_files + optional_files:
    sp = src / name
    if not sp.exists():
        manifest["files"].append({
            "path": name,
            "status": "missing",
        })
        continue

    text = read_text_safely(sp)
    dp = repo / name
    write_normalized(dp, text)

    data = dp.read_bytes()
    manifest["files"].append({
        "path": name,
        "status": "copied",
        "bytes": len(data),
        "lines": text.count("\n") + 1,
        "sha256": sha256_bytes(data),
    })

diag_dir = repo / "diagnostics"
diag_dir.mkdir(parents=True, exist_ok=True)

for rel in safe_status_files:
    sp = src / rel
    out_name = rel.replace("/", "__").replace("\\", "__")
    dp = diag_dir / out_name

    if not sp.exists():
        write_normalized(dp.with_suffix(dp.suffix + ".missing.txt"), "missing\n")
        manifest["files"].append({
            "path": "diagnostics/" + out_name,
            "status": "missing",
            "source": rel,
        })
        continue

    text = read_text_safely(sp)
    if len(text) > 12000:
        text = text[-12000:]

    write_normalized(dp.with_suffix(dp.suffix + ".txt"), text)

    data = dp.with_suffix(dp.suffix + ".txt").read_bytes()
    manifest["files"].append({
        "path": "diagnostics/" + out_name + ".txt",
        "status": "copied_tail",
        "source": rel,
        "bytes": len(data),
        "lines": text.count("\n") + 1,
        "sha256": sha256_bytes(data),
    })

readme = """# chatgpt-browser-agent-sync

Sanitized source snapshot for review and repair.

Included:
- agent1.py
- agent2.py
- agent_common.py
- status_ui.py
- selected optional launcher/config files if present
- diagnostics/*.txt containing small sanitized status snapshots

Excluded:
- browser profiles
- cookies
- workspace history
- logs
- temp files
- cache files
- zip archives
- credentials or tokens
"""

write_normalized(repo / "README_CURRENT_STATE.md", readme)

gitignore = """__pycache__/
*.pyc
*.pyo
*.log
*.tmp
.env
.env.*
browser_profile*/
browser_profile_default_like/
workspace/
logs/
tmp/
*.zip
*.7z
*.rar
"""

write_normalized(repo / ".gitignore", gitignore)

gitattributes = """*.py text eol=lf
*.md text eol=lf
*.txt text eol=lf
*.json text eol=lf
.gitignore text eol=lf
"""

write_normalized(repo / ".gitattributes", gitattributes)

manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)
write_normalized(repo / "SNAPSHOT_MANIFEST.json", manifest_text)

print("SNAPSHOT_DONE")
for f in manifest["files"]:
    print(f)
