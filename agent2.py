from __future__ import annotations

import os
import time
import traceback
import json
from pathlib import Path
from typing import Any, Dict, Optional


try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

try:
    import psutil
except Exception:
    psutil = None

from agent_common import (
    CFG,
    WORKSPACE,
    AGENT1_STATUS,
    AGENT2_STATUS,
    AGENT2_CONTROL,
    AGENT_CONTROL,
    now_iso,
    utc_ts,
    truncate,
    atomic_write_json,
    read_json,
    log_line,
    find_processes,
    start_detached_python,
    process_age_status,
    LIVE_TASK_DIR, DELAYED_MESSAGES, CHAT_SEND_LOCK,
    acquire_chat_send_lock, release_chat_send_lock, set_delayed_message, clear_delayed_message, tail_file, read_live_task,
)

# V9_3_MISSING_ONLY_AGENT2
# Purpose:
#   Keep agent2 as a supervisor only.
#   Do not send any text into ChatGPT automatically.
#   This avoids "no-context" idle prompts and the previous 180-second idle recovery disturbance.


POLL_SEC = float(CFG.get("agent2_poll_sec", CFG.get("poll_sec", 3.0)))
AGENT1_STALE_SEC = float(CFG.get("agent1_status_stale_sec", 25.0))
START_AGENT1_COOLDOWN_SEC = float(CFG.get("restart_cooldown_sec", 30.0))
ENABLE_AGENT1_RESTART = bool(CFG.get("enable_agent2_restart_agent1", True))


def status_pid_process(status_data: Dict[str, Any]) -> Dict[str, Any] | None:
    try:
        pid = int(status_data.get("pid") or 0)
    except Exception:
        return None
    if pid <= 0:
        return None
    if psutil is None:
        return None
    try:
        if not psutil.pid_exists(pid):
            return None
        proc = psutil.Process(pid)
        name = (proc.name() or "").lower()
        if "python" not in name:
            return None
        cmdline = " ".join(proc.cmdline())
        return {"pid": pid, "cmd": cmdline or "agent1_status_pid_fallback"}
    except Exception:
        return None


def status(**kwargs: Any) -> None:
    d: Dict[str, Any] = {
        "agent": "agent2",
        "pid": os.getpid(),
        "state": kwargs.pop("state", "supervising"),
        "updated_at": now_iso(),
        "updated_ts": utc_ts(),
        "version": "v12_watchdog_heartbeat_reporter",
        "chat_notify_enabled": True,
        "idle_recovery_enabled": False,
        "role": "supervisor_and_watchdog_reporter",
    }
    d.update(kwargs)
    atomic_write_json(AGENT2_STATUS, d)



def _chat_pages(context) -> list[Any]:
    pages = []
    try:
        for page in context.pages:
            try:
                if "chatgpt.com" in (page.url or ""):
                    pages.append(page)
            except Exception:
                pass
    except Exception:
        pass
    return pages


def _focus_composer(page) -> Dict[str, Any]:
    selectors = ["#prompt-textarea", "textarea", "div[contenteditable='true']", ".ProseMirror", "[data-testid='composer-root'] [contenteditable='true']"]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count <= 0:
                continue
            target = loc.nth(count - 1)
            target.scroll_into_view_if_needed(timeout=1200)
            target.focus(timeout=1200)
            return {"ok": True, "selector": sel}
        except Exception:
            continue
    return {"ok": False, "reason": "composer not found"}


def _composer_text_length(page) -> Dict[str, Any]:
    try:
        return page.evaluate("""
        () => {
          const el = document.querySelector('#prompt-textarea') || document.querySelector('textarea') || document.querySelector('div[contenteditable="true"]') || document.querySelector('.ProseMirror');
          if (!el) return {found:false, length:-1, text:''};
          const text = (el.value !== undefined ? el.value : (el.innerText || el.textContent || ''));
          return {found:true, length:text.length, text:text.slice(0,120)};
        }
        """)
    except Exception as exc:
        return {"found": False, "length": -1, "error": str(exc)}


def _click_send_button(page) -> Dict[str, Any]:
    try:
        return page.evaluate("""
        () => {
          const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
          };
          const disabled = (el) => !!(el.disabled || el.getAttribute('disabled') !== null || el.getAttribute('aria-disabled') === 'true');
          const buttons = Array.from(document.querySelectorAll('button'));
          const candidates = [];
          for (const b of buttons) {
            if (!visible(b) || disabled(b)) continue;
            const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '') + ' ' + (b.textContent || '')).toLowerCase();
            const likely = label.includes('send') || label.includes('发送') || (b.getAttribute('type') || '').toLowerCase() === 'submit';
            if (!likely) continue;
            let score = 0;
            if (label.includes('send') || label.includes('发送')) score += 100;
            if ((b.getAttribute('data-testid') || '').toLowerCase().includes('send')) score += 100;
            if ((b.getAttribute('type') || '').toLowerCase() === 'submit') score += 30;
            candidates.push({button:b, score});
          }
          candidates.sort((a,b) => b.score - a.score);
          if (!candidates.length) return {clicked:false, reason:'send button not found'};
          const b = candidates[0].button;
          try { b.click(); } catch(e) {}
          return {clicked:true, label:(b.getAttribute('aria-label') || b.textContent || '').slice(0,120)};
        }
        """)
    except Exception as exc:
        return {"clicked": False, "error": str(exc)}


