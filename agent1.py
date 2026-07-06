from __future__ import annotations

import json
import hashlib
import os
import time
import traceback
import subprocess
import re
import sys
import threading
import queue
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright

from agent_common import (
    CFG, WORKSPACE, LOGS, AGENT1_STATUS, AGENT2_STATUS, AGENT1_PID,
    PROCESSED_KEYS, AGENT2_CONTROL, AGENT_CONTROL, AGENT1_OUTBOX, now_iso, utc_ts, sha12, truncate,
    atomic_write_json, atomic_write_text, read_json, log_line, find_processes,
    start_detached_python, process_age_status,
    LIVE_TASK_DIR, LIVE_TASK_HISTORY, DELAYED_MESSAGES, CHAT_SEND_LOCK,
    acquire_chat_send_lock, release_chat_send_lock, set_delayed_message, clear_delayed_message, tail_file,
)

# V9_3_SEND_FIX_AGENT1
# STDOUT_TAIL_DECODE_BY_LINE_CONTEXT_MARKER\n# VISIBLE_POWERSHELL_UTF8_HOTFIX_MARKER
START_MARKER = "[[LOCAL_AGENT_START]]"
END_MARKER = "[[LOCAL_AGENT_END]]"
LOCAL_AGENT_V2 = "LOCAL_AGENT_V2"
LOCAL_AGENT_V2_END = "LOCAL_AGENT_V2_END"
POLL_SEC = float(CFG.get("poll_sec", 2.0))
ENABLE_LOCAL_AGENT_V1 = bool(CFG.get("enable_local_agent_v1", False))
V2_STABLE_SEC = float(CFG.get("local_agent_v2_stable_sec", 1.0))
V2_END_MARKER_TIMEOUT_SEC = float(CFG.get("local_agent_v2_end_marker_timeout_sec", 30.0))
V2_PARSE_ERROR_NOTIFY = bool(CFG.get("local_agent_v2_parse_error_notify", False))
PENDING_V2_TIMEOUT_SEC = float(CFG.get("local_agent_v2_pending_timeout_sec", 60.0))
SEND_MESSAGE_RETRY_COUNT = int(CFG.get("send_message_retry_count", 3))
SEND_BUTTON_WAIT_SEC = float(CFG.get("send_button_wait_sec", 8.0))

PENDING_OUTBOX = WORKSPACE / "pending_outbox"
PENDING_SENT = WORKSPACE / "pending_outbox_sent"
ROOT_DIR = Path(str(CFG.get("root_dir") or Path(__file__).resolve().parent)).resolve()
UPDATES_DIR = ROOT_DIR / "updates"
EXECUTED_TASKS = WORKSPACE / "executed_tasks.jsonl"
LOCAL_AGENT_V2_ALLOWED_HEADERS = {
    "task_id",
    "type",
    "cwd",
    "timeout",
    "max_output_chars",
    "allow_write",
    "dry_run",
    "soft_check_after_sec",
    "heartbeat_report",
}
SCRIPT_KEEP_COUNT = int(CFG.get("local_agent_tmp_script_keep_count", 40))

URL_NORMALIZED_ONCE = [False]
LAYOUT_FIX_DONE_ONCE = [False]
LAST_LAYOUT_FIX_TS = [0.0]
BROWSER_RECOVER_MAX_FAILURES = int(CFG.get("browser_recover_max_failures", 3))

# DEFAULT_LIKE_CHROME_ATTACH_MODE_MARKER
# Chrome 136+ does not expose CDP for the real default Chrome user-data directory.
# Therefore agent1 opens/attaches to a non-default user-data directory that is initialized
# from the user's default Chrome Preferences. This keeps DPI/page zoom/layout close to normal
# Chrome without controlling or closing the user's daily Chrome process.
ATTACHED_CDP_BROWSER = [None]
ATTACHED_CDP_URL = str(CFG.get("chrome_cdp_url") or "http://127.0.0.1:9222")
DEFAULT_CHROME_USER_DATA_DIR = str(CFG.get("default_chrome_user_data_dir") or (Path(os.environ.get("LOCALAPPDATA") or "") / "Google/Chrome/User Data"))
DEFAULT_CHROME_PROFILE_DIR = str(CFG.get("default_chrome_profile_directory") or "Default")
DEFAULT_LIKE_CHROME_USER_DATA_DIR = str(CFG.get("default_like_chrome_user_data_dir") or (ROOT_DIR / "browser_profile_default_like"))
ATTACH_DEFAULT_CHROME = bool(CFG.get("attach_to_default_chrome", True))
OPEN_NEW_CHROME_WINDOW_ON_START = bool(CFG.get("open_new_chrome_window_on_start", True))
COPY_DEFAULT_CHROME_PREFS_ON_INIT = bool(CFG.get("copy_default_chrome_prefs_on_init", True))



def status(**kwargs: Any) -> None:
    state = kwargs.pop("state", "unknown")
    phase = kwargs.pop("phase", state)
    when = now_iso()
    base = {
        "agent": "agent1",
        "pid": os.getpid(),
        "state": state,
        "phase": phase,
        "time": when,
        "last_error": kwargs.pop("last_error", None),
        "updated_at": when,
        "updated_ts": utc_ts(),
        "version": "v11_local_agent_v2",
    }
    base.update(kwargs)
    atomic_write_json(AGENT1_STATUS, base)


