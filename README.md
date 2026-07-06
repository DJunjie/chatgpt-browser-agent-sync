# ChatGPT 双 Agent 监督版

目标：两个 agent 相互监督；agent1 识别失败时直接在当前聊天里写“不识别”并贴出读到的内容；agent2 不再靠鼠标坐标或菜单栏点击输入框，而是用 CDP + DOM focus；额外 UI 只读显示两个 agent 状态。

## 文件

- `agent1.py`：扫描 ChatGPT、解析 `[[LOCAL_AGENT_START]]`、执行 actions、发送 observation。每轮写 `workspace/agent1_status.json`。解析失败会直接发回“不识别”和 raw block。
- `agent2.py`：读取 `agent1_status.json`，发现 agent1 假死/状态不更新则重启 agent1。发现 parse error 时通过 CDP/DOM 通知当前聊天。
- `status_ui.py`：只读 UI，显示两个 agent 状态和日志，不点击、不发消息。
- `POWERSHELL_AND_PROMPT_BUGS.md`：记录所有已知坑，后续新增问题先更新它。
- `start_all.ps1` / `stop_all.ps1`：启动和停止。

## 安装

解压到：

```powershell
D:\chatgpt-browser-agent
```

安装依赖：

```powershell
C:\Users\Du\Anaconda3\envs\chatgpt-agent\python.exe -m pip install -r D:\chatgpt-browser-agent\requirements.txt
C:\Users\Du\Anaconda3\envs\chatgpt-agent\python.exe -m playwright install chromium
```

检查 `config.json`：

```json
{
  "chat_url": "https://chatgpt.com/c/6a4a699b-6ca0-83ee-9a1a-a90cbec5a918",
  "target_chat_title": "浏览器ChatGPT控制电脑",
  "cdp_url": "http://127.0.0.1:9222"
}
```

启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\chatgpt-browser-agent\start_all.ps1
```

停止：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\chatgpt-browser-agent\stop_all.ps1
```

## 测试指令

```text
[[LOCAL_AGENT_START]]
{
  "task_id": "hello_test_001",
  "actions": [
    {
      "type": "run_powershell",
      "command": "Write-Host 'hello from agent1'",
      "timeout": 20
    }
  ]
}
[[LOCAL_AGENT_END]]
```

## 支持 action

- `run_powershell`
- `sleep`
- `read_file`
- `write_file`
- `list_dir`

复杂源码修改不要用长 `write_file content`。这正是之前反复出错的来源。