def send_chat_message_from_agent2(message: str, task_id: str = "") -> Dict[str, Any]:
    if sync_playwright is None:
        return {"sent": False, "error": "playwright unavailable"}
    token = acquire_chat_send_lock("agent2", "watchdog_report", task_id, ttl_sec=90, wait_sec=2.0)
    if not token:
        return {"sent": False, "error": "chat send lock busy", "lock_path": str(CHAT_SEND_LOCK)}
    try:
        with sync_playwright() as pw:
            cdp_url = str(CFG.get("cdp_url") or "http://127.0.0.1:9222")
            browser = pw.chromium.connect_over_cdp(cdp_url)
            contexts = list(browser.contexts)
            if not contexts:
                return {"sent": False, "error": "no browser context"}
            context = contexts[0]
            pages = _chat_pages(context)
            if not pages:
                return {"sent": False, "error": "no chatgpt page"}
            page = pages[-1]
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.wait_for_timeout(200)
            comp = _composer_text_length(page)
            if comp.get("found") and int(comp.get("length") or 0) > 0:
                return {"sent": False, "error": "composer not empty", "composer": comp}
            focus = _focus_composer(page)
            if not focus.get("ok"):
                return {"sent": False, "error": "focus failed", "focus": focus}
            page.keyboard.insert_text(message[: int(CFG.get("max_message_chars", 12000))])
            page.wait_for_timeout(500)
            click = _click_send_button(page)
            if not click.get("clicked"):
                page.keyboard.press("Enter")
                page.wait_for_timeout(1200)
            else:
                page.wait_for_timeout(1200)
            after = _composer_text_length(page)
            ok = bool(after.get("found") and int(after.get("length") or 0) == 0)
            return {"sent": ok, "click": click, "after": after}
    except Exception as exc:
        return {"sent": False, "error": type(exc).__name__ + ": " + str(exc)[:1000]}
    finally:
        release_chat_send_lock(token)


def _sent_heartbeat_path() -> Path:
    LIVE_TASK_DIR.mkdir(parents=True, exist_ok=True)
    return LIVE_TASK_DIR / "agent2_sent_heartbeats.json"


def load_sent_heartbeats() -> Dict[str, Any]:
    return read_json(_sent_heartbeat_path(), default={}) or {}


def save_sent_heartbeats(data: Dict[str, Any]) -> None:
    atomic_write_json(_sent_heartbeat_path(), data)


def build_heartbeat_report(live: Dict[str, Any]) -> str:
    task_id = str(live.get("task_id") or "")
    stdout_path = str(live.get("stdout_path") or "")
    stderr_path = str(live.get("stderr_path") or "")
    stdout_tail = tail_file(Path(stdout_path), 2500) if stdout_path else str(live.get("stdout_tail") or "")[-2500:]
    stderr_tail = tail_file(Path(stderr_path), 1200) if stderr_path else ""
    lines = [
        "[TASK HEARTBEAT REPORT]",
        "task_id=" + task_id,
        "source=agent2",
        "state=" + str(live.get("state")),
        "elapsed_sec=" + str(live.get("elapsed_sec")),
        "powershell_pid=" + str(live.get("powershell_pid")),
        "last_output_age_sec=" + str(live.get("last_output_age_sec")),
        "soft_check_after_sec=" + str(live.get("soft_check_after_sec")),
        "visible_ui=" + str(live.get("visible_ui")),
        "stdout_path=" + stdout_path,
        "stderr_path=" + stderr_path,
        "decision_required=true",
        "",
        "stdout_tail:",
        stdout_tail[-2500:],
    ]
    if stderr_tail.strip():
        lines += ["", "stderr_tail:", stderr_tail[-1200:]]
    lines += [
        "",
        "Expected reply format:",
        "[HEARTBEAT_DECISION]",
        "task_id=" + task_id,
        "decision=CONTINUE_WAIT | SOFT_STOP_TASK | FORCE_KILL_TASK | RESTART_AGENT1_KEEP_BROWSER | ASK_USER_CONFIRM",
        "wait_sec=180",
        "reason=...",
        "[/HEARTBEAT_DECISION]",
    ]
    return "\n".join(lines)