def load_processed() -> set[str]:
    try:
        data = json.loads(PROCESSED_KEYS.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(map(str, data))
    except Exception:
        pass
    return set()


def save_processed(keys: set[str]) -> None:
    atomic_write_text(PROCESSED_KEYS, json.dumps(sorted(keys), ensure_ascii=False, indent=2), encoding="utf-8")


def sanitize_task_id(task_id: str) -> str:
    task_id = str(task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", task_id):
        raise ValueError("task_id must match [A-Za-z0-9_.-]{1,120}")
    return task_id


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def load_executed_task_status(task_id: str) -> Optional[str]:
    try:
        task_id = sanitize_task_id(task_id)
    except Exception:
        return None
    if not EXECUTED_TASKS.exists():
        return None
    latest = None
    try:
        with EXECUTED_TASKS.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if str(item.get("task_id") or "") == task_id:
                    latest = str(item.get("status") or "")
    except Exception:
        return latest
    return latest


def record_executed_task(task_id: str, task_status: str, **extra: Any) -> None:
    try:
        task_id = sanitize_task_id(task_id)
    except Exception:
        return
    item: Dict[str, Any] = {
        "task_id": task_id,
        "status": task_status,
        "time": now_iso(),
        "ts": utc_ts(),
    }
    item.update(extra)
    EXECUTED_TASKS.parent.mkdir(parents=True, exist_ok=True)
    with EXECUTED_TASKS.open("a", encoding="utf-8", errors="replace") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def duplicate_task_result(task_id: str) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "overall_status": "skipped_duplicate_task_id",
        "results": [
            {
                "index": 0,
                "type": "dedupe",
                "status": "skipped",
                "result": {"status": "skipped_duplicate_task_id", "task_id": task_id},
                "error": None,
            }
        ],
    }


def extract_blocks(text: str) -> List[str]:
    blocks = []
    start = 0
    while True:
        i = text.find(START_MARKER, start)
        if i < 0:
            break
        j = text.find(END_MARKER, i + len(START_MARKER))
        if j < 0:
            break
        blocks.append(text[i + len(START_MARKER):j].strip())
        start = j + len(END_MARKER)
    return blocks


def command_key(cmd: Dict[str, Any], raw_hash: str) -> str:
    tid = cmd.get("task_id")
    if tid:
        return "task_id:" + str(tid)
    return "block_hash:" + raw_hash


def parse_command_block(raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        cmd = json.loads(raw)
        if not isinstance(cmd, dict):
            return None, "top-level JSON is not an object"
        if "actions" not in cmd or not isinstance(cmd["actions"], list):
            return None, "missing actions list"
        return cmd, None
    except Exception as exc:
        return None, type(exc).__name__ + ": " + str(exc)


def line_is_v2_end_marker(line: str) -> bool:
    stripped = str(line or "").strip()
    if stripped == LOCAL_AGENT_V2_END:
        return True
    if stripped.startswith("#") and stripped[1:].strip() == LOCAL_AGENT_V2_END:
        return True
    return False


def strip_local_agent_v2_end_marker(body: str) -> Tuple[str, bool]:
    lines = str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    found = False
    kept: List[str] = []
    for line in lines:
        if line_is_v2_end_marker(line):
            found = True
            continue
        kept.append(line)
    return "\n".join(kept), found


def code_first_line(code: str) -> str:
    return str(code or "").replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0] if code else ""


def code_has_local_agent_v2_end_marker(code: str) -> bool:
    text = str(code or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    sep_index = None
    for i, line in enumerate(lines[1:], start=1):
        if line == "---":
            sep_index = i
            break
    if sep_index is None:
        return False
    _body, found = strip_local_agent_v2_end_marker("\n".join(lines[sep_index + 1:]))
    return found


def parse_local_agent_v2_code(code: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        text = str(code or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        if not lines or lines[0] != LOCAL_AGENT_V2:
            return None, None

        sep_index = None
        for i, line in enumerate(lines[1:], start=1):
            if line == "---":
                sep_index = i
                break
        if sep_index is None:
            return None, "missing --- separator"

        header_lines = lines[1:sep_index]
        body = "\n".join(lines[sep_index + 1:])
        body, has_end_marker = strip_local_agent_v2_end_marker(body)
        if not has_end_marker:
            return None, LOCAL_AGENT_V2_END + " missing"
        headers: Dict[str, str] = {}
        for raw_line in header_lines:
            line = raw_line.strip()
            if not line:
                continue
            if "=" not in line:
                return None, "header line must be key=value: " + raw_line
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key not in LOCAL_AGENT_V2_ALLOWED_HEADERS:
                return None, "unsupported header: " + key
            if key in headers:
                return None, "duplicate header: " + key
            headers[key] = value

        for required in ("task_id", "type", "timeout"):
            if not headers.get(required):
                return None, "missing required header: " + required

        task_id = sanitize_task_id(headers["task_id"])
        action_type = headers["type"].strip()
        timeout = int(headers["timeout"])
        if timeout <= 0:
            return None, "timeout must be positive"
        if timeout > 3600:
            return None, "timeout exceeds 3600 seconds"
        max_output_chars = int(headers.get("max_output_chars") or CFG.get("max_output_chars", 20000) or 20000)
        max_output_chars = max(1000, min(max_output_chars, 200000))
        cwd = headers.get("cwd") or str(ROOT_DIR)

        if action_type == "run_powershell":
            if not body.strip():
                return None, "run_powershell body is empty"
            action = {
                "type": "run_powershell",
                "command": body,
                "cwd": cwd,
                "timeout": timeout,
                "max_output_chars": max_output_chars,
                "task_id": task_id,
                "protocol": LOCAL_AGENT_V2,
                "allow_write": parse_bool(headers.get("allow_write"), False),
                "soft_check_after_sec": headers.get("soft_check_after_sec"),
                "heartbeat_report": parse_bool(headers.get("heartbeat_report"), False),
            }
        elif action_type == "apply_update_package":
            zip_line = ""
            for line in body.splitlines():
                if line.strip():
                    zip_line = line.strip()
                    break
            if not zip_line:
                return None, "apply_update_package body must contain an update zip path"
            action = {
                "type": "apply_update_package",
                "zip_path": zip_line,
                "cwd": cwd,
                "timeout": timeout,
                "max_output_chars": max_output_chars,
                "task_id": task_id,
                "protocol": LOCAL_AGENT_V2,
                "dry_run": parse_bool(headers.get("dry_run"), False),
            }
        else:
            return None, "unsupported type: " + action_type

        return {
            "task_id": task_id,
            "protocol": LOCAL_AGENT_V2,
            "headers": headers,
            "actions": [action],
        }, None
    except Exception as exc:
        return None, type(exc).__name__ + ": " + str(exc)


def configured_browser_executable() -> Optional[str]:
    # Prefer official stable Google Chrome when available.
    candidates: List[str] = []
    for key in ("chrome_executable_path", "browser_executable_path"):
        value = str(CFG.get(key) or "").strip()
        if value:
            candidates.append(value)

    local_appdata = os.environ.get("LOCALAPPDATA") or ""
    candidates.extend([
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        str(Path(local_appdata) / "Google/Chrome/Application/chrome.exe") if local_appdata else "",
    ])

    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        normalized = str(raw).replace("\\", "/")
        if normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        try:
            p = Path(normalized)
            if p.exists() and p.is_file():
                return str(p)
        except Exception:
            continue
    return None


def build_v2_candidate(v2_scan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    code_blocks = v2_scan.get("code_blocks") or []
    v2_code_blocks = [str(x) for x in code_blocks if code_first_line(str(x)) == LOCAL_AGENT_V2]
    if not v2_scan.get("found") or len(v2_code_blocks) != 1:
        return None

    raw = str(v2_code_blocks[0])
    raw_hash = sha12(raw)
    now = time.time()
    return {
        "raw": raw,
        "raw_hash": raw_hash,
        "outside_text": str(v2_scan.get("outside_text") or "").strip(),
        "first_seen_ts": now,
        "last_seen_ts": now,
        "source_dom_index": v2_scan.get("dom_index"),
        "latest_user_dom_index": v2_scan.get("latest_user_dom_index"),
        "assistant_messages_after_latest_user": v2_scan.get("assistant_messages_after_latest_user"),
        "selected_assistant_offset_from_end": v2_scan.get("selected_assistant_offset_from_end"),
        "skipped_newer_assistant_messages": v2_scan.get("skipped_newer_assistant_messages"),
    }


def queue_pending_outbox_message(task_id: str, message: str, reason: str = "send_failed") -> Optional[Path]:
    try:
        PENDING_OUTBOX.mkdir(parents=True, exist_ok=True)
        safe = sanitize_task_id(task_id or ("observation_" + time.strftime("%Y%m%d_%H%M%S")))
        path = PENDING_OUTBOX / (safe + ".txt")
        if path.exists():
            path = PENDING_OUTBOX / (safe + "_" + time.strftime("%Y%m%d_%H%M%S") + ".txt")
        atomic_write_text(path, message, encoding="utf-8")
        meta = {
            "task_id": task_id,
            "reason": reason,
            "path": str(path),
            "created_at": now_iso(),
            "created_ts": utc_ts(),
            "chars": len(message),
        }
        atomic_write_text(path.with_suffix(path.suffix + ".json"), json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log_line("agent1.log", "queued_pending_outbox " + json.dumps(meta, ensure_ascii=False))
        return path
    except Exception as exc:
        log_line("agent1.log", "queue_pending_outbox_failed: " + str(exc))
        return None




def cdp_endpoint_available(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except Exception:
        return False


def wait_for_cdp_endpoint(url: str, timeout_sec: float = 12.0) -> bool:
    deadline = time.time() + max(0.5, timeout_sec)
    while time.time() < deadline:
        if cdp_endpoint_available(url, timeout=0.8):
            return True
        time.sleep(0.35)
    return cdp_endpoint_available(url, timeout=0.8)


def chrome_exe_path_for_default_profile() -> Optional[str]:
    configured = configured_browser_executable()
    if configured:
        return configured
    candidates = [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA") or ""
    if local_appdata:
        candidates.append(str(Path(local_appdata) / "Google/Chrome/Application/chrome.exe"))
    for raw in candidates:
        try:
            p = Path(raw)
            if p.exists() and p.is_file():
                return str(p)
        except Exception:
            pass
    return None



def _copy_file_if_exists(src: Path, dst: Path, max_bytes: int = 50 * 1024 * 1024) -> Dict[str, Any]:
    try:
        if not src.exists() or not src.is_file():
            return {"copied": False, "reason": "source_missing", "source": str(src)}
        size = src.stat().st_size
        if size > max_bytes:
            return {"copied": False, "reason": "too_large", "source": str(src), "size": size}
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
        return {"copied": True, "source": str(src), "target": str(dst), "size": size}
    except Exception as exc:
        return {"copied": False, "source": str(src), "target": str(dst), "error": type(exc).__name__ + ": " + str(exc)[:300]}


def remove_chrome_lock_files(user_data_dir: Path) -> None:
    for name in ("SingletonCookie", "SingletonLock", "SingletonSocket", "lockfile"):
        try:
            p = user_data_dir / name
            if p.exists():
                p.unlink()
        except Exception:
            pass


def ensure_default_like_chrome_profile() -> Dict[str, Any]:
    # DEFAULT_LIKE_PROFILE_INIT_MARKER
    # Initialize a non-default Chrome profile with the user's normal Chrome preferences.
    # Do not copy Cookies/Login Data here. Chrome remote debugging intentionally refuses the
    # real default profile; this clone is for layout/zoom/settings compatibility and safe CDP.
    result: Dict[str, Any] = {
        "user_data_dir": DEFAULT_LIKE_CHROME_USER_DATA_DIR,
        "source_user_data_dir": DEFAULT_CHROME_USER_DATA_DIR,
        "profile_directory": DEFAULT_CHROME_PROFILE_DIR,
        "copied": [],
    }
    dst_root = Path(DEFAULT_LIKE_CHROME_USER_DATA_DIR)
    src_root = Path(DEFAULT_CHROME_USER_DATA_DIR)
    profile = DEFAULT_CHROME_PROFILE_DIR or "Default"
    marker = dst_root / ".agent_default_like_profile_initialized.json"

    dst_root.mkdir(parents=True, exist_ok=True)
    (dst_root / profile).mkdir(parents=True, exist_ok=True)
    remove_chrome_lock_files(dst_root)

    first_init = not marker.exists()
    result["first_init"] = first_init
    if COPY_DEFAULT_CHROME_PREFS_ON_INIT and first_init:
        # Keep this small: Preferences/Local State are enough for zoom, language, appearance,
        # browser UI behavior and many site settings. Full profile copy is intentionally avoided.
        pairs = [
            (src_root / "Local State", dst_root / "Local State"),
            (src_root / "First Run", dst_root / "First Run"),
            (src_root / profile / "Preferences", dst_root / profile / "Preferences"),
            (src_root / profile / "Secure Preferences", dst_root / profile / "Secure Preferences"),
            (src_root / profile / "Bookmarks", dst_root / profile / "Bookmarks"),
        ]
        for s, d in pairs:
            result["copied"].append(_copy_file_if_exists(s, d))
        atomic_write_json(marker, {
            "created_at": now_iso(),
            "source_user_data_dir": str(src_root),
            "target_user_data_dir": str(dst_root),
            "profile_directory": profile,
            "note": "Initialized from default Chrome settings only; cookies/login data not copied.",
        })
    return result


def open_default_chrome_new_window(url: str, include_debug_flags: bool = True) -> Dict[str, Any]:
    chrome = chrome_exe_path_for_default_profile()
    profile_info = ensure_default_like_chrome_profile()
    if not chrome:
        return {"ok": False, "error": "chrome.exe not found", "profile_info": profile_info}
    args = [chrome]
    if include_debug_flags:
        args.extend([
            "--remote-debugging-port=9222",
            "--remote-allow-origins=*",
        ])
    args.extend([
        "--user-data-dir=" + DEFAULT_LIKE_CHROME_USER_DATA_DIR,
        "--profile-directory=" + DEFAULT_CHROME_PROFILE_DIR,
        "--new-window",
        url,
    ])
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            args,
            cwd=str(Path(chrome).parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return {"ok": True, "pid": proc.pid, "chrome": chrome, "args": args, "profile_info": profile_info}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc), "chrome": chrome, "args": args, "profile_info": profile_info}

def launch_context(pw):
    # REUSE_EXISTING_CHROME_WINDOW_MARKER
    # Prefer reusing the existing CDP browser/window.
    # Only launch Chrome when CDP is not available.
    import urllib.request
    import urllib.error
    import subprocess
    import shutil

    cdp_url = str(CFG.get("cdp_url") or "http://127.0.0.1:9222").strip()
    chat_url = str(CFG.get("chat_url") or "https://chatgpt.com/").strip()
    profile_dir = ROOT_DIR / "browser_profile_default_like"

    def cdp_available() -> bool:
        try:
            with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=2.5) as r:
                return 200 <= int(getattr(r, "status", 200)) < 300
        except Exception:
            return False

    def connect_existing(reason: str):
        browser = pw.chromium.connect_over_cdp(cdp_url)
        contexts = list(browser.contexts)
        if contexts:
            context = contexts[0]
        else:
            context = browser.new_context(no_viewport=True)
        try:
            pages = []
            for ctx in browser.contexts:
                pages.extend(ctx.pages)
            status(
                state="starting",
                phase="attached_existing_browser",
                browser_mode="reuse_existing_chrome_window",
                cdp_url=cdp_url,
                attach_reason=reason,
                attached_contexts=len(browser.contexts),
                attached_pages=len(pages),
            )
            log_line("agent1.log", "reuse_existing_chrome_window reason=" + reason + " pages=" + str(len(pages)))
        except Exception:
            pass
        return context

    if cdp_available():
        return connect_existing("cdp_already_available")

    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy a small subset of normal Chrome preferences on first use.
    # Do not copy cookies or login databases.
    try:
        src_user_data = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
        src_default = src_user_data / "Default"
        dst_default = profile_dir / "Default"
        dst_default.mkdir(parents=True, exist_ok=True)
        for name in ["Local State"]:
            src = src_user_data / name
            dst = profile_dir / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
        for name in ["Preferences", "Secure Preferences", "Bookmarks"]:
            src = src_default / name
            dst = dst_default / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
    except Exception as exc:
        log_line("agent1.log", "copy_default_like_prefs_failed: " + str(exc))

    chrome_path = configured_browser_executable()
    if not chrome_path:
        raise RuntimeError("Chrome executable not found")

    args = [
        chrome_path,
        "--remote-debugging-port=9222",
        "--remote-allow-origins=*",
        "--user-data-dir=" + str(profile_dir),
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        chat_url,
    ]

    launch_info = {"chrome": chrome_path, "args": args}
    try:
        proc = subprocess.Popen(args, cwd=str(ROOT_DIR))
        launch_info["ok"] = True
        launch_info["pid"] = proc.pid
        log_line("agent1.log", "launch_default_like_chrome_for_cdp pid=" + str(proc.pid))
    except Exception as exc:
        launch_info["ok"] = False
        launch_info["error"] = type(exc).__name__ + ": " + str(exc)
        status(
            state="browser_launch_failed",
            phase="launching_browser",
            browser_mode="default_like_clone",
            cdp_url=cdp_url,
            launch_info=launch_info,
            last_error=launch_info["error"],
        )
        raise

    deadline = time.time() + 20.0
    while time.time() < deadline:
        if cdp_available():
            return connect_existing("launched_new_chrome_because_cdp_unavailable")
        time.sleep(0.5)

    status(
        state="browser_attach_failed",
        phase="launching_browser",
        browser_mode="default_like_clone",
        cdp_url=cdp_url,
        launch_info=launch_info,
        last_error="CDP endpoint unavailable after launching Chrome",
    )
    raise RuntimeError("CDP endpoint unavailable after launching Chrome")


def get_chat_pages(context) -> List[Any]:
    pages = []
    for page in context.pages:
        try:
            if "chatgpt.com" in (page.url or ""):
                pages.append(page)
        except Exception:
            pass
    return pages


def ensure_target_page(context):
    pages = get_chat_pages(context)
    title_target = str(CFG.get("target_chat_title") or "")
    chat_url = str(CFG.get("chat_url") or "https://chatgpt.com/")
    target_base = chat_url.split("?")[0] if chat_url else ""

    # Prefer the newest matching page/window. This matters when agent1 opens a new Chrome window.
    if target_base:
        for page in reversed(pages):
            try:
                if target_base in (page.url or ""):
                    return page
            except Exception:
                pass

    if title_target:
        for page in reversed(pages):
            try:
                if title_target in (page.title() or ""):
                    return page
            except Exception:
                pass

    if pages:
        return pages[-1]

    page = context.new_page()
    page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
    return page

def normalize_chat_url_once(page) -> None:
    # No loop. This prevents the repeated refresh problem.
    if URL_NORMALIZED_ONCE[0]:
        return
    URL_NORMALIZED_ONCE[0] = True
    try:
        u = page.url or ""
        if "mweb_fallback=1" in u and "chatgpt.com/c/" in u:
            base = u.split("?")[0]
            log_line("agent1.log", "normalize_chat_url_once goto=" + base)
            page.goto(base, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
    except Exception as exc:
        log_line("agent1.log", "normalize_chat_url_once_failed: " + str(exc))


def apply_browser_layout_fix(page, force: bool = False) -> None:
    # DEFAULT_CHROME_NO_LAYOUT_FORCE_MARKER
    # Keep the default Chrome/ChatGPT zoom and layout. Only remove mweb_fallback and old injected CSS.
    try:
        now = utc_ts()
        if not force and now - LAST_LAYOUT_FIX_TS[0] < 120:
            return
        LAST_LAYOUT_FIX_TS[0] = now

        result = page.evaluate("""
        () => {
          const before = {
            url: location.href,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio,
            visualScale: window.visualViewport ? window.visualViewport.scale : null
          };
          let removedFallback = false;
          try {
            const u = new URL(location.href);
            if (u.searchParams.has('mweb_fallback')) {
              u.searchParams.delete('mweb_fallback');
              history.replaceState(null, '', u.toString());
              removedFallback = true;
            }
          } catch(e) {}
          const oldStyle = document.getElementById('chatgpt-local-agent-layout-css');
          if (oldStyle) oldStyle.remove();
          try { document.documentElement.style.zoom = ''; } catch(e) {}
          try { document.body.style.zoom = ''; } catch(e) {}
          const after = {
            url: location.href,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio,
            visualScale: window.visualViewport ? window.visualViewport.scale : null
          };
          return {before, after, removedFallback, removedOldAgentCss: !!oldStyle};
        }
        """)
        log_line("agent1.log", "default_chrome_no_layout_force " + json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        log_line("agent1.log", "default_chrome_no_layout_force_failed: " + str(exc))

def get_assistant_messages(page) -> List[Dict[str, Any]]:
    js = """
    () => {
      const nodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
      return nodes.map((n, i) => ({
        index: i,
        text: n.innerText || n.textContent || "",
        html: n.outerHTML ? n.outerHTML.slice(0, 1000) : ""
      }));
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return []


def get_assistant_messages_after_latest_user(page) -> Dict[str, Any]:
    js = """
    () => {
      const nodes = Array.from(document.querySelectorAll('[data-message-author-role]'));
      const all = nodes.map((n, i) => ({
        dom_index: i,
        role: n.getAttribute('data-message-author-role') || '',
        text: n.innerText || n.textContent || "",
        html: n.outerHTML ? n.outerHTML.slice(0, 1000) : ""
      }));
      let latestUserIndex = -1;
      for (const item of all) {
        if (item.role === 'user') latestUserIndex = item.dom_index;
      }
      const assistants = all
        .filter((item) => item.role === 'assistant' && item.dom_index > latestUserIndex)
        .map((item, i) => ({
          index: i,
          dom_index: item.dom_index,
          text: item.text,
          html: item.html
        }));
      return {
        total_messages: all.length,
        user_messages: all.filter((item) => item.role === 'user').length,
        assistant_messages_total: all.filter((item) => item.role === 'assistant').length,
        latest_user_dom_index: latestUserIndex,
        assistants_after_latest_user: assistants
      };
    }
    """
    try:
        data = page.evaluate(js)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "total_messages": 0,
        "user_messages": 0,
        "assistant_messages_total": 0,
        "latest_user_dom_index": -1,
        "assistants_after_latest_user": [],
    }


def get_latest_assistant_v2_candidate(page) -> Dict[str, Any]:
    js = """
    () => {
      const marker = 'LOCAL_AGENT_V2';
      const nodes = Array.from(document.querySelectorAll('[data-message-author-role]'));
      const all = nodes.map((n, i) => ({
        node: n,
        dom_index: i,
        role: n.getAttribute('data-message-author-role') || ''
      }));

      let latestUserIndex = -1;
      for (const item of all) {
        if (item.role === 'user') latestUserIndex = item.dom_index;
      }

      const assistants = all.filter((item) => item.role === 'assistant' && item.dom_index > latestUserIndex);

      if (!assistants.length) {
        return {
          found: false,
          latest_user_dom_index: latestUserIndex,
          assistant_messages_after_latest_user: 0,
          selected_assistant_offset_from_end: null,
          skipped_newer_assistant_messages: 0,
          code_block_count: 0,
          code_blocks: [],
          outside_text: '',
          no_v2_code_block_after_latest_user: true
        };
      }

      let selected = null;
      let selectedCodeBlocks = [];
      let selectedOffset = null;

      for (let idx = assistants.length - 1; idx >= 0; idx--) {
        const item = assistants[idx];
        const msg = item.node;
        const codeNodes = Array.from(msg.querySelectorAll('pre code'));
        const codeBlocks = codeNodes.map((c) => c.innerText || c.textContent || '');
        const hasV2 = codeBlocks.some((code) => {
          const first = String(code || '').split(/\\r?\\n/, 1)[0].trim();
          return first === marker;
        });
        if (hasV2) {
          selected = item;
          selectedCodeBlocks = codeBlocks;
          selectedOffset = assistants.length - 1 - idx;
          break;
        }
      }

      if (!selected) {
        const last = assistants[assistants.length - 1];
        const lastMsg = last.node;
        const lastCodes = Array.from(lastMsg.querySelectorAll('pre code')).map((c) => c.innerText || c.textContent || '');
        return {
          found: false,
          latest_user_dom_index: latestUserIndex,
          assistant_messages_after_latest_user: assistants.length,
          dom_index: last.dom_index,
          selected_assistant_offset_from_end: null,
          skipped_newer_assistant_messages: 0,
          code_block_count: lastCodes.length,
          code_blocks: lastCodes,
          outside_text: '',
          text_length: (lastMsg.innerText || lastMsg.textContent || '').length,
          no_v2_code_block_after_latest_user: true
        };
      }

      const msg = selected.node;
      const clone = msg.cloneNode(true);
      clone.querySelectorAll('pre').forEach((n) => n.remove());
      clone.querySelectorAll('button, svg, [role="button"], [aria-hidden="true"]').forEach((n) => n.remove());
      const outsideText = (clone.innerText || clone.textContent || '').trim();

      return {
        found: true,
        latest_user_dom_index: latestUserIndex,
        assistant_messages_after_latest_user: assistants.length,
        dom_index: selected.dom_index,
        selected_assistant_offset_from_end: selectedOffset,
        skipped_newer_assistant_messages: selectedOffset,
        code_block_count: selectedCodeBlocks.length,
        code_blocks: selectedCodeBlocks,
        outside_text: outsideText,
        text_length: (msg.innerText || msg.textContent || '').length,
        no_v2_code_block_after_latest_user: false
      };
    }
    """
    try:
        data = page.evaluate(js)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        return {
            "found": False,
            "error": type(exc).__name__ + ": " + str(exc),
            "latest_user_dom_index": -1,
            "assistant_messages_after_latest_user": 0,
            "selected_assistant_offset_from_end": None,
            "skipped_newer_assistant_messages": 0,
            "code_block_count": 0,
            "code_blocks": [],
            "outside_text": "",
            "no_v2_code_block_after_latest_user": True,
        }
    return {
        "found": False,
        "latest_user_dom_index": -1,
        "assistant_messages_after_latest_user": 0,
        "selected_assistant_offset_from_end": None,
        "skipped_newer_assistant_messages": 0,
        "code_block_count": 0,
        "code_blocks": [],
        "outside_text": "",
        "no_v2_code_block_after_latest_user": True,
    }



def get_page_diag(page) -> Dict[str, Any]:
    try:
        return {
            "title": page.title(),
            "url": page.url,
            "layout": page.evaluate("""
            () => ({
              innerWidth: window.innerWidth,
              innerHeight: window.innerHeight,
              devicePixelRatio: window.devicePixelRatio,
              visualScale: window.visualViewport ? window.visualViewport.scale : null
            })
            """)
        }
    except Exception as exc:
        return {"title": "", "url": "", "error": str(exc)}


def focus_composer(page) -> Dict[str, Any]:
    selectors = [
        "#prompt-textarea",
        "textarea",
        "div[contenteditable='true']",
        ".ProseMirror",
        "[data-testid='composer-root'] [contenteditable='true']",
    ]
    tried = []
    for sel in selectors:
        tried.append(sel)
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count <= 0:
                continue
            target = loc.nth(count - 1)
            target.scroll_into_view_if_needed(timeout=1500)
            target.focus(timeout=1500)
            return {"ok": True, "method": "locator", "selector": sel, "tried": tried}
        except Exception:
            continue
    try:
        res = page.evaluate("""
        () => {
          const el =
            document.querySelector('#prompt-textarea') ||
            document.querySelector('textarea') ||
            document.querySelector('div[contenteditable="true"]') ||
            document.querySelector('.ProseMirror');
          if (!el) return {ok:false, reason:'composer not found'};
          el.focus();
          const r = el.getBoundingClientRect();
          return {ok:true, method:'js-focus', tag:el.tagName, rect:{left:r.left, top:r.top, width:r.width, height:r.height}};
        }
        """)
        if res and res.get("ok"):
            return res
        return {"ok": False, "tried": tried, "js": res}
    except Exception as exc:
        return {"ok": False, "tried": tried, "error": str(exc)}


def composer_text_length(page) -> Dict[str, Any]:
    try:
        return page.evaluate("""
        () => {
          const el =
            document.querySelector('#prompt-textarea') ||
            document.querySelector('textarea') ||
            document.querySelector('div[contenteditable="true"]') ||
            document.querySelector('.ProseMirror');
          if (!el) return {found:false, length:-1, text:''};
          const text = (el.value !== undefined ? el.value : (el.innerText || el.textContent || ''));
          return {found:true, length:text.length, text:text.slice(0,120)};
        }
        """)
    except Exception as exc:
        return {"found": False, "length": -1, "error": str(exc)}


def clear_composer_if_needed(page) -> Dict[str, Any]:
    state = composer_text_length(page)
    if not state.get("found") or int(state.get("length") or 0) <= 0:
        return {"cleared": False, "before": state}
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)
        return {"cleared": True, "before": state, "after": composer_text_length(page)}
    except Exception as exc:
        return {"cleared": False, "before": state, "error": str(exc)}




def stop_generation_if_needed(page) -> Dict[str, Any]:
    # V9_3_STOP_GENERATION_BEFORE_SEND
    js_count = """
    () => {
      const buttons = Array.from(document.querySelectorAll('button'));
      const hits = [];
      for (const b of buttons) {
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const s = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '') + ' ' + (b.textContent || ''));
        const l = s.toLowerCase();
        if (l.includes('stop') || l.includes('cancel') || s.includes('停止') || s.includes('中止')) {
          hits.push({label:s.slice(0,120), x:r.x, y:r.y, w:r.width, h:r.height});
        }
      }
      return hits;
    }
    """
    js_click = """
    () => {
      let clicked = 0;
      const buttons = Array.from(document.querySelectorAll('button'));
      for (const b of buttons) {
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const s = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '') + ' ' + (b.textContent || ''));
        const l = s.toLowerCase();
        if (l.includes('stop') || l.includes('cancel') || s.includes('停止') || s.includes('中止')) {
          try { b.click(); clicked += 1; } catch(e) {}
        }
      }
      return clicked;
    }
    """
    try:
        initial = page.evaluate(js_count) or []
        if not initial:
            return {"ok": True, "initial_stop_count": 0, "clicked": 0}
        clicked = int(page.evaluate(js_click) or 0)
        page.wait_for_timeout(1200)
        last = []
        for _ in range(20):
            last = page.evaluate(js_count) or []
            if not last:
                break
            page.wait_for_timeout(500)
        return {"ok": True, "initial_stop_count": len(initial), "clicked": clicked, "remaining_stop_count": len(last)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:300]}


def normalize_runtime_before_send(page) -> Dict[str, Any]:
    # V9_3_RUNTIME_NORMALIZE_URL_AND_ZOOM
    try:
        return page.evaluate("""
        () => {
          let removedFallback = false;
          try {
            const u = new URL(location.href);
            if (u.searchParams.has('mweb_fallback')) {
              u.searchParams.delete('mweb_fallback');
              history.replaceState(null, '', u.toString());
              removedFallback = true;
            }
          } catch(e) {}
          return {
            url: location.href,
            removedFallback,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            scale: window.visualViewport ? window.visualViewport.scale : null,
            dpr: window.devicePixelRatio
          };
        }
        """)
    except Exception as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)[:300]}


def click_send_button(page) -> Dict[str, Any]:
    # V9_COMPOSER_VIEWPORT_SEND_BUTTON
    js = """
    () => {
      const rectObj = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {left:r.left, top:r.top, width:r.width, height:r.height, right:r.right, bottom:r.bottom};
      };
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 &&
               st.visibility !== 'hidden' && st.display !== 'none' &&
               r.bottom >= 0 && r.top <= window.innerHeight &&
               r.right >= 0 && r.left <= window.innerWidth;
      };
      const disabled = (el) => {
        return !!(el.disabled || el.getAttribute('disabled') !== null || el.getAttribute('aria-disabled') === 'true');
      };
      const composer =
        document.querySelector('#prompt-textarea') ||
        document.querySelector('textarea') ||
        document.querySelector('div[contenteditable="true"]') ||
        document.querySelector('.ProseMirror');

      let root = null;
      if (composer) {
        root = composer.closest('[data-testid="composer-root"]') || composer.closest('form') || composer.parentElement;
      }
      if (!root) root = document.querySelector('[data-testid="composer-root"]') || document.querySelector('form') || document;

      const candidates = [];
      const selectors = [
        'button[data-testid="send-button"]',
        'button[data-testid="composer-send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send message"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
        'button[type="submit"]',
        'button'
      ];

      const seen = new Set();
      for (const sel of selectors) {
        for (const b of Array.from(root.querySelectorAll(sel))) {
          if (seen.has(b)) continue;
          seen.add(b);
          if (!visible(b) || disabled(b)) continue;
          const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '') + ' ' + (b.textContent || '')).toLowerCase();
          const likely = label.includes('send') || label.includes('发送') || (b.getAttribute('type') || '').toLowerCase() === 'submit';
          if (!likely) continue;
          let score = 0;
          if (label.includes('send')) score += 100;
          if (label.includes('发送')) score += 100;
          if (label.includes('prompt')) score += 40;
          if (label.includes('composer')) score += 40;
          if ((b.getAttribute('type') || '').toLowerCase() === 'submit') score += 35;
          if ((b.getAttribute('data-testid') || '').toLowerCase().includes('send')) score += 100;
          if (composer) {
            const br = b.getBoundingClientRect();
            const cr = composer.getBoundingClientRect();
            const dy = Math.abs((br.top + br.bottom)/2 - (cr.top + cr.bottom)/2);
            if (dy < 200) score += 80;
          }
          candidates.push({button:b, selector:sel, score});
        }
      }

      candidates.sort((a,b) => b.score - a.score);
      if (!candidates.length) {
        return {
          clicked:false,
          reason:'no visible enabled send button in composer/viewport',
          button_count:document.querySelectorAll('button').length,
          composerRect: rectObj(composer),
          rootRect: rectObj(root),
          viewport:{width:window.innerWidth,height:window.innerHeight}
        };
      }

      const item = candidates[0];
      const b = item.button;
      const r = b.getBoundingClientRect();
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
        try { b.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, view:window})); } catch (e) {}
      }
      try { b.click(); } catch (e) {}
      return {
        clicked:true,
        selector:item.selector,
        score:item.score,
        label:(b.getAttribute('aria-label') || b.textContent || '').slice(0,120),
        testid:b.getAttribute('data-testid') || '',
        rect:{left:r.left, top:r.top, width:r.width, height:r.height, right:r.right, bottom:r.bottom},
        viewport:{width:window.innerWidth,height:window.innerHeight},
        composerRect: rectObj(composer),
        rootRect: rectObj(root)
      };
    }
    """
    try:
        return page.evaluate(js)
    except Exception as exc:
        return {"clicked": False, "error": str(exc)}


def _send_message_unlocked(page, message: str) -> Dict[str, Any]:
    message = truncate(message, int(CFG.get("max_message_chars", 12000)))
    atomic_write_text(AGENT1_OUTBOX, message, encoding="utf-8")

    attempts: List[Dict[str, Any]] = []
    last_error: Optional[str] = None
    preflight: Dict[str, Any] = {}

    try:
        page.bring_to_front()
    except Exception:
        pass
    page.wait_for_timeout(200)

    try:
        preflight = {
            "stop": stop_generation_if_needed(page),
            "runtime": normalize_runtime_before_send(page),
        }
    except Exception as exc:
        preflight = {"error": type(exc).__name__ + ": " + str(exc)}

    for attempt in range(1, max(1, SEND_MESSAGE_RETRY_COUNT) + 1):
        try:
            status(
                state="sending_observation",
                phase="sending_observation",
                send_phase="focus_composer",
                send_attempt=attempt,
                send_button_found=None,
                send_button_enabled=None,
                composer_text_len=None,
                send_last_error=last_error,
            )

            focus = focus_composer(page)
            if not focus.get("ok"):
                last_error = "composer focus failed: " + json.dumps(focus, ensure_ascii=False)[:1000]
                attempts.append({"attempt": attempt, "stage": "focus_failed", "focus": focus})
                page.wait_for_timeout(800)
                continue

            cleared = clear_composer_if_needed(page)
            page.keyboard.insert_text(message)
            page.wait_for_timeout(700)
            inserted = composer_text_length(page)
            attempts.append({"attempt": attempt, "stage": "after_insert", "composer": inserted, "cleared": cleared})

            status(
                state="sending_observation",
                phase="sending_observation",
                send_phase="waiting_send_button_enabled",
                send_attempt=attempt,
                composer_text_len=inserted.get("length"),
                send_last_error=last_error,
            )

            clicked = False
            click_result: Dict[str, Any] = {}
            start_wait = time.time()
            while time.time() - start_wait <= SEND_BUTTON_WAIT_SEC:
                click_result = click_send_button(page)
                attempts.append({"attempt": attempt, "stage": "dom_click_try", "result": click_result})
                if click_result.get("clicked"):
                    clicked = True
                    status(
                        state="sending_observation",
                        phase="sending_observation",
                        send_phase="clicked_send_button",
                        send_attempt=attempt,
                        send_button_found=True,
                        send_button_enabled=True,
                        composer_text_len=(composer_text_length(page) or {}).get("length"),
                    )
                    break

                last_error = str(click_result.get("reason") or click_result.get("error") or "send button not ready")
                status(
                    state="sending_observation",
                    phase="sending_observation",
                    send_phase="waiting_send_button_enabled",
                    send_attempt=attempt,
                    send_button_found=False if "no visible" in last_error else None,
                    send_button_enabled=False,
                    composer_text_len=(composer_text_length(page) or {}).get("length"),
                    send_last_error=last_error,
                )
                page.wait_for_timeout(500)

            if clicked:
                page.wait_for_timeout(1800)
                after_click = composer_text_length(page)
                attempts.append({"attempt": attempt, "stage": "after_dom_click", "composer": after_click})
                if after_click.get("found") and int(after_click.get("length") or 0) == 0:
                    return {"sent": True, "method": "dom_click", "attempt": attempt, "preflight": preflight, "attempts": attempts, "chars": len(message)}

            for key_name in ("Enter", "Control+Enter"):
                status(
                    state="sending_observation",
                    phase="sending_observation",
                    send_phase="keyboard_fallback_" + key_name.lower().replace("+", "_"),
                    send_attempt=attempt,
                    composer_text_len=(composer_text_length(page) or {}).get("length"),
                    send_last_error=last_error,
                )
                page.keyboard.press(key_name)
                page.wait_for_timeout(1800)
                after_key = composer_text_length(page)
                attempts.append({"attempt": attempt, "stage": "after_" + key_name, "composer": after_key})
                if after_key.get("found") and int(after_key.get("length") or 0) == 0:
                    return {"sent": True, "method": key_name, "attempt": attempt, "preflight": preflight, "attempts": attempts, "chars": len(message)}

            last_error = "message inserted but composer did not clear"
            status(
                state="send_retry_wait",
                phase="sending_observation",
                send_phase="retry_wait",
                send_attempt=attempt,
                composer_text_len=(composer_text_length(page) or {}).get("length"),
                send_last_error=last_error,
            )
            page.wait_for_timeout(1000)

        except Exception as exc:
            last_error = type(exc).__name__ + ": " + str(exc)
            attempts.append({"attempt": attempt, "stage": "exception", "error": last_error})
            status(
                state="send_retry_wait",
                phase="sending_observation",
                send_phase="exception_retry_wait",
                send_attempt=attempt,
                send_last_error=last_error,
            )
            page.wait_for_timeout(1000)

    status(
        state="send_failed",
        phase="sending_observation",
        send_phase="failed",
        send_attempt=SEND_MESSAGE_RETRY_COUNT,
        composer_text_len=(composer_text_length(page) or {}).get("length"),
        send_last_error=last_error,
    )
    raise RuntimeError("message inserted but not submitted; send attempts=" + json.dumps(attempts[-20:], ensure_ascii=False)[:5000])



def send_message(page, message: str) -> Dict[str, Any]:
    token = acquire_chat_send_lock("agent1", "send_message", "", ttl_sec=90, wait_sec=20.0)
    if not token:
        raise RuntimeError("chat send lock busy; agent1 did not send")
    try:
        return _send_message_unlocked(page, message)
    finally:
        release_chat_send_lock(token)


def flush_pending_outbox(page) -> None:
    try:
        PENDING_OUTBOX.mkdir(parents=True, exist_ok=True)
        PENDING_SENT.mkdir(parents=True, exist_ok=True)
        items = sorted(PENDING_OUTBOX.glob("*.txt"))
        for f in items[:3]:
            try:
                msg = f.read_text(encoding="utf-8", errors="replace")
                if not msg.strip():
                    f.unlink(missing_ok=True)
                    continue
                send_message(page, msg)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                dest = PENDING_SENT / (f.name + "." + stamp + ".sent")
                meta = f.with_suffix(f.suffix + ".json")
                try:
                    f.replace(dest)
                except Exception:
                    f.unlink(missing_ok=True)
                if meta.exists():
                    try:
                        meta.replace(PENDING_SENT / (meta.name + "." + stamp + ".sent"))
                    except Exception:
                        meta.unlink(missing_ok=True)
                log_line("agent1.log", "pending_outbox_sent " + f.name)
                time.sleep(1.0)
            except Exception as exc:
                log_line("agent1.log", "pending_outbox_send_failed " + f.name + ": " + str(exc))
                break
    except Exception as exc:
        log_line("agent1.log", "flush_pending_outbox_failed: " + str(exc))


def send_parse_error(page, raw: str, err: str) -> None:
    raw_snip = truncate(raw, int(CFG.get("max_raw_block_chars", 1800)))
    msg = (
        "[AGENT1 PARSE ERROR]\n"
        "The LOCAL_AGENT block is not valid JSON. Common cause: an unescaped double quote inside the command string.\n\n"
        "For PowerShell strings inside JSON, prefer single quotes:\n"
        "  \"command\": \"Write-Output 'TEXT'\"\n"
        "Do not write:\n"
        "  \"command\": \"Write-Output \"TEXT\"\"\n\n"
        "Raw block read by agent1:\n"
        "```text\n" + raw_snip + "\n```\n\n"
        "Parse error:\n"
        "```text\n" + truncate(err, 1000) + "\n```"
    )
    send_message(page, msg)


def send_v2_parse_error(page, raw: str, err: str) -> None:
    raw_snip = truncate(raw, int(CFG.get("max_raw_block_chars", 1800)))
    msg = (
        "[AGENT1 LOCAL_AGENT_V2 REJECTED]\n"
        "The latest assistant message contains a LOCAL_AGENT_V2 code block, but agent1 refused to execute it.\n\n"
        "Reason:\n"
        "```text\n" + truncate(err, 1000) + "\n```\n\n"
        "Code block read by agent1:\n"
        "```text\n" + raw_snip + "\n```"
    )
    send_message(page, msg)


def safe_action_write_file(action: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(str(action["path"]))
    content = str(action.get("content", ""))
    backup = None
    if path.exists():
        backup = str(path) + ".bak_" + time.strftime("%Y%m%d_%H%M%S")
        Path(backup).write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return {"path": str(path), "bytes_written": len(content.encode("utf-8")), "backup_path": backup}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cleanup_tmp_scripts(tmp: Path) -> None:
    try:
        scripts = sorted(
            [p for p in tmp.glob("*.ps1") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in scripts[SCRIPT_KEEP_COUNT:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _match_text(haystack: str, needle: str) -> bool:
    if not needle:
        return True
    return needle.lower() in str(haystack or "").lower()


def select_download_pages(context, default_page, action: Dict[str, Any]) -> List[Any]:
    pages = get_chat_pages(context) if context is not None else []
    if default_page is not None and default_page not in pages:
        pages.append(default_page)

    url_need = str(action.get("page_url_contains") or "")
    title_need = str(action.get("page_title_contains") or "")
    if not url_need and not title_need:
        return [default_page] if default_page is not None else pages

    out = []
    for p in pages:
        try:
            url_ok = _match_text(p.url or "", url_need)
            title_ok = _match_text(p.title() or "", title_need)
            if url_ok and title_ok:
                out.append(p)
        except Exception:
            pass
    return out or ([default_page] if default_page is not None else pages)


def _locator_text(loc, index: int) -> str:
    try:
        return loc.nth(index).inner_text(timeout=700) or ""
    except Exception:
        return ""


def _locator_attr(loc, index: int, attr: str) -> str:
    try:
        return loc.nth(index).get_attribute(attr, timeout=700) or ""
    except Exception:
        return ""


def _download_candidate_matches(text: str, href: str, download_name: str, action: Dict[str, Any]) -> bool:
    filename_need = str(action.get("filename_contains") or action.get("name_contains") or "")
    text_need = str(action.get("text_contains") or "")
    href_need = str(action.get("href_contains") or "")

    combined_name = f"{download_name} {href} {text}"
    if filename_need and not _match_text(combined_name, filename_need):
        return False
    if text_need and not _match_text(text, text_need):
        return False
    if href_need and not _match_text(href, href_need):
        return False
    if filename_need or text_need or href_need:
        return True

    # Default to update-looking archives when no explicit selector is supplied.
    low = combined_name.lower()
    return ".zip" in low or "update" in low


def action_download_chatgpt_file(action: Dict[str, Any], context=None, default_page=None) -> Dict[str, Any]:
    if context is None and default_page is None:
        raise RuntimeError("download_chatgpt_file requires an active ChatGPT page")

    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    timeout_sec = max(5.0, float(action.get("timeout", action.get("timeout_sec", 120))))
    deadline = time.time() + timeout_sec
    selectors = action.get("selectors") or ["a", "button", '[role="button"]']
    if isinstance(selectors, str):
        selectors = [selectors]

    attempts = []
    pages = select_download_pages(context, default_page, action)
    for page in pages:
        try:
            page.bring_to_front()
        except Exception:
            pass

        for selector in selectors:
            try:
                loc = page.locator(str(selector))
                count = min(int(loc.count()), int(action.get("max_candidates", 80)))
            except Exception as exc:
                attempts.append({"selector": selector, "error": str(exc)})
                continue

            for i in range(count):
                if time.time() >= deadline:
                    raise TimeoutError("download_chatgpt_file timed out")
                text = _locator_text(loc, i)
                href = _locator_attr(loc, i, "href")
                download_name = _locator_attr(loc, i, "download")
                label = _locator_attr(loc, i, "aria-label")
                visible_text = " ".join(x for x in [text, label] if x)
                if not _download_candidate_matches(visible_text, href, download_name, action):
                    continue

                candidate = {
                    "page_url": getattr(page, "url", ""),
                    "selector": str(selector),
                    "index": i,
                    "text": truncate(visible_text, 240),
                    "href": truncate(href, 300),
                    "download": download_name,
                }
                attempts.append(candidate)
                remaining_ms = int(max(3000, min(30000, (deadline - time.time()) * 1000)))
                try:
                    with page.expect_download(timeout=remaining_ms) as dl_info:
                        loc.nth(i).click(timeout=5000, force=True)
                    download = dl_info.value
                    dest_raw = str(action.get("dest") or action.get("path") or "")
                    if dest_raw:
                        dest = Path(dest_raw)
                        if dest.exists() and dest.is_dir():
                            dest = dest / download.suggested_filename
                    else:
                        dest = UPDATES_DIR / download.suggested_filename
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    download.save_as(str(dest))
                    actual = file_sha256(dest)
                    expected = str(action.get("sha256") or "").lower().strip()
                    if expected and actual.lower() != expected:
                        raise RuntimeError(f"SHA256 mismatch: actual={actual} expected={expected}")
                    return {
                        "status": "success",
                        "path": str(dest),
                        "suggested_filename": download.suggested_filename,
                        "sha256": actual,
                        "size_bytes": dest.stat().st_size,
                        "matched": candidate,
                        "attempts_count": len(attempts),
                    }
                except Exception as exc:
                    candidate["download_error"] = type(exc).__name__ + ": " + str(exc)
                    continue

    return {
        "status": "error",
        "error": "No matching downloadable ChatGPT file was found or clicked successfully.",
        "attempts": attempts[-20:],
    }


def action_request_update(action: Dict[str, Any]) -> Dict[str, Any]:
    root = Path(str(action.get("root") or CFG.get("root_dir") or Path(__file__).parent))
    updater = root / "tools" / "request_update.ps1"
    if not updater.exists():
        raise FileNotFoundError(str(updater))

    zip_path = str(action.get("zip_path") or action.get("path") or "")
    zip_url = str(action.get("zip_url") or action.get("url") or "")
    if not zip_path and not zip_url:
        raise ValueError("request_update requires zip_path or zip_url")

    args = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(updater),
        "-Root",
        str(root),
    ]
    if zip_path:
        args += ["-ZipPath", zip_path]
    if zip_url:
        args += ["-ZipUrl", zip_url]
    if action.get("sha256"):
        args += ["-Sha256", str(action.get("sha256"))]
    if bool(action.get("start_ui", False)):
        args += ["-StartUi"]

    timeout = int(action.get("timeout", 30))
    cp = subprocess.run(
        args,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "status": "success" if cp.returncode == 0 else "error",
        "exit_code": cp.returncode,
        "stdout": truncate(cp.stdout, 4000),
        "stderr": truncate(cp.stderr, 4000),
        "report_path": str(root / "workspace" / "v10_update_last_report.json"),
    }


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def action_apply_update_package(action: Dict[str, Any]) -> Dict[str, Any]:
    root = ROOT_DIR
    updates_dir = root / "updates"
    zip_path = Path(str(action.get("zip_path") or "")).resolve()
    if not zip_path.exists():
        raise FileNotFoundError(str(zip_path))
    if not path_is_within(zip_path, updates_dir):
        raise ValueError("update zip must be inside " + str(updates_dir))

    updater = root / "tools" / "update_manager.py"
    if not updater.exists():
        raise FileNotFoundError(str(updater))

    py = str(CFG.get("python_exe") or sys.executable)
    timeout = int(action.get("timeout", 120))
    max_output_chars = int(action.get("max_output_chars") or CFG.get("max_output_chars", 20000) or 20000)
    max_output_chars = max(1000, min(max_output_chars, 200000))
    args = [
        py,
        str(updater),
        "--root",
        str(root),
        "--zip-path",
        str(zip_path),
        "--task-id",
        sanitize_task_id(str(action.get("task_id") or "apply_update")),
    ]
    if parse_bool(action.get("dry_run"), False):
        args.append("--dry-run")

    cp = subprocess.run(
        args,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "status": "success" if cp.returncode == 0 else "error",
        "exit_code": cp.returncode,
        "stdout": truncate(cp.stdout, max_output_chars),
        "stderr": truncate(cp.stderr, max_output_chars),
        "report_path": str(root / "workspace" / "update_manager_last_report.json"),
        "dry_run": parse_bool(action.get("dry_run"), False),
    }



def decode_live_task_text_file(path: Path, limit: int) -> str:
    try:
        raw = path.read_bytes()
    except Exception:
        return ""
    if not raw:
        return ""
    data = ""
    try:
        head = raw[:1024]
        nul_count = head.count(b"\x00")
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff") or nul_count >= 8:
            try:
                data = raw.decode("utf-16", errors="replace")
            except Exception:
                data = raw.decode("utf-8", errors="replace")
        else:
            data = raw.decode("utf-8", errors="replace")
    except Exception:
        try:
            data = raw.decode("utf-8", errors="replace")
        except Exception:
            data = ""
    data = data.replace("\x00", "")
    if len(data) <= limit:
        return data
    return data[-limit:]

def run_powershell_action(action: Dict[str, Any]) -> Dict[str, Any]:
    cmd = str(action.get("command", ""))
    timeout = int(action.get("timeout", 60))
    cwd = str(action.get("cwd") or WORKSPACE)
    max_output_chars = int(action.get("max_output_chars") or CFG.get("max_output_chars", 20000) or 20000)
    max_output_chars = max(1000, min(max_output_chars, 200000))
    task_id = sanitize_task_id(str(action.get("task_id") or ("local_cmd_" + time.strftime("%Y%m%d_%H%M%S"))))
    soft_check_after_sec = int(float(action.get("soft_check_after_sec") or CFG.get("live_task_soft_check_after_sec", 0) or 0))
    heartbeat_report = bool(action.get("heartbeat_report") or soft_check_after_sec > 0)

    tmp = WORKSPACE / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    LIVE_TASK_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_TASK_HISTORY.mkdir(parents=True, exist_ok=True)
    run_stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = LIVE_TASK_HISTORY / (run_stamp + "_" + task_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    script = LIVE_TASK_DIR / "current.ps1"
    wrapper = LIVE_TASK_DIR / "visible_wrapper.ps1"
    stdout_log = LIVE_TASK_DIR / "stdout.log"
    stderr_log = LIVE_TASK_DIR / "stderr.log"
    transcript_log = LIVE_TASK_DIR / "transcript.log"
    heartbeat_json = LIVE_TASK_DIR / "heartbeat.json"
    current_json = LIVE_TASK_DIR / "current.json"
    delay_key = "heartbeat:" + task_id

    script.write_text("$ErrorActionPreference = 'Continue'\n" + cmd, encoding="utf-8", newline="\r\n")
    for p in (stdout_log, stderr_log, transcript_log):
        try:
            p.write_text("", encoding="utf-8")
        except Exception:
            pass
    try:
        (run_dir / "current.ps1").write_text(script.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    except Exception:
        pass
    cleanup_tmp_scripts(tmp)

    def psq(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    wrapper_text = """
