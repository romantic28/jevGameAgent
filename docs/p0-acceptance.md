# P0 能力探针验收记录

日期：2026-10-12。阶段结论：**P0 验收通过。P1 尚未开发。**

## 本阶段交付

实现 src 布局的 Python 包和固定入口 `.venv/Scripts/python.exe -m jev_game_agent`。MCP 当前只公开 start，输入为结构化 request，仅允许 mode=self_check；repetitions=0 为本地检查，1–10 为小型真实文本请求。服务不创建游戏运行或执行 UI；本阶段的真实页面输入由 Codex 宿主提供。

服务 lifespan 建立一个官方异步 TypeSafe 客户端，结束时关闭。SDK 内部重试关闭；探针对暂时连接故障、超时、429 和 5xx 最多重试一次，遵守 Retry-After 及 12 秒整体预算。认证、请求错误、明确网络权限拒绝和响应无效不重试。输出只保留安全错误类别，不输出 SDK 异常正文、请求 header 或密钥。

已锁定直接依赖：mcp 2.3.0、typesafe-sdk 0.7.4、pydantic 2.14.1、httpx2 2.13.1；测试依赖 pytest 9.1.1、pytest-asyncio 1.4.0。本地 Python 为 3.12.8。requirements-dev.windows.lock.txt 保存当前 Windows 开发依赖快照。

## 本地代码与协议验证

28 项自动化测试通过：工具发现、嵌套对象传参、结构化输出、缺 key 时阻塞、客户端跨请求复用与关闭、非法输入、认证/格式/限流/服务错误、重试上限、Retry-After 超预算、整体超时、无效回答、网络原因分类，以及日志和输出不泄露测试凭据。

原始 stdio 测试完成 initialize、tools/list 和 tools/call；stdout 每行均为 JSON-RPC，诊断日志走 stderr。官方 MCP Python 客户端在真实子进程中通过 legacy 模式协商到 2025-11-25 协议。

独立真实文本预检曾连续完成 10 个请求，共 10 次 HTTP 尝试，无重试，返回 jev-1.13.0。该预检通过开发命令的一次网络授权运行，不能单独证明桌面宿主免逐次审批；正式验收依据为下文的当前聊天直接 MCP 调用。

## 当前桌面宿主正式验收

新服务已在本聊天工具清单中被发现，实际调用入口为 mcp__jev_game_agent__start。

1. 直接传入 `{"request":{"mode":"self_check","repetitions":0}}`，返回 LOCAL_READY，api_key_present=true、client_ready=true。
2. 在相同 MCP 入口传入 `{"request":{"mode":"self_check","repetitions":10}}`，返回 TEXT_PROBE_PASSED，10/10 成功。
3. 两次调用的 service_instance_id 均为 aa43882b-0af0-43c4-8f3f-7c56d77e4ae3，client_initializations=1。
4. 10 个逻辑请求共 10 次 HTTP 尝试，无重试、无逐次授权、无先沙箱失败再升级执行路径。
5. 所有请求返回实际模型 jev-1.13.0；耗时 303.27–1020.94 ms，中位数 339.23 ms，总判别耗时 5160.06 ms。这是小型文本探针数据，不是游戏端到端性能。
6. 请求和返回均未包含 key。凭据由环境变量名称转发；注册未写入 key，未更改审批或沙箱策略。

服务报告中的 p0_accepted、host_approval_verified 和 ui_binding_verified 仍保持默认 false：服务本身无法观测宿主审批和浏览器结果。正式阶段结论由宿主另存 acceptance.json，附真实 MCP 与 UI 证据；没有修改服务报告来伪造它自身的观测能力。

## 真实游戏 UI 验收

用户本轮明确提供并授权使用 http://192.168.1.123:18090/。按项目 SKILL.md 与 AGENTS.md 恢复截图、可见决策 JSON、单次输入、截图验证和检查点流程，创建独立运行 p0-ui-20261012。

通过当前 cua_repl 的 in-app browser 打开指定地址，页面实际 URL 自动带 scene 参数；宿主保持 browserId=2、tabId=1。这些 ID 只描述本次会话，不是跨运行固定配置。