def watchdog_live_task_report() -> Dict[str, Any]:
    live = read_live_task(default={}) or {}
    if not live:
        return {"active": False, "reason": "no live task"}
    task_id = str(live.get("task_id") or "")
    state = str(live.get("state") or "")
    if state not in ("running", "executing"):
        clear_delayed_message("heartbeat:" + task_id)
        return {"active": False, "task_id": task_id, "state": state}
    if not bool(live.get("heartbeat_report")):
        return {"active": True, "task_id": task_id, "state": state, "heartbeat_report": False}
    try:
        started_ts = float(live.get("started_ts") or 0.0)
        soft = float(live.get("soft_check_after_sec") or 0.0)
    except Exception:
        started_ts = 0.0
        soft = 0.0
    if not task_id or started_ts <= 0 or soft <= 0:
        return {"active": True, "task_id": task_id, "state": state, "error": "missing timing"}
    trigger_ts = started_ts + soft
    now = utc_ts()
    delay_key = "heartbeat:" + task_id
    set_delayed_message(delay_key, {
        "source": "agent2",
        "target": "chatgpt",
        "type": "TASK_HEARTBEAT_REPORT",
        "task_id": task_id,
        "state": "scheduled" if now < trigger_ts else "due",
        "next_trigger_ts": trigger_ts,
        "next_trigger_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_ts)),
        "remaining_sec": max(0, int(trigger_ts - now)),
    })
    if now < trigger_ts:
        return {"active": True, "task_id": task_id, "state": state, "remaining_sec": int(trigger_ts - now)}
    sent = load_sent_heartbeats()
    sent_key = task_id + ":" + str(int(trigger_ts))
    if sent.get(sent_key):
        return {"active": True, "task_id": task_id, "state": state, "already_sent": True}
    message = build_heartbeat_report(live)
    send_result = send_chat_message_from_agent2(message, task_id=task_id)
    if send_result.get("sent"):
        sent[sent_key] = {"sent_at": now_iso(), "sent_ts": now, "task_id": task_id}
        save_sent_heartbeats(sent)
        clear_delayed_message(delay_key)
    return {"active": True, "task_id": task_id, "state": state, "sent": bool(send_result.get("sent")), "send_result": send_result}


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    last_start_agent1_ts = 0.0

    log_line("agent2.log", "agent2 v9_safe_no_idle_prompt started")

    while True:
        try:
            shared_control = read_json(AGENT_CONTROL, default={}) or {}
            if shared_control.get("stop_all") or shared_control.get("stop_agent2"):
                status(state="stopped_by_control", agent_control=shared_control)
                log_line("agent2.log", "stopped_by_control " + str(shared_control))
                return

            procs = find_processes(
                "agent1.py",
                exclude_keywords=["agent2.py", "py_compile", "local_cmd", "install_v9"],
            )

            st = process_age_status(AGENT1_STATUS)
            agent1_status = read_json(AGENT1_STATUS, default={}) or {}
            control = read_json(AGENT2_CONTROL, default={}) or {}
            restart_paused = bool(control.get("pause_agent1_restart", False))
            status_proc = status_pid_process(agent1_status)
            if status_proc is not None and not any(int(p.get("pid") or 0) == int(status_proc["pid"]) for p in procs):
                procs.append(status_proc)

            stale = (
                (not st.get("exists"))
                or (
                    st.get("age_sec") is not None
                    and float(st["age_sec"]) > AGENT1_STALE_SEC
                )
            )

            restart_reason = None

            if ENABLE_AGENT1_RESTART and (not restart_paused) and (not procs):
                now = utc_ts()
                if now - last_start_agent1_ts >= START_AGENT1_COOLDOWN_SEC:
                    last_start_agent1_ts = now
                    restart_reason = "missing_process"
                    try:
                        pid = start_detached_python(str(CFG["agent1_script"]), cwd=str(CFG["root_dir"]))
                        log_line("agent2.log", f"start_agent1 reason={restart_reason} pid={pid}")
                    except Exception as exc:
                        log_line("agent2.log", "failed_start_agent1: " + str(exc))

            watchdog = watchdog_live_task_report()

            status(
                agent1_process_count=len(procs),
                agent1_processes=procs[:5],
                agent1_status_exists=bool(st.get("exists")),
                agent1_status_age_sec=st.get("age_sec"),
                agent1_state=agent1_status.get("state"),
                latest_block_hash=agent1_status.get("latest_block_hash"),
                latest_task_id=agent1_status.get("latest_task_id"),
                restart_reason=restart_reason,
                agent1_restart_paused=restart_paused,
                agent2_control=control,
                agent_control=shared_control,
                watchdog=watchdog,
                live_task=read_live_task(default={}),
                chat_send_lock=read_json(CHAT_SEND_LOCK, default={}) or {},
                delayed_messages=read_json(DELAYED_MESSAGES, default={}) or {},
                idle_recovery={
                    "idle_ok": False,
                    "disabled": True,
                    "reason": "v9 disables automatic ChatGPT message injection",
                },
            )

        except Exception:
            tb = truncate(traceback.format_exc(), 4000)
            log_line("agent2.log", "loop error: " + tb)
            status(state="error", last_error=tb)

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