$ErrorActionPreference = 'Continue'
$TaskId = __TASK_ID__
$ScriptPath = __SCRIPT_PATH__
$StdoutPath = __STDOUT_PATH__
$StderrPath = __STDERR_PATH__
$TranscriptPath = __TRANSCRIPT_PATH__
$CwdPath = __CWD_PATH__
$HeartbeatPath = __HEARTBEAT_PATH__

function Write-Heartbeat([string]$State, [int]$ExitCode) {
  try {
    $obj = [ordered]@{
      task_id = $TaskId
      state = $State
      exit_code = $ExitCode
      wrapper_pid = $PID
      updated_at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
      updated_ts = [double](Get-Date -UFormat %s)
      stdout_path = $StdoutPath
      stderr_path = $StderrPath
      transcript_path = $TranscriptPath
      script_path = $ScriptPath
      cwd = $CwdPath
    }
    $obj | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $HeartbeatPath -Encoding UTF8
  } catch {}
}

Write-Heartbeat 'running' -999
try { Set-Location -LiteralPath $CwdPath } catch {}
try { Start-Transcript -Path $TranscriptPath -Force | Out-Null } catch {}
Write-Output ('VISIBLE_TASK_START task_id=' + $TaskId + ' pid=' + $PID)
Write-Output ('script=' + $ScriptPath)
Write-Output ('cwd=' + $CwdPath)
$code = 0
try {
  & powershell -NoProfile -ExecutionPolicy Bypass -File $ScriptPath 2>&1 | ForEach-Object {
    param($item)
    $line = [string]$item
    Write-Output $line
    Add-Content -LiteralPath $StdoutPath -Value $line -Encoding UTF8
  }
  $code = $LASTEXITCODE
  if ($null -eq $code) { $code = 0 }
} catch {
  $msg = 'VISIBLE_TASK_EXCEPTION ' + $_.Exception.Message
  Write-Output $msg
  Add-Content -LiteralPath $StdoutPath -Value $msg -Encoding UTF8
  Add-Content -LiteralPath $StderrPath -Value $msg -Encoding UTF8
  $code = 1
}
Write-Output ('VISIBLE_TASK_END task_id=' + $TaskId + ' exit_code=' + $code)
try { Stop-Transcript | Out-Null } catch {}
Write-Heartbeat 'finished' $code
exit $code
"""
    wrapper_text = wrapper_text.replace("__TASK_ID__", psq(task_id))
    wrapper_text = wrapper_text.replace("__SCRIPT_PATH__", psq(script))
    wrapper_text = wrapper_text.replace("__STDOUT_PATH__", psq(stdout_log))
    wrapper_text = wrapper_text.replace("__STDERR_PATH__", psq(stderr_log))
    wrapper_text = wrapper_text.replace("__TRANSCRIPT_PATH__", psq(transcript_log))
    wrapper_text = wrapper_text.replace("__CWD_PATH__", psq(cwd))
    wrapper_text = wrapper_text.replace("__HEARTBEAT_PATH__", psq(heartbeat_json))
    wrapper.write_text(wrapper_text, encoding="utf-8", newline="\r\n")

    started_ts = time.time()
    last_status_ts = 0.0
    last_output_ts = 0.0
    last_line = ""

    def parse_live_progress(text: str) -> Dict[str, Any]:
        tail = text[-2500:].replace("\r", "\n")
        info: Dict[str, Any] = {}
        percents = re.findall(r"(?<!\d)(100|[1-9]?\d)\s*%", tail)
        if percents:
            try:
                info["live_progress_percent"] = int(percents[-1])
            except Exception:
                pass
        speeds = re.findall(r"\b\d+(?:\.\d+)?\s*(?:B|KB|KiB|MB|MiB|GB|GiB)/s\b", tail, re.I)
        if speeds:
            info["live_progress_speed"] = speeds[-1]
        return info

    def write_live_state(state: str, pid: Optional[int], exit_code: Optional[int] = None, force: bool = False) -> None:
        nonlocal last_status_ts, last_output_ts, last_line
        now = time.time()
        if not force and now - last_status_ts < 1.0:
            return
        last_status_ts = now
        stdout_tail = tail_file(stdout_log, 6000)
        try:
            if stdout_log.exists():
                mtime = stdout_log.stat().st_mtime
                if mtime > last_output_ts:
                    last_output_ts = mtime
        except Exception:
            pass
        lines = [x.strip() for x in stdout_tail.replace("\r", "\n").splitlines() if x.strip()]
        if lines:
            last_line = lines[-1]
        output_age = round(now - last_output_ts, 1) if last_output_ts > 0 else None
        extra = parse_live_progress(stdout_tail)
        data = {
            "task_id": task_id,
            "state": state,
            "agent1_pid": os.getpid(),
            "powershell_pid": pid,
            "exit_code": exit_code,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_ts)),
            "started_ts": started_ts,
            "updated_at": now_iso(),
            "updated_ts": now,
            "elapsed_sec": round(now - started_ts, 1),
            "timeout_sec": timeout,
            "soft_check_after_sec": soft_check_after_sec,
            "heartbeat_report": heartbeat_report,
            "next_heartbeat_at": (started_ts + soft_check_after_sec if heartbeat_report and soft_check_after_sec > 0 else None),
            "visible_ui": True,
            "cwd": cwd,
            "current_script_path": str(script),
            "wrapper_script_path": str(wrapper),
            "stdout_path": str(stdout_log),
            "stderr_path": str(stderr_log),
            "transcript_path": str(transcript_log),
            "history_dir": str(run_dir),
            "last_output_age_sec": output_age,
            "last_output_line": truncate(last_line, 1000),
            "stdout_tail": truncate(stdout_tail, 6000),
        }
        data.update(extra)
        atomic_write_json(current_json, data)
        atomic_write_json(heartbeat_json, data)
        status(
            state="executing" if state == "running" else state,
            current_action_type="run_powershell",
            current_command=truncate(cmd, 800),
            current_cwd=cwd,
            current_script_path=str(script),
            live_task_dir=str(LIVE_TASK_DIR),
            live_elapsed_sec=data["elapsed_sec"],
            live_last_output_age_sec=output_age,
            live_last_output_line=truncate(last_line, 1000),
            live_stdout_tail=truncate(stdout_tail, 6000),
            live_visible_ui=True,
            live_powershell_pid=pid,
            soft_check_after_sec=soft_check_after_sec,
            next_heartbeat_at=data["next_heartbeat_at"],
            **extra,
        )
        if heartbeat_report and soft_check_after_sec > 0 and state == "running":
            set_delayed_message(delay_key, {
                "source": "agent2",
                "target": "chatgpt",
                "type": "TASK_HEARTBEAT_REPORT",
                "task_id": task_id,
                "state": "scheduled",
                "next_trigger_ts": started_ts + soft_check_after_sec,
                "next_trigger_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_ts + soft_check_after_sec)),
                "remaining_sec": max(0, int(started_ts + soft_check_after_sec - now)),
            })

    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_CONSOLE"):
        creationflags = subprocess.CREATE_NEW_CONSOLE
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        write_live_state("running", proc.pid, force=True)
        while True:
            write_live_state("running", proc.pid)
            if proc.poll() is not None:
                break
            if timeout > 0 and time.time() - started_ts > timeout:
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                stdout = tail_file(stdout_log, max_output_chars)
                write_live_state("timeout", proc.pid, exit_code=124, force=True)
                clear_delayed_message(delay_key)
                return {
                    "status": "error",
                    "exit_code": 124,
                    "stdout": truncate(stdout, max_output_chars),
                    "stderr": "timeout after " + str(timeout) + " seconds",
                    "script_path": str(script),
                    "wrapper_path": str(wrapper),
                    "live_task_dir": str(LIVE_TASK_DIR),
                    "visible_ui": True,
                }
            time.sleep(0.25)
        rc = proc.wait()
        stdout = tail_file(stdout_log, max_output_chars)
        stderr = tail_file(stderr_log, max_output_chars)
        write_live_state("finished", proc.pid, exit_code=rc, force=True)
        clear_delayed_message(delay_key)
        try:
            for src, name in ((script, "current.ps1"), (wrapper, "visible_wrapper.ps1"), (stdout_log, "stdout.log"), (stderr_log, "stderr.log"), (transcript_log, "transcript.log"), (heartbeat_json, "heartbeat.json"), (current_json, "current.json")):
                if src.exists():
                    (run_dir / name).write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except Exception:
            pass
        return {
            "status": "success" if rc == 0 else "error",
            "exit_code": rc,
            "stdout": truncate(stdout, max_output_chars),
            "stderr": truncate(stderr, max_output_chars),
            "script_path": str(script),
            "wrapper_path": str(wrapper),
            "live_task_dir": str(LIVE_TASK_DIR),
            "history_dir": str(run_dir),
            "visible_ui": True,
        }
    except Exception as exc:
        clear_delayed_message(delay_key)
        return {
            "status": "error",
            "exit_code": 125,
            "stdout": truncate(tail_file(stdout_log, max_output_chars), max_output_chars),
            "stderr": str(exc),
            "script_path": str(script),
            "live_task_dir": str(LIVE_TASK_DIR),
            "visible_ui": True,
        }


def execute_action(action: Dict[str, Any], page=None, context=None) -> Dict[str, Any]:
    t = str(action.get("type", ""))
    if t == "sleep":
        sec = float(action.get("seconds", 1))
        time.sleep(sec)
        return {"slept_sec": sec}
    if t == "run_powershell":
        return run_powershell_action(action)
    if t == "read_file":
        p = Path(str(action["path"]))
        max_chars = int(action.get("max_chars", 8000))
        return {"path": str(p), "content": truncate(p.read_text(encoding="utf-8", errors="replace"), max_chars)}
    if t == "write_file":
        return safe_action_write_file(action)
    if t == "list_dir":
        p = Path(str(action.get("path") or WORKSPACE))
        return {"path": str(p), "items": [x.name for x in p.iterdir()][:200]}
    if t == "download_chatgpt_file":
        return action_download_chatgpt_file(action, context=context, default_page=page)
    if t == "request_update":
        return action_request_update(action)
    if t == "apply_update_package":
        return action_apply_update_package(action)
    raise ValueError("unknown action type: " + t)


def execute_command(cmd: Dict[str, Any], page=None, context=None) -> Dict[str, Any]:
    result = {
        "task_id": cmd.get("task_id"),
        "started_at": now_iso(),
        "results": [],
        "overall_status": "success",
    }
    for i, action in enumerate(cmd.get("actions", [])):
        item = {"index": i, "type": action.get("type"), "status": "success", "result": None, "error": None}
        try:
            item["result"] = execute_action(action, page=page, context=context)
            if isinstance(item["result"], dict) and item["result"].get("status") == "error":
                item["status"] = "error"
                result["overall_status"] = "partial_error"
                if not action.get("continue_on_error"):
                    result["results"].append(item)
                    break
        except Exception as exc:
            item["status"] = "error"
            item["error"] = type(exc).__name__ + ": " + str(exc)
            result["overall_status"] = "partial_error"
            if not action.get("continue_on_error"):
                result["results"].append(item)
                break
        result["results"].append(item)
    result["finished_at"] = now_iso()
    return result


def send_observation(page, result: Dict[str, Any]) -> None:
    # V9_3_COMPACT_OBSERVATION
    full = json.dumps(result, ensure_ascii=False, indent=2)
    full_path = WORKSPACE / "last_observation_full.json"
    try:
        atomic_write_text(full_path, full, encoding="utf-8")
    except Exception:
        pass
    if len(full) > 5500:
        compact = {
            "task_id": result.get("task_id"),
            "overall_status": result.get("overall_status"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "results_count": len(result.get("results", [])) if isinstance(result.get("results"), list) else None,
            "full_result_path": str(full_path),
            "note": "Full observation saved locally. Compact summary shown to avoid ChatGPT composer/send failure.",
            "results": [],
        }
        for item in (result.get("results", []) or [])[:8]:
            brief = {
                "index": item.get("index"),
                "type": item.get("type"),
                "status": item.get("status"),
                "error": item.get("error"),
            }
            r = item.get("result")
            if isinstance(r, dict):
                brief["result_keys"] = list(r.keys())[:20]
                if "status" in r:
                    brief["result_status"] = r.get("status")
                if "exit_code" in r:
                    brief["exit_code"] = r.get("exit_code")
                if "stdout" in r:
                    brief["stdout_tail"] = str(r.get("stdout") or "")[-1200:]
                if "stderr" in r:
                    brief["stderr_tail"] = str(r.get("stderr") or "")[-1200:]
                if "script_path" in r:
                    brief["script_path"] = r.get("script_path")
            else:
                brief["result"] = truncate(r, 1200)
            compact["results"].append(brief)
        body = json.dumps(compact, ensure_ascii=False, indent=2)
    else:
        body = full
    msg = "[OBSERVATION]\nlocal_time: " + now_iso() + "\nresult:\n" + body + "\n[/OBSERVATION]"
    try:
        send_message(page, msg)
    except Exception:
        queue_pending_outbox_message(str(result.get("task_id") or "observation"), msg, reason="send_observation_failed")
        raise


def start_agent2_if_needed(last_start: List[float]) -> None:
    if not CFG.get("enable_agent1_restart_agent2", True):
        return
    procs = find_processes("agent2.py", exclude_keywords=["agent1.py", "py_compile"])
    st = process_age_status(AGENT2_STATUS)
    stale = (not st["exists"]) or (st["age_sec"] is not None and st["age_sec"] > float(CFG.get("agent2_status_stale_sec", 25)))

    if procs:
        if stale:
            log_line("agent1.log", "agent2 status stale but agent2 process exists; skip duplicate start")
        return

    if utc_ts() - last_start[0] < float(CFG.get("restart_cooldown_sec", 30)):
        return
    last_start[0] = utc_ts()
    try:
        pid = start_detached_python(str(CFG["agent2_script"]), cwd=str(WORKSPACE))
        log_line("agent1.log", f"started agent2 pid={pid} reason=missing_process")
    except Exception as exc:
        log_line("agent1.log", f"failed to start agent2: {exc}")


def prepare_page(context):
    page = ensure_target_page(context)
    normalize_chat_url_once(page)
    apply_browser_layout_fix(page, force=not LAYOUT_FIX_DONE_ONCE[0])
    LAYOUT_FIX_DONE_ONCE[0] = True
    flush_pending_outbox(page)
    return page


def is_browser_context_closed_error(exc: BaseException) -> bool:
    s = (type(exc).__name__ + ": " + str(exc)).lower()
    needles = [
        "targetclosederror",
        "browsercontext.new_page",
        "target page, context or browser has been closed",
        "browser has been closed",
        "context has been closed",
        "page has been closed",
    ]
    return any(x in s for x in needles)


def reset_browser_session_flags() -> None:
    URL_NORMALIZED_ONCE[0] = False
    LAYOUT_FIX_DONE_ONCE[0] = False
    LAST_LAYOUT_FIX_TS[0] = 0.0


def control_requests_stop_agent1() -> Optional[Dict[str, Any]]:
    control = read_json(AGENT_CONTROL, default={}) or {}
    if control.get("stop_all") or control.get("stop_agent1"):
        return control
    return None


def mark_visible_blocks_processed(page, processed: set[str]) -> int:
    messages = get_assistant_messages(page)
    recent = messages[-int(CFG.get("recent_assistant_count", 8)):]
    added = 0
    for msg in recent:
        for raw in extract_blocks(msg.get("text", "")):
            raw_hash = sha12(raw)
            block_key = "block_hash:" + raw_hash
            if block_key not in processed:
                processed.add(block_key)
                added += 1
            cmd, _err = parse_command_block(raw)
            if cmd is not None:
                key = command_key(cmd, raw_hash)
                if key not in processed:
                    processed.add(key)
                    added += 1
    if added:
        save_processed(processed)
        log_line("agent1.log", f"startup_ignored_visible_blocks added_keys={added}")
    return added


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    AGENT1_PID.write_text(str(os.getpid()), encoding="utf-8")
    processed = load_processed()
    last_agent2_start = [0.0]
    status(state="starting", phase="starting", processed_count=len(processed))

    with sync_playwright() as pw:
        status(state="starting", phase="launching_browser", processed_count=len(processed))
        context = launch_context(pw)
        status(state="starting", phase="waiting_for_page", processed_count=len(processed))
        page = prepare_page(context)
        if ENABLE_LOCAL_AGENT_V1:
            ignored = mark_visible_blocks_processed(page, processed)
            if ignored:
                status(state="startup_ignored_visible_blocks", phase="parsing_command", ignored_keys=ignored, processed_count=len(processed))
        last_wait_log = 0.0
        browser_recover_failures = 0
        last_v2_hash = [""]
        last_v2_seen_ts = [0.0]
        pending_v2: Dict[str, Any] = {}

        while True:
            try:
                control = control_requests_stop_agent1()
                if control is not None:
                    status(state="stopped_by_control", phase="idle", control=control, processed_count=len(processed))
                    log_line("agent1.log", "stopped_by_control " + json.dumps(control, ensure_ascii=False))
                    return

                start_agent2_if_needed(last_agent2_start)
                page = prepare_page(context)
                diag = get_page_diag(page)
                browser_recover_failures = 0
                v2_scan = get_latest_assistant_v2_candidate(page)
                code_blocks = v2_scan.get("code_blocks") or []
                code_count = int(v2_scan.get("code_block_count") or 0)
                v2_code_blocks = [str(x) for x in code_blocks if code_first_line(str(x)) == LOCAL_AGENT_V2]
                v2_code_count = len(v2_code_blocks)
                latest_user_dom_index = v2_scan.get("latest_user_dom_index")

                if pending_v2 and pending_v2.get("latest_user_dom_index") != latest_user_dom_index:
                    log_line(
                        "agent1.log",
                        "pending_v2_cleared_new_user old="
                        + str(pending_v2.get("latest_user_dom_index"))
                        + " new="
                        + str(latest_user_dom_index),
                    )
                    pending_v2.clear()

                reject_v2_multi = False
                reject_v2_hash = ""
                if v2_scan.get("found") and v2_code_count > 1:
                    reject_v2_multi = True
                    reject_v2_hash = sha12(json.dumps({"dom": v2_scan.get("dom_index"), "v2_count": v2_code_count}, ensure_ascii=False))
                elif v2_scan.get("found") and v2_code_count == 1:
                    fresh = build_v2_candidate(v2_scan)
                    if fresh is not None:
                        if pending_v2.get("raw_hash") == fresh.get("raw_hash"):
                            fresh["first_seen_ts"] = pending_v2.get("first_seen_ts", fresh["first_seen_ts"])
                        pending_v2.clear()
                        pending_v2.update(fresh)

                candidate_age = None
                if pending_v2:
                    candidate_age = time.time() - float(pending_v2.get("first_seen_ts") or time.time())
                    if candidate_age > PENDING_V2_TIMEOUT_SEC:
                        log_line(
                            "agent1.log",
                            "pending_v2_cleared_timeout hash="
                            + str(pending_v2.get("raw_hash"))
                            + f" age={candidate_age:.1f}",
                        )
                        pending_v2.clear()
                        candidate_age = None

                status(
                    state="scanning",
                    phase="parsing_command",
                    page_title=diag.get("title"),
                    page_url=diag.get("url"),
                    layout=diag.get("layout"),
                    protocol=LOCAL_AGENT_V2,
                    latest_user_dom_index=latest_user_dom_index,
                    assistant_messages_after_latest_user=v2_scan.get("assistant_messages_after_latest_user"),
                    latest_assistant_dom_index=v2_scan.get("dom_index"),
                    selected_assistant_offset_from_end=v2_scan.get("selected_assistant_offset_from_end"),
                    skipped_newer_assistant_messages=v2_scan.get("skipped_newer_assistant_messages"),
                    no_v2_code_block_after_latest_user=v2_scan.get("no_v2_code_block_after_latest_user"),
                    latest_assistant_code_blocks=code_count,
                    latest_assistant_v2_code_blocks=v2_code_count,
                    pending_v2=bool(pending_v2),
                    pending_v2_hash=pending_v2.get("raw_hash") if pending_v2 else None,
                    pending_v2_age_sec=round(candidate_age, 2) if candidate_age is not None else None,
                    processed_count=len(processed),
                )

                handled = False

                if reject_v2_multi:
                    reject_key = "v2_reject_code_count:" + reject_v2_hash
                    if reject_key not in processed:
                        processed.add(reject_key)
                        save_processed(processed)
                        log_line("agent1.log", f"v2_rejected v2_code_block_count={v2_code_count} hash={reject_v2_hash}")
                    status(
                        state="v2_rejected",
                        phase="parsing_command",
                        latest_block_parse_ok=False,
                        latest_parse_error="LOCAL_AGENT_V2 requires exactly one V2 pre code block in the selected assistant message",
                        latest_assistant_code_blocks=code_count,
                        latest_assistant_v2_code_blocks=v2_code_count,
                        latest_block_hash=reject_v2_hash,
                    )
                    pending_v2.clear()
                    handled = True

                candidate = dict(pending_v2) if pending_v2 else None
                if (not handled) and candidate:
                    raw = str(candidate.get("raw") or "")
                    raw_hash = str(candidate.get("raw_hash") or sha12(raw))
                    outside_text = str(candidate.get("outside_text") or "").strip()

                    if outside_text:
                        reject_key = "v2_reject_outside_text:" + raw_hash
                        if reject_key not in processed:
                            processed.add(reject_key)
                            save_processed(processed)
                            log_line("agent1.log", f"v2_rejected outside_text hash={raw_hash} text={truncate(outside_text, 500)}")
                            if V2_PARSE_ERROR_NOTIFY:
                                try:
                                    send_v2_parse_error(page, raw, "Code block outside text is not empty. Send only the single LOCAL_AGENT_V2 code block.")
                                except Exception as exc:
                                    status(state="parse_error_notify_failed", phase="error", last_error=str(exc))
                                    log_line("agent1.log", f"v2_parse_error_notify_failed: {exc}")
                        status(
                            state="v2_rejected",
                            phase="parsing_command",
                            latest_block_parse_ok=False,
                            latest_block_hash=raw_hash,
                            latest_parse_error="code block outside text is not empty",
                            latest_raw_block_snippet=truncate(raw, int(CFG.get("max_raw_block_chars", 1800))),
                            pending_v2=True,
                        )
                        pending_v2.clear()
                        handled = True

                    elif not code_has_local_agent_v2_end_marker(raw):
                        elapsed = time.time() - float(candidate.get("first_seen_ts") or time.time())
                        state = "waiting_for_v2_end_marker"
                        parse_error = None
                        if elapsed >= V2_END_MARKER_TIMEOUT_SEC:
                            parse_error = LOCAL_AGENT_V2_END + " missing"
                            log_line("agent1.log", f"v2_waiting_end_marker_timeout hash={raw_hash} elapsed={elapsed:.1f}")
                        status(
                            state=state,
                            phase="parsing_command",
                            latest_block_hash=raw_hash,
                            latest_block_parse_ok=False,
                            latest_parse_error=parse_error,
                            waiting_elapsed_sec=round(elapsed, 2),
                            end_marker_timeout_sec=V2_END_MARKER_TIMEOUT_SEC,
                            pending_v2=True,
                            pending_v2_source_dom_index=candidate.get("source_dom_index"),
                            selected_assistant_offset_from_end=candidate.get("selected_assistant_offset_from_end"),
                            skipped_newer_assistant_messages=candidate.get("skipped_newer_assistant_messages"),
                        )
                        handled = True

                    else:
                        elapsed = time.time() - float(candidate.get("first_seen_ts") or time.time())
                        if elapsed < V2_STABLE_SEC:
                            status(
                                state="waiting_for_stable_v2_block",
                                phase="parsing_command",
                                latest_block_hash=raw_hash,
                                stable_elapsed_sec=round(elapsed, 2),
                                stable_wait_sec=V2_STABLE_SEC,
                                end_marker_seen=True,
                                pending_v2=True,
                                pending_v2_source_dom_index=candidate.get("source_dom_index"),
                                selected_assistant_offset_from_end=candidate.get("selected_assistant_offset_from_end"),
                                skipped_newer_assistant_messages=candidate.get("skipped_newer_assistant_messages"),
                            )
                            handled = True
                        elif "v2_block:" + raw_hash in processed:
                            pending_v2.clear()
                            handled = True
                        else:
                            cmd, err = parse_local_agent_v2_code(raw)
                            if cmd is None:
                                processed.add("v2_block:" + raw_hash)
                                save_processed(processed)
                                status(
                                    state="parse_error",
                                    phase="error",
                                    latest_block_seen=True,
                                    latest_block_parse_ok=False,
                                    latest_block_hash=raw_hash,
                                    latest_parse_error=err,
                                    latest_raw_block_snippet=truncate(raw, int(CFG.get("max_raw_block_chars", 1800))),
                                )
                                log_line("agent1.log", f"v2_parse_error hash={raw_hash} err={err}")
                                pending_v2.clear()
                                if V2_PARSE_ERROR_NOTIFY:
                                    try:
                                        send_v2_parse_error(page, raw, err or "")
                                    except Exception as exc:
                                        status(state="parse_error_notify_failed", phase="error", last_error=str(exc))
                                        log_line("agent1.log", f"v2_parse_error_notify_failed: {exc}")
                                handled = True
                            else:
                                task_id = str(cmd.get("task_id") or "")
                                prior_task_status = load_executed_task_status(task_id)
                                processed.add("v2_block:" + raw_hash)
                                processed.add("task_id:" + task_id)
                                save_processed(processed)
                                if prior_task_status:
                                    result = duplicate_task_result(task_id)
                                    result["results"][0]["result"]["previous_status"] = prior_task_status
                                    record_executed_task(task_id, "skipped_duplicate_task_id", previous_status=prior_task_status, protocol=LOCAL_AGENT_V2)
                                    status(
                                        state="skipped_duplicate_task_id",
                                        phase="idle",
                                        latest_task_id=task_id,
                                        prior_task_status=prior_task_status,
                                        latest_block_hash=raw_hash,
                                    )
                                    log_line("agent1.log", f"v2_skipped_duplicate task_id={task_id} previous_status={prior_task_status}")
                                    pending_v2.clear()
                                    try:
                                        send_observation(page, result)
                                    except Exception as exc:
                                        atomic_write_text(WORKSPACE / "agent1_observation_send_failed.json", json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                                        status(state="observation_send_failed", phase="error", latest_task_id=task_id, last_error=str(exc))
                                        log_line("agent1.log", f"observation_send_failed: {exc}")
                                    handled = True
                                else:
                                    record_executed_task(task_id, "started", protocol=LOCAL_AGENT_V2, block_hash=raw_hash)
                                    status(
                                        state="executing",
                                        phase="executing_task",
                                        latest_block_seen=True,
                                        latest_block_parse_ok=True,
                                        latest_task_id=task_id,
                                        latest_block_hash=raw_hash,
                                        protocol=LOCAL_AGENT_V2,
                                    )
                                    log_line("agent1.log", f"v2_executing task_id={task_id} hash={raw_hash}")
                                    pending_v2.clear()
                                    result = execute_command(cmd, page=page, context=context)
                                    final_status = "succeeded" if result.get("overall_status") == "success" else "failed"
                                    record_executed_task(task_id, final_status, protocol=LOCAL_AGENT_V2, block_hash=raw_hash, overall_status=result.get("overall_status"))
                                    status(
                                        state="sending_observation",
                                        phase="executing_task",
                                        latest_task_id=task_id,
                                        last_execution_status=result.get("overall_status"),
                                    )
                                    try:
                                        send_observation(page, result)
                                    except Exception as exc:
                                        atomic_write_text(WORKSPACE / "agent1_observation_send_failed.json", json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                                        status(state="observation_send_failed", phase="error", latest_task_id=task_id, last_error=str(exc))
                                        log_line("agent1.log", f"observation_send_failed: {exc}")
                                    handled = True

                if (not handled) and ENABLE_LOCAL_AGENT_V1:
                    scan = get_assistant_messages_after_latest_user(page)
                    messages = scan.get("assistants_after_latest_user", []) or []
                    recent = messages[-int(CFG.get("recent_assistant_count", 8)):]
                    blocks = []
                    for m in recent:
                        for raw in extract_blocks(m.get("text", "")):
                            blocks.append((m, raw, sha12(raw)))

                    for msg_info, raw, raw_hash in blocks:
                        if "block_hash:" + raw_hash in processed:
                            continue

                        cmd, err = parse_command_block(raw)
                        if cmd is None:
                            processed.add("block_hash:" + raw_hash)
                            save_processed(processed)
                            status(
                                state="parse_error",
                                phase="error",
                                latest_block_seen=True,
                                latest_block_parse_ok=False,
                                latest_block_hash=raw_hash,
                                latest_parse_error=err,
                                latest_raw_block_snippet=truncate(raw, int(CFG.get("max_raw_block_chars", 1800))),
                            )
                            log_line("agent1.log", f"parse_error hash={raw_hash} err={err}")
                            try:
                                send_parse_error(page, raw, err or "")
                            except Exception as exc:
                                status(state="parse_error_notify_failed", phase="error", last_error=str(exc))
                                log_line("agent1.log", f"parse_error_notify_failed: {exc}")
                            handled = True
                            break

                        key = command_key(cmd, raw_hash)
                        if key in processed:
                            continue

                        processed.add(key)
                        processed.add("block_hash:" + raw_hash)
                        save_processed(processed)

                        status(
                            state="executing",
                            phase="executing_task",
                            latest_block_seen=True,
                            latest_block_parse_ok=True,
                            latest_task_id=cmd.get("task_id"),
                            latest_block_hash=raw_hash,
                            protocol="LOCAL_AGENT_V1_COMPAT",
                        )
                        log_line("agent1.log", f"executing key={key}")
                        result = execute_command(cmd, page=page, context=context)
                        status(state="sending_observation", phase="executing_task", latest_task_id=cmd.get("task_id"), last_execution_status=result.get("overall_status"))
                        try:
                            send_observation(page, result)
                        except Exception as exc:
                            atomic_write_text(WORKSPACE / "agent1_observation_send_failed.json", json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                            status(state="observation_send_failed", phase="error", latest_task_id=cmd.get("task_id"), last_error=str(exc))
                            log_line("agent1.log", f"observation_send_failed: {exc}")
                        handled = True
                        break

                if not handled:
                    if utc_ts() - last_wait_log > 15:
                        log_line("agent1.log", f"waiting title={diag.get('title')} v2_code_blocks={code_count}")
                        last_wait_log = utc_ts()
                    status(
                        state="waiting",
                        phase="idle",
                        page_title=diag.get("title"),
                        page_url=diag.get("url"),
                        layout=diag.get("layout"),
                        protocol=LOCAL_AGENT_V2,
                        latest_user_dom_index=v2_scan.get("latest_user_dom_index"),
                        assistant_messages_after_latest_user=v2_scan.get("assistant_messages_after_latest_user"),
                        latest_assistant_dom_index=v2_scan.get("dom_index"),
                        selected_assistant_offset_from_end=v2_scan.get("selected_assistant_offset_from_end"),
                        skipped_newer_assistant_messages=v2_scan.get("skipped_newer_assistant_messages"),
                        no_v2_code_block_after_latest_user=v2_scan.get("no_v2_code_block_after_latest_user"),
                        latest_assistant_code_blocks=code_count,
                        latest_assistant_v2_code_blocks=v2_code_count,
                        pending_v2=bool(pending_v2),
                        legacy_json_enabled=ENABLE_LOCAL_AGENT_V1,
                        processed_count=len(processed),
                    )

                time.sleep(POLL_SEC)

            except Exception as exc:
                tb = traceback.format_exc()
                if is_browser_context_closed_error(exc):
                    browser_recover_failures += 1
                    status(
                        state="recovering_browser_context",
                        recover_failures=browser_recover_failures,
                        last_error=type(exc).__name__ + ": " + str(exc),
                    )
                    log_line(
                        "agent1.log",
                        f"browser_context_closed recover_attempt={browser_recover_failures}: "
                        + truncate(type(exc).__name__ + ": " + str(exc), 1200),
                    )
                    try:
                        context.close()
                    except Exception:
                        pass

                    if browser_recover_failures > BROWSER_RECOVER_MAX_FAILURES:
                        status(
                            state="fatal_browser_context_recover_failed",
                            recover_failures=browser_recover_failures,
                            last_error=type(exc).__name__ + ": " + str(exc),
                        )
                        log_line("agent1.log", "browser_context_recover_failed_exit")
                        raise SystemExit(2)

                    try:
                        reset_browser_session_flags()
                        context = launch_context(pw)
                        page = prepare_page(context)
                        ignored = mark_visible_blocks_processed(page, processed)
                        status(
                            state="browser_context_recovered",
                            recover_failures=browser_recover_failures,
                            ignored_keys=ignored,
                            processed_count=len(processed),
                        )
                        log_line("agent1.log", "browser_context_recovered")
                        time.sleep(POLL_SEC)
                        continue
                    except Exception as rec_exc:
                        status(
                            state="browser_context_recover_error",
                            recover_failures=browser_recover_failures,
                            last_error=type(rec_exc).__name__ + ": " + str(rec_exc),
                            traceback=truncate(traceback.format_exc(), 4000),
                        )
                        log_line("agent1.log", "browser_context_recover_error: " + truncate(traceback.format_exc(), 4000))
                        time.sleep(min(30, 3 * browser_recover_failures))
                        continue

                browser_recover_failures = 0
                status(state="error", last_error=type(exc).__name__ + ": " + str(exc), traceback=truncate(tb, 4000))
                log_line("agent1.log", "loop error: " + truncate(tb, 4000))
                time.sleep(3)


if __name__ == "__main__":
    main()

# VISIBLE_WRAPPER_TEE_TO_UTF8_PATCH_MARKER