首次截图显示公告弹窗和右上角关闭叉号。动作前展示决策 JSON，将 (543,125) 绑定到本次截图，并保存 prepared 意图。动作来源明确记为 main_model、执行用途为 live_capability_probe，不宣称 Jev 或尚未实现的 Runtime 已选择/执行该游戏操作。

调用当前工具文档支持的 `p0GameTab.click([543,125])` 一次，随后取得新 AX 状态和截图。新图显示公告已消失、底层登录页和“进入游戏”按钮可见；同一 tab 与实际 URL 保持不变。验收完成后停止输入，未登录游戏、未消费、未战斗。本次单次操作路径未观察到交互、状态、显示或流程异常，没有进行两次无响应检查或额外控制台检查。

### 已确认的坐标和截图契约

- 浏览器截图实际为 JPEG；以 .jpg 保存。最初误按 PNG 头读取尺寸产生无效数值，随后读取工具截图说明并解析 JPEG 尺寸修正；没有据错误数值执行输入。
- 截图尺寸 710×892，浏览器视口 710×892，devicePixelRatio=1，前后未改变。
- 输入使用视口 CSS 像素，原点为页面左上角。该环境下截图像素与输入坐标 1:1，无浏览器窗口边框偏移。
- 可见游戏 canvas 的 CSS 边界为 x=104、y=0、width=502、height=892；canvas 内部尺寸为 720×1280。浏览器点击无需把视口点换算为内部游戏像素，也不能忽略左右黑边偏移。
- 当前 AX 树仅有页面和 image，未暴露公告按钮的语义对象；受支持的浏览器坐标 click 已实际生效。
- 缩放、视口、页面或布局变化时必须重新获取截图和几何信息。此次只验证该页面的截图与一次点击，未验证拖拽、键盘、多设备缩放或跨游戏操作。
- 浏览器对象由活动宿主持有；Python Runtime 后续只保存序列化绑定与执行参数，不直接持有或调用 REPL 对象。

此前 data: 内嵌夹具因浏览器 URL 策略被拒绝，未产生点击或通过其他方式绕过。本轮使用用户新指定的 HTTP 真实预览页补齐验证，该历史失败不计为游戏缺陷。

## 证据与下一阶段

忽略目录 run/p0-20261012/ 保存：

- pytest.xml：28 项自动化测试结果。
- stdio-offline.json、stdio-live-preflight.json：独立协议和文本预检。
- desktop-host-live.json：当前聊天直接 MCP 调用的自检与 10 次请求原始结构化结果。
- acceptance.json：宿主阶段结论、检查项、耗时与证据路径。

独立 UI 运行 run/p0-ui-20261012/ 保存 state.json、before.jpg 和 after.jpg。检查点为 complete，pending_action=null；页面已标记保留供后续恢复。未将该登录页显示的账号或服务器信息用作其他运行的进度。

**P1 准入条件已满足。** 下一阶段只开发核心 Runtime、五个工具、SQLite、幂等和恢复；完成 P1 验收后再推进上下文与知识阶段。本轮停在 P0，不实施 P1。

## 本地复验入口

在项目目录使用 PowerShell：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.windows.lock.txt
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/verify_p0.py --output run/p0-check/stdio-offline.json
# 仅当执行环境允许联网且已配置 key：
.venv/Scripts/python.exe scripts/verify_p0.py --live --output run/p0-check/stdio-live.json
.venv/Scripts/python.exe scripts/register_mcp.py
# 注册会写工作区外的 Codex 配置，须符合宿主文件权限：
.venv/Scripts/python.exe scripts/register_mcp.py --apply
```

该依赖快照仅对应 Windows；其他平台按 pyproject.toml 安装并重新记录依赖，不能直接强装 pywin32。独立复验仍不能替代实际桌面宿主调用验收。

## 官方接口依据

- [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python)、[异步客户端](https://docs.typesafe.ai/sdk/python/api/clients/async)、[重试](https://docs.typesafe.ai/sdk/python/api/retries)、[响应](https://docs.typesafe.ai/sdk/python/api/types/responses)、[异常](https://docs.typesafe.ai/sdk/python/api/exceptions)。
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[Client transports](https://py.sdk.modelcontextprotocol.io/client/transports/)、[Lifespan](https://py.sdk.modelcontextprotocol.io/handlers/lifespan/)。
- [Codex MCP](https://developers.openai.com/codex/mcp)：stdio、环境变量转发和配置。
