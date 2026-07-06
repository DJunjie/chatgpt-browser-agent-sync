from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# V9_3_AGENT_COMMON_NO_WINDOW
DEFAULT_CONFIG: Dict[str, Any] = {
    "root_dir": "D:/chatgpt-browser-agent",
    "workspace_dir": "D:/chatgpt-browser-agent/workspace",
    "logs_dir": "D:/chatgpt-browser-agent/logs",
    "python_exe": "C:/Users/Du/Anaconda3/envs/chatgpt-agent/python.exe",
    "agent1_script": "D:/chatgpt-browser-agent/agent1.py",
    "agent2_script": "D:/chatgpt-browser-agent/agent2.py",
    "browser_profile_dir": "D:/chatgpt-browser-agent/browser_profile",
    "chat_url": "https://chatgpt.com/",
    "target_chat_title": "",
    "cdp_url": "http://127.0.0.1:9222",
    "poll_sec": 2.0,
    "agent2_poll_sec": 3.0,
    "recent_assistant_count": 8,
    "agent1_status_stale_sec": 25,
    "agent2_status_stale_sec": 25,
    "restart_cooldown_sec": 30,
    "max_message_chars": 12000,
    "max_raw_block_chars": 1800,
    "max_log_bytes": 800000,
    "enable_agent1_restart_agent2": True,
    "enable_agent2_restart_agent1": True,
    "idle_recovery_enabled": False,
    "idle_recovery_idle_sec": 999999,
    "idle_recovery_repeat_sec": 999999,
    "idle_recovery_max_prompts": 0,
    "chat_notify_enabled": False,
    "v9_safe_no_idle_prompt": True,
}


def now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def utc_ts() -> float:
    return time.time()


def sha12(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()[:12]


def truncate(text: Any, limit: int) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"...[+{len(s) - limit} chars]"


def load_config() -> Dict[str, Any]:
    p = Path(__file__).with_name("config.json")
    cfg = dict(DEFAULT_CONFIG)

    if p.exists():
        try:
            raw = p.read_text(encoding="utf-8-sig")
            loaded = json.loads(raw) if raw.strip() else {}
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception as exc:
            print(f"[WARN] failed to load config.json: {exc}", flush=True)

    for key in ["root_dir", "workspace_dir", "logs_dir", "browser_profile_dir"]:
        try:
            Path(str(cfg[key])).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    return cfg


CFG = load_config()

ROOT = Path(str(CFG["root_dir"]))
WORKSPACE = Path(str(CFG["workspace_dir"]))
LOGS = Path(str(CFG["logs_dir"]))

AGENT1_STATUS = WORKSPACE / "agent1_status.json"
AGENT2_STATUS = WORKSPACE / "agent2_status.json"
AGENT1_PID = WORKSPACE / "agent1.pid"
AGENT2_PID = WORKSPACE / "agent2.pid"
PROCESSED_KEYS = WORKSPACE / "agent1_processed_keys.json"
AGENT2_CONTROL = WORKSPACE / "agent2_control.json"
AGENT_CONTROL = WORKSPACE / "agent_control.json"
AGENT1_OUTBOX = WORKSPACE / "agent1_outbox_last_message.txt"
AGENT2_OUTBOX = WORKSPACE / "agent2_outbox_last_message.txt"
PENDING_OUTBOX = WORKSPACE / "pending_outbox"


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + str(os.getpid()) + "." + str(int(time.time() * 1000)) + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(str(tmp), str(path))


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    d = dict(data)
    d.setdefault("updated_at", now_iso())
    d.setdefault("updated_ts", utc_ts())
    atomic_write_text(Path(path), json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default=None) -> Optional[Dict[str, Any]]:
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
        if not raw.strip():
            return default
        data = json.loads(raw)
        return data if data is not None else default
    except Exception:
        return default


def log_line(name: str, text: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / name
    try:
        if path.exists() and path.stat().st_size > int(CFG.get("max_log_bytes", 800000)):
            bak = path.with_name(path.name + ".1")
            if bak.exists():
                bak.unlink()
            path.rename(bak)

        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{now_iso()}] {text}\n")
    except Exception:
        pass


def run_powershell_capture(script: str, timeout: int = 10) -> subprocess.CompletedProcess:
    # V9_3_NO_POWERSHELL_WINDOW_FLASH
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        except Exception:
            pass
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        **kwargs,
    )


