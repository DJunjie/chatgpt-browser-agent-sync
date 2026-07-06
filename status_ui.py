from __future__ import annotations

import json
import os
import subprocess
import time
import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from agent_common import (
    AGENT1_STATUS,
    AGENT2_STATUS,
    AGENT2_CONTROL,
    AGENT_CONTROL,
    CFG,
    atomic_write_json,
    find_processes,
    now_iso,
    read_json,
    start_detached_python,
    LIVE_TASK_DIR, DELAYED_MESSAGES, CHAT_SEND_LOCK, read_live_task, tail_file,
)


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def run_hidden_powershell(script: str, timeout: int = 10) -> subprocess.CompletedProcess:
    kwargs: Dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if os.name == "nt":
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = CREATE_NO_WINDOW
        except Exception:
            pass
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        **kwargs,
    )


def fresh_status_pid(path: Path, max_age_sec: int = 300) -> Optional[int]:
    data = read_json(path, default={}) or {}
    try:
        pid = int(data.get("pid") or 0)
        updated_ts = float(data.get("updated_ts") or 0.0)
    except Exception:
        return None
    if pid <= 0 or time.time() - updated_ts > max_age_sec:
        return None
    return pid


def pids_from_process_scan(agent_script: str, exclude_keywords: Optional[List[str]] = None) -> Set[int]:
    pids: Set[int] = set()
    for proc in find_processes(agent_script, exclude_keywords=exclude_keywords or []):
        try:
            pid = int(proc.get("pid") or 0)
        except Exception:
            continue
        if pid > 0:
            pids.add(pid)
    return pids


def stop_pids(pids: Iterable[int]) -> Dict[str, Any]:
    unique = sorted({int(pid) for pid in pids if int(pid) > 0})
    if not unique:
        return {"stopped": [], "message": "No matching process ids found."}
    script = "\n".join(
        f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue" for pid in unique
    )
    cp = run_hidden_powershell(script, timeout=10)
    return {
        "stopped": unique,
        "exit_code": cp.returncode,
        "stdout": cp.stdout.strip(),
        "stderr": cp.stderr.strip(),
    }


def write_agent2_pause(paused: bool) -> None:
    atomic_write_json(
        AGENT2_CONTROL,
        {
            "pause_agent1_restart": bool(paused),
            "reason": "set from status_ui",
            "set_at": now_iso(),
        },
    )


def write_agent_control(**kwargs: Any) -> None:
    data = {
        "stop_agent1": bool(kwargs.get("stop_agent1", False)),
        "stop_agent2": bool(kwargs.get("stop_agent2", False)),
        "stop_all": bool(kwargs.get("stop_all", False)),
        "reason": str(kwargs.get("reason") or "set from status_ui"),
        "set_at": now_iso(),
    }
    atomic_write_json(AGENT_CONTROL, data)


class StatusUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("ChatGPT Local Agents")
        root.geometry("1280x760")

        controls = tk.Frame(root)
        controls.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Button(controls, text="Stop Agent1", command=self.stop_agent1).pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Stop Agent2", command=self.stop_agent2).pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Stop Both", command=self.stop_both).pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Start Agent1", command=self.start_agent1).pack(side=tk.LEFT, padx=16)
        tk.Button(controls, text="Start Agent2", command=self.start_agent2).pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Clear Stop/Pause", command=self.clear_stop_pause).pack(side=tk.LEFT, padx=16)

        self.notice = tk.StringVar(value="Ready.")
        tk.Label(controls, textvariable=self.notice, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        top = tk.Frame(root)
        top.pack(fill=tk.BOTH, expand=True)

        self.a1 = ScrolledText(top, width=80, height=28)
        self.a2 = ScrolledText(top, width=80, height=28)
        self.a1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.a2.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.refresh()

    def set_notice(self, text: str) -> None:
        self.notice.set(f"{now_iso()}  {text}")

    def short_value(self, value: Any, limit: int = 1600) -> str:
        text = str(value)
        if len(text) > limit:
            return text[:limit] + f"...[+{len(text)-limit} chars]"
        return text

    def progress_bar(self, percent: Any, width: int = 28) -> str:
        try:
            pct = float(percent)
        except Exception:
            return "[" + "." * width + "]"
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        filled = int(round(width * pct / 100.0))
        return "[" + "#" * filled + "." * (width - filled) + f"] {pct:.1f}%"

    def fmt_main_progress(self, data: Dict[str, Any]) -> List[str]:
        state = data.get("state", "")
        phase = data.get("phase", "")
        action = data.get("current_action_type", "")
        task_id = data.get("current_task_id") or data.get("latest_task_id") or ""
        elapsed = data.get("live_elapsed_sec", "")
        percent = data.get("live_progress_percent", None)
        speed = data.get("live_progress_speed") or data.get("live_progress_speed_est") or ""
        age = data.get("live_last_output_age_sec", "")

        lines: List[str] = []
        lines.append("=== LIVE EXECUTION ===")
        lines.append(f"state: {state}")
        lines.append(f"phase: {phase}")
        if task_id:
            lines.append(f"task_id: {task_id}")
        if action:
            lines.append(f"action: {action}")
        if elapsed != "":
            lines.append(f"elapsed_sec: {elapsed}")
        if percent is not None:
            lines.append("progress: " + self.progress_bar(percent))
        if speed:
            lines.append(f"speed: {speed}")
        if age != "":
            lines.append(f"last_output_age_sec: {age}")
        return lines

    def fmt_command_block(self, data: Dict[str, Any]) -> List[str]:
        lines: List[str] = []
        command = data.get("current_command")
        cwd = data.get("current_cwd")
        script = data.get("current_script_path")

        if cwd or script or command:
            lines.append("")
            lines.append("=== EXECUTING STATEMENT ===")
        if cwd:
            lines.append(f"cwd: {cwd}")
        if script:
            lines.append(f"script: {script}")
        if command:
            lines.append("command:")
            lines.append(self.short_value(command, 3000))
        return lines

    def fmt_stdout_block(self, data: Dict[str, Any]) -> List[str]:
        lines: List[str] = []
        last_line = data.get("live_last_output_line")
        tail = data.get("live_stdout_tail")

        if last_line or tail:
            lines.append("")
            lines.append("=== LIVE OUTPUT ===")
        if last_line:
            lines.append("last_line:")
            lines.append(self.short_value(last_line, 1000))
        if tail:
            lines.append("")
            lines.append("stdout_tail:")
            lines.append(self.short_value(tail, 6000))
        return lines


    def fmt_shared_watchdog(self) -> str:
        lines: List[str] = []
        lines.append("=== WATCHDOG / DELAYED MESSAGES ===")
        now = time.time()
        delayed = read_json(DELAYED_MESSAGES, default={}) or {}
        if delayed:
            items = []
            for key, item in delayed.items():
                if not isinstance(item, dict):
                    continue
                try:
                    ts = float(item.get("next_trigger_ts") or 0.0)
                except Exception:
                    ts = 0.0
                remaining = max(0, int(ts - now)) if ts else ""
                items.append((ts or 9999999999.0, key, item, remaining))
            items.sort(key=lambda x: x[0])
            lines.append(f"pending_count: {len(items)}")
            for _ts, key, item, remaining in items[:6]:
                lines.append(
                    f"- key={key} type={item.get('type')} task_id={item.get('task_id')} "
                    f"source={item.get('source')} state={item.get('state')} remaining_sec={remaining} "
                    f"next={item.get('next_trigger_at')}"
                )
        else:
            lines.append("pending_count: 0")

        lines.append("")
        lines.append("=== CHAT SEND LOCK ===")
        lock = read_json(CHAT_SEND_LOCK, default={}) or {}
        if lock:
            try:
                exp = float(lock.get("expires_ts") or 0.0)
                ttl = int(exp - now)
            except Exception:
                ttl = ""
            lines.append(f"owner: {lock.get('owner')}")
            lines.append(f"purpose: {lock.get('purpose')}")
            lines.append(f"task_id: {lock.get('task_id')}")
            lines.append(f"pid: {lock.get('pid')}")
            lines.append(f"expires_in_sec: {ttl}")
        else:
            lines.append("lock: free")

        lines.append("")
        lines.append("=== LIVE TASK FILE ===")
        live = read_live_task(default={}) or {}
        if live:
            keys = [
                "task_id", "state", "powershell_pid", "elapsed_sec", "timeout_sec",
                "soft_check_after_sec", "heartbeat_report", "next_heartbeat_at",
                "visible_ui", "last_output_age_sec", "stdout_path", "stderr_path",
            ]
            for key in keys:
                if key in live:
                    lines.append(f"{key}: {self.short_value(live.get(key), 1000)}")
            tail_path = live.get("stdout_path")
            if tail_path:
                tail = tail_file(Path(str(tail_path)), 3000)
                if tail:
                    lines.append("")
                    lines.append("live_stdout_tail:")
                    lines.append(self.short_value(tail, 3000))
        else:
            lines.append("live_task: none")
        return "\n".join(lines)

    def fmt_key_values(self, title: str, data: Dict[str, Any]) -> List[str]:
        keys = [
            "updated_at",
            "pid",
            "version",
            "current_action_type",
            "current_cwd",
            "current_script_path",
            "page_title",
            "page_url",
            "assistant_messages",
            "recent_blocks",
            "processed_count",
            "latest_block_seen",
            "latest_block_parse_ok",
            "latest_task_id",
            "latest_parse_error",
            "last_execution_status",
            "send_phase",
            "send_attempt",
            "send_button_found",
            "send_button_enabled",
            "composer_text_len",
            "send_last_error",
            "pending_v2",
            "pending_v2_hash",
            "pending_v2_age_sec",
            "pending_v2_source_dom_index",
            "selected_assistant_offset_from_end",
            "skipped_newer_assistant_messages",
            "no_v2_code_block_after_latest_user",
            "recover_failures",
            "agent1_process_count",
            "agent1_state",
            "agent1_status_exists",
            "agent1_status_age_sec",
            "agent1_restart_paused",
            "restart_reason",
            "last_error",
        ]

        lines = ["", "=== DETAILS ==="]
        for key in keys:
            if key in data:
                lines.append(f"{key}: {self.short_value(data[key], 1600)}")
        return lines

    def fmt(self, title: str, data: Dict[str, Any] | None) -> str:
        if not data:
            return f"=== {title} ===\nstatus file missing or unreadable\n"

        lines = [f"=== {title} ==="]
        lines += self.fmt_main_progress(data)
        lines += self.fmt_command_block(data)
        lines += self.fmt_stdout_block(data)
        lines += self.fmt_key_values(title, data)

        full = json.dumps(data, ensure_ascii=False, indent=2)
        lines += ["", "=== FULL JSON HEAD ===", full[:6000]]

        return "\n".join(lines)

    def update_panel(self, panel: ScrolledText, text: str) -> None:
        panel.configure(state=tk.NORMAL)
        panel.delete("1.0", tk.END)
        panel.insert(tk.END, text)
        panel.configure(state=tk.DISABLED)

    def refresh(self) -> None:
        shared = self.fmt_shared_watchdog()
        self.update_panel(self.a1, self.fmt("AGENT1", read_json(AGENT1_STATUS)) + "\\n\\n" + shared)
        self.update_panel(self.a2, self.fmt("AGENT2", read_json(AGENT2_STATUS)) + "\\n\\n" + shared)
        self.root.after(1000, self.refresh)

    def agent1_pids(self) -> Set[int]:
        pids = pids_from_process_scan("agent1.py", exclude_keywords=["agent2.py", "py_compile", "local_cmd"])
        pid = fresh_status_pid(AGENT1_STATUS)
        if pid:
            pids.add(pid)
        return pids

    def agent2_pids(self) -> Set[int]:
        pids = pids_from_process_scan("agent2.py", exclude_keywords=["agent1.py", "py_compile", "local_cmd"])
        pid = fresh_status_pid(AGENT2_STATUS)
        if pid:
            pids.add(pid)
        return pids

    def stop_agent1(self) -> None:
        try:
            write_agent2_pause(True)
            write_agent_control(stop_agent1=True, reason="stop agent1 from status_ui")
            result = stop_pids(self.agent1_pids())
            self.set_notice("Agent1 stop requested; cooperative stop set and agent2 restart is paused. " + json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            self.set_notice(f"Stop Agent1 failed: {type(exc).__name__}: {exc}")

    def stop_agent2(self) -> None:
        try:
            write_agent_control(stop_agent2=True, reason="stop agent2 from status_ui")
            result = stop_pids(self.agent2_pids())
            self.set_notice("Agent2 stop requested; cooperative stop set. " + json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            self.set_notice(f"Stop Agent2 failed: {type(exc).__name__}: {exc}")

    def stop_both(self) -> None:
        try:
            write_agent2_pause(True)
            write_agent_control(stop_all=True, reason="stop both agents from status_ui")
            agent2 = stop_pids(self.agent2_pids())
            agent1 = stop_pids(self.agent1_pids())
            self.set_notice(
                "Both agents stop requested; cooperative stop set and agent1 restart is paused. "
                + json.dumps({"agent2": agent2, "agent1": agent1}, ensure_ascii=False)
            )
        except Exception as exc:
            self.set_notice(f"Stop Both failed: {type(exc).__name__}: {exc}")

    def clear_stop_pause(self) -> None:
        try:
            write_agent_control(reason="clear stop requests from status_ui")
            write_agent2_pause(False)
            self.set_notice("Stop requests cleared and agent1 restart is allowed again.")
        except Exception as exc:
            self.set_notice(f"Clear stop/pause failed: {type(exc).__name__}: {exc}")

    def start_agent1(self) -> None:
        try:
            self.clear_stop_pause()
            pid = start_detached_python(str(CFG["agent1_script"]), cwd=str(CFG["root_dir"]))
            self.set_notice(f"Agent1 start requested. pid={pid}")
        except Exception as exc:
            self.set_notice(f"Start Agent1 failed: {type(exc).__name__}: {exc}")

    def start_agent2(self) -> None:
        try:
            self.clear_stop_pause()
            pid = start_detached_python(str(CFG["agent2_script"]), cwd=str(CFG["root_dir"]))
            self.set_notice(f"Agent2 start requested. pid={pid}")
        except Exception as exc:
            self.set_notice(f"Start Agent2 failed: {type(exc).__name__}: {exc}")


def main() -> None:
    root = tk.Tk()
    StatusUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
