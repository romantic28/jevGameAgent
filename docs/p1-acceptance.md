# P1 核心 Runtime 验收记录

日期：2026-10-12。版本：0.2.0。结论：**P1 验收通过，包含本地、stdio 和当前桌面会话的五工具复验。P2 未开发。**

## 本阶段实现

- 五个结构化工具：start、advance、resume、status、finish；start 保留 P0 self_check 模式。
- 单运行状态机：观察、判别、待执行、核验、完成、受阻和停止；Jev 控制正常选择，Runtime 不操作浏览器。
- 每运行独立 SQLite 账本，记录原任务、观察、候选请求、实际回答、执行意图、工具回执、视觉验证、上下文版本与事件。
- 输入摘要幂等、旧截图/旧版本拒绝、单个在途请求、迟到回答归档、中断及进程重启恢复。
- 完成条件按当前事实引用核验；停止选择无证据时继续核验。工具接受、视觉成功、用户主动结束各自独立。
- 显式约束标签、未知前提保护、等待上限和无响应/无进展分析分支。没有 Codex 静默接管。
- 自动导出 state.json 和结束报告 report.json；它们不能反向改变数据库。

模块见 src/jev_game_agent/contracts.py、runtime.py、store.py、exports.py、server.py 和 decisions/。接口约定见 [P1 工具契约](p1-contracts.md)。

## 验证结果

72 项自动化测试通过，包含原有 P0 测试和 P1 的控制流、恢复与协议测试。模拟客户端注入服务核心，不增加生产环境的“假 Jev”开关。

关键验证包括：

- 相同 submission_id 仅判别一次；不同输入冲突被拒绝；重复输出不能授权第二次 UI 输入。
- 两个 Runtime 实例争用同一 SQLite 运行时，最多一个在途请求。
- Jev 返回前中断、回答后提交前崩溃、意图提交后交付前崩溃，均不能盲目重放。
- 实际 stdio 子进程退出后启动另一子进程，重载同一待执行意图，要求新截图核对；模拟视觉确认后才完成。
- resume/finish 期间失效的回答继续归档为 late，未产生执行意图。
- 改写兼容 state.json 不改变账本状态；导出失败不丢失已提交意图。
- 错误选项、缺失关键前提、陈旧截图、错误回执关联、跨页绑定、完成条件缺项、认证/网络/服务失败，都不会获得执行许可。
- 低 confidence 本身不改选；等待有 5 秒上限；分析完成后正常交回 Jev。
- 工具拒绝单独记录为 execution_rejected，不计为游戏视觉失败或已验证无响应；服务重启时缺 key 仍可查看已有账本，但不能发起判别。

固定 stdio 协议验收执行 start → advance → 重复 advance → resume → advance 核验完成 → status，并在独立运行调用 finish 取消。五个工具均被实际发现并调用。

真实客户端验收通过相同虚拟环境入口 `.venv/Scripts/python.exe -m jev_game_agent`，使用官方 MCP Python 客户端。一次实际 Jev 请求成功，返回 jev-1.13.0，耗时 1097.35 ms，共一次 HTTP 尝试、无重试。SQLite 记录一次决策、一次模拟执行意图和一次模拟视觉核验。

**视觉输入、提交回执与完成事实均为明确标注的 simulation 夹具。没有执行真实游戏输入，也没有把模拟完成写成游戏完成。** 该真实客户端验收通过一次开发命令的网络授权运行，不证明刷新后的桌面五工具调用免逐次审批；P0 已通过的桌面连通性证据仍有效，但新工具加载须独立复核。

### 重启后的桌面正式复验

用户重启 Codex 后，当前聊天已发现并实际调用 start、advance、resume、status、finish 五个工具。自检返回 LOCAL_READY，key 转发和客户端初始化正常。

直接通过桌面 MCP 提交 simulation 观察，Jev 选择 close_notice，返回 EXECUTE、来源观察及完整参数。实际模型为 jev-1.13.0，判别耗时 849.92 ms，共一次 HTTP 尝试，无重试、无逐次审批、无沙箱失败再升级路径。

本次桌面复验通过 14 项检查：五工具发现、真实判别、重复交付不授权输入、重复 decision_id 一致、同 ID 异内容冲突、旧版本拒绝、旧截图拒绝、resume 不重放、缺回执保留待核验、当前证据完成、仅一次逻辑判别、取消不冒充成功、取消后不能恢复执行，以及从重启后的服务读取此前运行账本。

主运行 009cf9bad4f544a980893a5a2fae37c1：三条模拟观察、一次判别、一次执行意图；恢复时不带旧坐标和参数，且保留未知动作。即使新夹具表示目标已达到，缺少回执关联时仍返回 ANALYZE/reconcile_execution；补齐与原 decision_id 关联的模拟视觉成功后才 COMPLETE。最终仍只有一次 Jev 请求。

独立取消运行正确进入 STOPPED，后续 resume 返回 run_closed。旧运行 e86e1daacb524e27a56d2500cae793b8 也由当前服务直接从 SQLite 读取，完成状态与原始模型/请求次数保持一致。本轮重跑 72 项测试，全部通过。

上述执行意图、回执和目标证据仍为 simulation，实际 UI 输入为 0。P1 验收的是服务控制流和持久化；真实游戏自动闭环属于后续 P3。

## 证据

run/p1-20261012/ 保存 pytest.xml、stdio-simulated.json、stdio-live.json、acceptance.json，以及本次 desktop-host-acceptance.json、desktop-refresh-tests.xml。桌面证据包含全部直接调用和 14 项检查；验收摘要保留源代码 hash。各运行的数据库与兼容导出位于对应 run_id 目录，图像引用均标记为 simulation。

P1 已更新本地安装元数据到 0.2.0，沿用 P0 的固定启动命令和依赖版本，没有改动全局 MCP 配置或权限策略。

## 桌面加载与阶段边界

开发时当前聊天仍只加载 P0 的旧定义；这项加载限制已经通过用户重启 Codex 后的实际五工具复验解决。独立 stdio 预检和桌面直接调用各有独立证据，没有相互替代。

P1 阶段验收现已全部完成，P2 的准入条件已满足。本轮止于 P1 验收。

P2 的按游戏知识检索、最近结果/失败摘要构建和输入预算尚未实现，P1 只向 Jev 提供原任务、本次观察、直接反馈和本次分析。P3 的浏览器闭环与 skill/AGENTS 迁移也未实施，原游戏操作规则保持有效。不能据 P1 声称已有完整自动游戏闭环。

## 可重复验收

在项目目录执行：

```powershell
.venv/Scripts/python.exe -m pytest -q --junitxml=run/p1-check/pytest.xml
.venv/Scripts/python.exe scripts/verify_p1.py --output run/p1-check/stdio-simulated.json
# 配置 key 且该执行环境允许联网时：
.venv/Scripts/python.exe scripts/verify_p1.py --live --output run/p1-check/stdio-live.json
```

脚本自动组织结构化请求、保存证据并维护运行目录；不要求宿主手写中间数据文件。