def list_processes() -> List[Dict[str, Any]]:
    script = (
        "$items=Get-CimInstance Win32_Process; "
        "$arr=@(); "
        "foreach($p in $items){"
        "$arr += [pscustomobject]@{ProcessId=$p.ProcessId;CommandLine=[string]$p.CommandLine}"
        "}; "
        "$arr | ConvertTo-Json -Compress"
    )
    try:
        cp = run_powershell_capture(script, timeout=15)
        if cp.returncode != 0 or not cp.stdout.strip():
            return []

        data = json.loads(cp.stdout)
        if isinstance(data, dict):
            data = [data]

        out: List[Dict[str, Any]] = []
        for x in data:
            try:
                out.append({
                    "pid": int(x.get("ProcessId", 0)),
                    "cmd": str(x.get("CommandLine") or ""),
                })
            except Exception:
                pass
        return out
    except Exception:
        return []


def find_processes(keyword: str, exclude_keywords: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    exclude_keywords = exclude_keywords or []
    keyword_l = keyword.lower()
    excludes_l = [x.lower() for x in exclude_keywords]

    found: List[Dict[str, Any]] = []
    for p in list_processes():
        cmd = str(p.get("cmd") or "")
        cmd_l = cmd.lower()
        if keyword_l not in cmd_l:
            continue
        if any(x in cmd_l for x in excludes_l):
            continue
        found.append(p)
    return found


def start_detached_python(script: str, cwd: Optional[str] = None) -> int:
    python_exe = str(CFG.get("python_exe") or sys.executable)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [python_exe, str(script)],
        cwd=str(cwd or ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return int(proc.pid)


def process_age_status(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {
            "exists": False,
            "age_sec": None,
            "mtime": None,
            "path": str(path),
        }

    try:
        mtime = path.stat().st_mtime
        return {
            "exists": True,
            "age_sec": max(0.0, time.time() - mtime),
            "mtime": mtime,
            "path": str(path),
        }
    except Exception as exc:
        return {
            "exists": True,
            "age_sec": None,
            "mtime": None,
            "path": str(path),
            "error": str(exc),
        }


# WATCHDOG_LIVE_TASK_V1
LIVE_TASK_DIR = WORKSPACE / "live_task"
LIVE_TASK_HISTORY = LIVE_TASK_DIR / "history"
CHAT_SEND_LOCK = WORKSPACE / "chat_send_lock.json"
CHAT_OUTBOX = WORKSPACE / "chat_outbox"
CHAT_SENT = WORKSPACE / "chat_sent"
DELAYED_MESSAGES = WORKSPACE / "delayed_messages.json"
HEARTBEAT_DECISIONS = WORKSPACE / "heartbeat_decisions.jsonl"


def tail_file(path: Path, limit: int = 4000) -> str:
    # SELF_UPDATE_CHUNK_PROTOCOL_V1_MARKER robust utf16/nul safe tail
    try:
        p = Path(path)
        if not p.exists():
            return ""
        data = p.read_bytes()
        if not data:
            return ""
        sample = data[:4096]
        nul_count = sample.count(b"\x00")
        text = ""
        if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
            try:
                text = data.decode("utf-16", errors="replace")
            except Exception:
                text = data.decode("utf-8", errors="replace")
        elif nul_count > max(8, len(sample) // 8):
            try:
                text = data.decode("utf-16-le", errors="replace")
            except Exception:
                text = data.decode("utf-8", errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
        text = text.replace("\x00", "")
        if len(text) <= limit:
            return text
        return text[-limit:]
    except Exception:
        return ""

def read_live_task(default=None) -> Dict[str, Any]:
    return read_json(LIVE_TASK_DIR / "current.json", default=default or {}) or {}


def write_live_task(data: Dict[str, Any]) -> None:
    LIVE_TASK_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(LIVE_TASK_DIR / "current.json", data)


def read_delayed_messages() -> Dict[str, Any]:
    data = read_json(DELAYED_MESSAGES, default={}) or {}
    if not isinstance(data, dict):
        return {}
    return data


def set_delayed_message(key: str, data: Dict[str, Any]) -> None:
    if not key:
        return
    items = read_delayed_messages()
    d = dict(data)
    d.setdefault("key", key)
    d.setdefault("updated_at", now_iso())
    d.setdefault("updated_ts", utc_ts())
    items[str(key)] = d
    atomic_write_json(DELAYED_MESSAGES, items)


def clear_delayed_message(key: str) -> None:
    if not key:
        return
    items = read_delayed_messages()
    if str(key) in items:
        items.pop(str(key), None)
        atomic_write_json(DELAYED_MESSAGES, items)


def acquire_chat_send_lock(owner: str, purpose: str, task_id: str = "", ttl_sec: int = 60, wait_sec: float = 0.0) -> Optional[str]:
    """Acquire a cross-agent send lock using atomic file create.

    Returns a token when acquired; otherwise None.
    Stale locks are removed after ttl_sec.
    """
    CHAT_SEND_LOCK.parent.mkdir(parents=True, exist_ok=True)
    token = f"{owner}:{os.getpid()}:{int(time.time()*1000)}:{sha12(owner + purpose + task_id + str(time.time()))}"
    deadline = time.time() + max(0.0, float(wait_sec))
    while True:
        now = time.time()
        try:
            if CHAT_SEND_LOCK.exists():
                stale = False
                try:
                    data = read_json(CHAT_SEND_LOCK, default={}) or {}
                    expires_ts = float(data.get("expires_ts") or 0.0)
                    pid = int(data.get("pid") or 0)
                    stale = expires_ts <= now or pid <= 0
                except Exception:
                    stale = True
                if stale:
                    try:
                        CHAT_SEND_LOCK.unlink()
                    except Exception:
                        pass
            payload = {
                "owner": owner,
                "purpose": purpose,
                "task_id": task_id,
                "pid": os.getpid(),
                "token": token,
                "acquired_at": now_iso(),
                "acquired_ts": now,
                "expires_ts": now + max(10, int(ttl_sec)),
            }
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(str(CHAT_SEND_LOCK), flags)
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            finally:
                os.close(fd)
            return token
        except FileExistsError:
            if time.time() >= deadline:
                return None
            time.sleep(0.25)
        except Exception:
            if time.time() >= deadline:
                return None
            time.sleep(0.25)


def release_chat_send_lock(token: str) -> bool:
    try:
        data = read_json(CHAT_SEND_LOCK, default={}) or {}
        if str(data.get("token") or "") != str(token):
            return False
        CHAT_SEND_LOCK.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def enqueue_chat_message(source: str, message_type: str, body: str, priority: int = 50, task_id: str = "", dedupe_key: str = "") -> Path:
    CHAT_OUTBOX.mkdir(parents=True, exist_ok=True)
    created_ts = utc_ts()
    safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(source or "agent"))[:60]
    safe_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(message_type or "message"))[:80]
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id or "no_task"))[:100]
    name = f"{int(created_ts*1000)}_{int(priority):03d}_{safe_source}_{safe_type}_{safe_task}.json"
    path = CHAT_OUTBOX / name
    payload = {
        "source": source,
        "type": message_type,
        "priority": int(priority),
        "task_id": task_id,
        "dedupe_key": dedupe_key,
        "body": body,
        "created_at": now_iso(),
        "created_ts": created_ts,
        "path": str(path),
    }
    atomic_write_json(path, payload)
    return path
