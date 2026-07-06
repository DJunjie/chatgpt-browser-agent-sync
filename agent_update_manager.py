from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "self_update_manager_v1_20260706"
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}$")
DEFAULT_ALLOWED_TARGETS = {
    "agent1.py",
    "agent2.py",
    "agent_common.py",
    "status_ui.py",
    "agent_update_manager.py",
    "README.md",
    "README_CURRENT_STATE.md",
    "requirements.txt",
    "config.json",
}


def now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_id(value: str, name: str = "id") -> str:
    s = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(s):
        raise ValueError(f"{name} must match [A-Za-z0-9_.-]{{1,120}}")
    return s


def ensure_under(root: Path, target: Path) -> Path:
    root = root.resolve()
    target = target.resolve()
    try:
        target.relative_to(root)
    except Exception:
        raise ValueError(f"path escapes root: {target}")
    return target


def normalize_relpath(rel: str) -> str:
    rel = str(rel or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or ":" in rel:
        raise ValueError(f"invalid relative path: {rel!r}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"invalid relative path: {rel!r}")
    return "/".join(parts)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    data = str(text).encode(encoding, errors="replace")
    atomic_write_bytes(path, data)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def root_paths(root: Path) -> Dict[str, Path]:
    root = root.resolve()
    workspace = root / "workspace"
    return {
        "root": root,
        "workspace": workspace,
        "staged": workspace / "staged_updates",
        "results": workspace / "self_update_results",
        "backups": root / "backups" / "self_update",
    }


def package_dir(root: Path, package_id: str) -> Path:
    package_id = safe_id(package_id, "package_id")
    return root_paths(root)["staged"] / package_id


def stage_chunk(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).resolve()
    package_id = safe_id(args.package_id, "package_id")
    idx = int(args.chunk_index)
    count = int(args.chunk_count)
    if idx < 1 or count < 1 or idx > count:
        raise ValueError("chunk_index/chunk_count out of range")
    expected_chunk_sha = str(args.chunk_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_chunk_sha):
        raise ValueError("chunk_sha256 must be a 64-char hex digest")

    b64_path = Path(args.b64_file)
    b64_text = b64_path.read_text(encoding="ascii", errors="strict")
    b64_text = "".join(b64_text.split())
    try:
        raw = base64.b64decode(b64_text.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError("base64 decode failed: " + str(exc))
    actual_chunk_sha = sha256_bytes(raw)
    if actual_chunk_sha.lower() != expected_chunk_sha.lower():
        raise ValueError(f"chunk sha mismatch: expected={expected_chunk_sha} actual={actual_chunk_sha}")

    pkg = package_dir(root, package_id)
    chunks_dir = pkg / "chunks"
    state_path = pkg / "state.json"
    part_path = chunks_dir / f"part_{idx:05d}.bin"
    atomic_write_bytes(part_path, raw)

    state = read_json(state_path, default={}) or {}
    state.setdefault("version", VERSION)
    state.setdefault("package_id", package_id)
    state["chunk_count"] = count
    state.setdefault("created_at", now_iso())
    state["updated_at"] = now_iso()
    parts = state.setdefault("parts", {})
    parts[str(idx)] = {
        "index": idx,
        "path": str(part_path),
        "bytes": len(raw),
        "sha256": actual_chunk_sha,
        "updated_at": now_iso(),
    }
    received = sorted(int(k) for k in parts.keys())
    missing = [i for i in range(1, count + 1) if i not in set(received)]
    state["received_count"] = len(received)
    state["missing"] = missing
    write_json(state_path, state)
    return {
        "status": "ok",
        "action": "stage_chunk",
        "version": VERSION,
        "package_id": package_id,
        "chunk_index": idx,
        "chunk_count": count,
        "received_count": len(received),
        "missing_count": len(missing),
        "chunk_sha256": actual_chunk_sha,
        "state_path": str(state_path),
    }


def inspect_package(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).resolve()
    package_id = safe_id(args.package_id, "package_id")
    pkg = package_dir(root, package_id)
    state = read_json(pkg / "state.json", default={}) or {}
    parts = state.get("parts") if isinstance(state, dict) else {}
    if not isinstance(parts, dict):
        parts = {}
    count = int(state.get("chunk_count") or 0) if isinstance(state, dict) else 0
    received = sorted(int(k) for k in parts.keys())
    missing = [i for i in range(1, count + 1) if i not in set(received)] if count else []
    return {
        "status": "ok",
        "action": "inspect",
        "version": VERSION,
        "package_id": package_id,
        "package_dir": str(pkg),
        "exists": pkg.exists(),
        "chunk_count": count,
        "received_count": len(received),
        "received": received,
        "missing": missing,
        "state": state,
    }


def assemble_payload(root: Path, package_id: str, expected_sha256: str) -> Tuple[Path, bytes, Dict[str, Any]]:
    package_id = safe_id(package_id, "package_id")
    expected_sha256 = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("payload_sha256 must be a 64-char hex digest")
    pkg = package_dir(root, package_id)
    state = read_json(pkg / "state.json", default={}) or {}
    count = int(state.get("chunk_count") or 0)
    if count <= 0:
        raise ValueError("no staged chunks found")
    parts = state.get("parts") or {}
    if not isinstance(parts, dict):
        raise ValueError("invalid staged state: parts is not a dict")
    missing = [i for i in range(1, count + 1) if str(i) not in parts]
    if missing:
        raise ValueError("missing chunks: " + ",".join(map(str, missing[:30])))

    chunks: List[bytes] = []
    for i in range(1, count + 1):
        info = parts[str(i)]
        data = Path(info["path"]).read_bytes()
        actual = sha256_bytes(data)
        if actual.lower() != str(info.get("sha256") or "").lower():
            raise ValueError(f"stored chunk sha mismatch at {i}")
        chunks.append(data)
    payload = b"".join(chunks)
    actual_payload_sha = sha256_bytes(payload)
    if actual_payload_sha.lower() != expected_sha256.lower():
        raise ValueError(f"payload sha mismatch: expected={expected_sha256} actual={actual_payload_sha}")
    payload_path = pkg / "payload.bin"
    atomic_write_bytes(payload_path, payload)
    return payload_path, payload, state


def safe_extract_zip(zip_path: Path, extract_dir: Path) -> None:
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    root = extract_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or ":" in name or ".." in name.split("/"):
                raise ValueError("unsafe zip member: " + info.filename)
            target = (extract_dir / name).resolve()
            ensure_under(root, target)
        zf.extractall(extract_dir)


def compile_files(root: Path, relpaths: List[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for rel in relpaths:
        rel_norm = normalize_relpath(rel)
        path = ensure_under(root, root / rel_norm)
        item: Dict[str, Any] = {"path": rel_norm, "absolute": str(path)}
        try:
            py_compile.compile(str(path), doraise=True)
            item["status"] = "ok"
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = type(exc).__name__ + ": " + str(exc)
            results.append(item)
            raise RuntimeError("compile failed for " + rel_norm + ": " + item["error"])
        results.append(item)
    return results


def maybe_git_sync(root: Path, changed_relpaths: List[str], config: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"enabled": False}
    if not config or not config.get("enabled"):
        return result
    repo_raw = config.get("repo") or config.get("repo_path") or "D:/chatgpt-browser-agent-sync"
    repo = Path(str(repo_raw)).resolve()
    result = {"enabled": True, "repo": str(repo), "steps": []}
    if not repo.exists():
        result["status"] = "repo_missing"
        return result
    for rel in changed_relpaths:
        rel_norm = normalize_relpath(rel)
        src = ensure_under(root, root / rel_norm)
        dst = ensure_under(repo, repo / rel_norm)
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        if rel_norm.endswith((".py", ".md", ".txt", ".json", ".ps1", ".bat")):
            text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
            atomic_write_bytes(dst, text.encode("utf-8"))
        else:
            atomic_write_bytes(dst, data)
    def run_git(cmd: List[str]) -> Dict[str, Any]:
        p = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace")
        item = {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]}
        result["steps"].append(item)
        return item
    run_git(["git", "config", "core.autocrlf", "false"])
    run_git(["git", "add"] + changed_relpaths)
    status = run_git(["git", "status", "--porcelain"])
    if not status.get("stdout", "").strip():
        result["status"] = "no_changes"
        return result
    message = str(config.get("commit_message") or ("Self-update " + now_iso()))
    commit = run_git(["git", "commit", "-m", message])
    if commit["returncode"] != 0:
        result["status"] = "commit_failed"
        return result
    if config.get("push", True):
        push = run_git(["git", "push", "origin", "main"])
        result["status"] = "pushed" if push["returncode"] == 0 else "push_failed"
    else:
        result["status"] = "committed_no_push"
    return result


def schedule_restart(root: Path, delay_sec: int = 12, python_exe: Optional[str] = None) -> Dict[str, Any]:
    python_exe = python_exe or sys.executable
    workspace = root_paths(root)["workspace"]
    scripts = workspace / "restart_scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    ps1 = scripts / f"restart_agents_after_self_update_{stamp}.ps1"
    root_s = str(root).replace("'", "''")
    py_s = str(python_exe).replace("'", "''")
    text = f"""$ErrorActionPreference = 'Continue'
Start-Sleep -Seconds {int(delay_sec)}
$Root = '{root_s}'
$Py = '{py_s}'
$needle1 = 'agent1.py'
$needle2 = 'agent2.py'
Get-CimInstance Win32_Process | Where-Object {{
    ($_.CommandLine -like "*$needle1*") -or ($_.CommandLine -like "*$needle2*")
}} | ForEach-Object {{
    try {{ Stop-Process -Id $_.ProcessId -Force }} catch {{}}
}}
Start-Sleep -Seconds 2
try {{ Start-Process -FilePath $Py -ArgumentList @((Join-Path $Root 'agent2.py')) -WorkingDirectory $Root -WindowStyle Minimized }} catch {{}}
Start-Sleep -Seconds 3
$agent1Running = @(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*agent1.py*' }}).Count -gt 0
if (-not $agent1Running) {{
    try {{ Start-Process -FilePath $Py -ArgumentList @((Join-Path $Root 'agent1.py')) -WorkingDirectory $Root -WindowStyle Minimized }} catch {{}}
}}
"""
    atomic_write_text(ps1, text, encoding="utf-8")
    subprocess.Popen([
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)
    ], cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    return {"scheduled": True, "delay_sec": delay_sec, "script": str(ps1)}


def apply_manifest(root: Path, extract_dir: Path, manifest: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
    package_id = safe_id(str(manifest.get("package_id") or request.get("package_id") or "manual_update"), "package_id")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("update_manifest.json must contain a non-empty files list")
    allowed_targets = set(DEFAULT_ALLOWED_TARGETS)
    for extra in manifest.get("allowed_targets", []) or []:
        allowed_targets.add(normalize_relpath(str(extra)))

    backup_dir = root_paths(root)["backups"] / (package_id + "_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    changed_relpaths: List[str] = []
    planned: List[Dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("file item is not object")
        source_rel = normalize_relpath(item.get("source") or item.get("path") or "")
        target_rel = normalize_relpath(item.get("target") or source_rel)
        if target_rel not in allowed_targets:
            raise ValueError("target is not allowed by manifest: " + target_rel)
        source = ensure_under(extract_dir, extract_dir / source_rel)
        target = ensure_under(root, root / target_rel)
        data = source.read_bytes()
        expected = str(item.get("sha256") or "").strip().lower()
        actual = sha256_bytes(data)
        if expected and actual.lower() != expected.lower():
            raise ValueError(f"source sha mismatch for {source_rel}: expected={expected} actual={actual}")
        planned.append({
            "source_rel": source_rel,
            "target_rel": target_rel,
            "source": str(source),
            "target": str(target),
            "bytes": len(data),
            "sha256": actual,
            "existed_before": target.exists(),
            "before_sha256": sha256_bytes(target.read_bytes()) if target.exists() and target.is_file() else None,
        })

    applied: List[Dict[str, Any]] = []
    try:
        for item in planned:
            target = Path(item["target"])
            backup_path = backup_dir / item["target_rel"]
            if target.exists() and target.is_file():
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_path)
            data = Path(item["source"]).read_bytes()
            atomic_write_bytes(target, data)
            after = sha256_bytes(target.read_bytes())
            if after != item["sha256"]:
                raise RuntimeError("post-write sha mismatch for " + item["target_rel"])
            changed_relpaths.append(item["target_rel"])
            applied.append({**item, "status": "written", "after_sha256": after})

        compile_list = manifest.get("compile")
        if compile_list is None:
            compile_list = [p for p in changed_relpaths if p.endswith(".py")]
        if not isinstance(compile_list, list):
            raise ValueError("compile must be a list")
        compile_results = compile_files(root, [str(x) for x in compile_list])

    except Exception:
        rollback_errors = []
        for item in reversed(planned):
            target = Path(item["target"])
            backup_path = backup_dir / item["target_rel"]
            try:
                if item["existed_before"] and backup_path.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path, target)
                elif target.exists():
                    target.unlink()
            except Exception as exc:
                rollback_errors.append({"target": item["target_rel"], "error": str(exc)})
        raise RuntimeError("apply failed and rollback attempted; rollback_errors=" + json.dumps(rollback_errors, ensure_ascii=False))

    verify_after = []
    for item in planned:
        target = Path(item["target"])
        verify_after.append({
            "target_rel": item["target_rel"],
            "expected_sha256": item["sha256"],
            "actual_sha256": sha256_bytes(target.read_bytes()),
            "bytes": target.stat().st_size,
        })

    git_config = request.get("git_sync") or manifest.get("git_sync") or {}
    git_result = maybe_git_sync(root, changed_relpaths, git_config if isinstance(git_config, dict) else {})

    restart_req = bool(request.get("restart", manifest.get("restart", False)))
    restart_result = None
    if restart_req:
        delay_sec = int(request.get("restart_delay_sec", manifest.get("restart_delay_sec", 12)) or 12)
        restart_result = schedule_restart(root, delay_sec=delay_sec)

    return {
        "status": "ok",
        "action": "apply_manifest",
        "version": VERSION,
        "package_id": package_id,
        "backup_dir": str(backup_dir),
        "changed_relpaths": changed_relpaths,
        "applied": applied,
        "compile_results": compile_results,
        "verify_after": verify_after,
        "git_sync": git_result,
        "restart": restart_result,
    }


def apply_staged_update(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).resolve()
    request = read_json(Path(args.request_file), default={}) or {}
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    package_id = safe_id(request.get("package_id") or args.package_id, "package_id")
    payload_sha = str(request.get("payload_sha256") or args.payload_sha256 or "").strip().lower()
    payload_kind = str(request.get("payload_kind") or args.payload_kind or "zip").strip().lower()
    payload_path, payload, state = assemble_payload(root, package_id, payload_sha)
    pkg = package_dir(root, package_id)
    result: Dict[str, Any] = {
        "status": "ok",
        "action": "apply_staged_update",
        "version": VERSION,
        "package_id": package_id,
        "payload_kind": payload_kind,
        "payload_path": str(payload_path),
        "payload_bytes": len(payload),
        "payload_sha256": sha256_bytes(payload),
    }

    if payload_kind != "zip":
        raise ValueError("only payload_kind=zip is enabled in unattended self-update v1")
    zip_path = pkg / "payload.zip"
    atomic_write_bytes(zip_path, payload)
    extract_dir = pkg / "extract"
    safe_extract_zip(zip_path, extract_dir)
    manifest_path = extract_dir / "update_manifest.json"
    if not manifest_path.exists():
        raise ValueError("payload zip missing update_manifest.json")
    manifest = read_json(manifest_path, default=None)
    if not isinstance(manifest, dict):
        raise ValueError("update_manifest.json is not a JSON object")
    apply_result = apply_manifest(root, extract_dir, manifest, request)
    result["manifest_path"] = str(manifest_path)
    result["apply_result"] = apply_result
    results_dir = root_paths(root)["results"]
    results_dir.mkdir(parents=True, exist_ok=True)
    write_json(results_dir / (package_id + ".json"), result)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ChatGPT local agent self-update manager")
    parser.add_argument("command", choices=["stage", "inspect", "apply"])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--package-id", default="")
    parser.add_argument("--chunk-index", type=int, default=0)
    parser.add_argument("--chunk-count", type=int, default=0)
    parser.add_argument("--chunk-sha256", default="")
    parser.add_argument("--b64-file", default="")
    parser.add_argument("--request-file", default="")
    parser.add_argument("--payload-sha256", default="")
    parser.add_argument("--payload-kind", default="zip")
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            result = stage_chunk(args)
        elif args.command == "inspect":
            result = inspect_package(args)
        elif args.command == "apply":
            result = apply_staged_update(args)
        else:
            raise ValueError("unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        result = {
            "status": "failed",
            "action": args.command,
            "version": VERSION,
            "error": type(exc).__name__ + ": " + str(exc),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
