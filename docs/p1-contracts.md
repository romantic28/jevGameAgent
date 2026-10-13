# P1 工具契约与执行约定

P1 提供核心运行控制流。SQLite 管理任务、观察、候选、请求、回答、执行意图、回执与视觉结果；浏览器仍由 Codex 宿主调用。P2 的知识检索、历史筛选和上下文预算，以及 P3 的 skill 迁移尚未实现。

## start

保留原有 self_check 模式及其平铺结构化返回。运行模式接收如下对象：

```json
{
  "request": {
    "mode": "run",
    "url": "http://192.168.1.123:18090/",
    "game_id": "unknown",
    "evidence_mode": "live",
    "goal": "关闭公告弹窗",
    "completion_criteria": [{"id": "notice_closed", "description": "公告消失，底层登录页可见"}],
    "constraints": [{"id": "no-spend", "description": "不消费游戏货币", "forbidden_tags": ["spend"]}]
  }
}
```

返回 run_id、context_version=0、OBSERVE 任务和原任务概要。启动自检检查客户端配置与运行存储；首次真实判别失败则 BLOCKED，未宣称配置存在即可证明网络或认证有效。任务没有客户端时不创建运行。

completion_criteria 的 ID 唯一且至少一项。约束文本进入 Jev 上下文；forbidden_tags 是宿主明确标注的确定性拦截条件。requires_clarification=true 会要求分析，不靠字符串匹配假装任意自然语言授权已解决。P1 不提供自动修改用户目标的接口；目标变更需要结束旧运行并用新目标启动。

evidence_mode 为 live、simulation 或 reconstructed，默认 live。验收夹具使用 simulation；重建证据不得冒充真实截图。模式随运行和完成证据保存。

## advance

每轮直接提交对象，不创建输入文件。必需字段为 run_id、submission_id、context_version、observation；previous_execution 和 analysis 为可选反馈。

- submission_id：同一提交的重试保持原 ID 和原输入；新一轮使用新 ID。同 ID 不同内容返回 submission_conflict。
- context_version：使用最新任务返回的版本。resume 也会增加版本；旧版本被拒绝。
- observation.frame：browser_id、page_id、url、screenshot_ref、layout_ref、viewport_width、viewport_height、device_pixel_ratio、coordinate_space。P1 的坐标空间仅支持 viewport_css_pixels。
- screenshot_ref：每次截图的唯一引用，可为平台引用或独立文件路径，不能是可能重复的画面内容 hash。不同截图即使像素相同，也需要不同捕获引用；重复消费旧引用会被拒绝。服务自动生成 observation_id 和时间。
- page_label、facts、interpretations、unknowns、changes、objects、candidates、goal_evidence、anomalies：事实与解释分开；异常线索保存为观察数据，完整缺陷归档流程留待 P3。

objects 使用 id、label、state。候选使用 id、description、semantic_target、action，并可附 constraint_tags、prerequisites、prerequisites_known 和 known_effects。prerequisites_known=false 的候选仍进入 Jev 选择空间，但选中后先取得缺失前提证据，不能直接执行。

action 是带 kind 的类型化对象：

- click：本次 screenshot_ref、x、y、button。
- key：本次 screenshot_ref、key。
- drag：本次 screenshot_ref、from_x、from_y、to_x、to_y。
- wait：wait_ms，范围 1–5000。
- observe：补充观察。
- analyze：有限帮助 category。
- stop：请求核验完成；证据不足时返回 OBSERVE。

坐标必须处于本次视口内。UI 候选绑定本次截图；页面句柄改变需要先 resume 后核对新截图。url 只允许无嵌入凭据的 HTTP(S)，且观察须与原任务同源。P1 的这些检查不能识别同源页面中任意账号切换，账号一致性仍须宿主按实际画面核对。

previous_execution 包含 decision_id、submission_status（accepted/rejected/unknown）、outcome（success/failure/unknown）、fact_indices 和 unresponsive。fact_indices 是本次 facts 的零基索引。仅工具接受不代表成功；视觉成功需要当前证据。rejected 不能同时报告成功，也不计为已验证无响应。丢失回执但新画面能够证实效果时，可记录 submission_status=unknown、outcome=success，保留这两个不同事实。

没有反馈或效果未知时保留 pending_execution，返回 ANALYZE/reconcile_execution；不会自动重发。没有候选时返回 ANALYZE/add_candidates。两次已验证无响应要求重新定位及检查可用日志；三轮无进展要求分析。analysis 反馈含 category、summary、semantic_targets、当前 fact_indices；完成对应分析后可重新交给 Jev，不把分析说明自动升级为长期知识。

goal_evidence 对每项完成条件提供 criterion_id、satisfied 和当前 fact_indices。所有条件都有当前视觉依据且待执行动作已核对后，Runtime 自动 COMPLETE。该检查验证证据关联和结构；自然语言条件与画面是否匹配仍由宿主视觉能力负责，不采用关键词匹配或 Jev 的停止选项替代它。

## 返回任务及重复交付

任务类型为 EXECUTE、OBSERVE、ANALYZE、COMPLETE 或 BLOCKED，附 run_id、phase、context_version、observation_id、原任务及必要反馈。Jev 决策附 decision_id 和 source=jev；确定性控制流 source=runtime。服务不生成 Jev 的思考理由。

只有 EXECUTE 且 execute_allowed=true 才允许宿主执行所给 candidate 和 binding，且每个 decision_id 至多提交一次 UI 操作。执行后先取得新截图，再提交回执。OBSERVE 携带等待上限；ANALYZE 携带有限帮助类别；COMPLETE 附已保存的目标证据。

相同提交的回答从账本返回，保持原决定与候选；交付元数据 replayed=true、execute_allowed=false。重复交付不授权再次点击，即使第一次回答丢失。宿主若无法确定是否提交过动作，使用 resume 和新证据核对。历史返回的 context_version 是原版本，当前版本以 status/resume 为准。

## resume、status、finish

resume 和 status 接收 request.run_id。resume 增加上下文版本、使旧执行绑定和在途请求失效，返回 OBSERVE。待核对动作只展示语义、decision_id 和来源，不带历史坐标或执行参数。服务重启后从 SQLite 恢复；旧回答迟到时记录为 late，不下发操作。

status 仅读账本和汇总，不访问 Jev、不修改运行。返回 phase、context_version、任务、待验证动作、错误、性能摘要和账本路径。

finish 接收 run_id、reason（cancelled/stopped/blocked）与 explanation，并可合并最后的 observation 和 previous_execution。取消或停止产生 STOPPED；blocked 保留可恢复状态。finish 导出报告，不用主动结束冒充目标完成。已完成或停止的运行不能继续执行。

## 持久化边界

数据库位于 run/<run_id>/runtime.sqlite3，schema version 为 1。事务保存请求摘要、观察及请求意图后才调用 Jev；保存执行意图后才返回 EXECUTE。网络等待期间不持有 SQLite 写事务。单运行只能有一个在途判别和一个待核对 UI 动作，SQLite 事务也保护跨服务实例的竞争。

SQLite 是唯一运行事实来源。state.json 和 report.json 是自动导出，修改它们不会改变运行。导出失败不抹掉已提交的账本；返回 export_warning，后续从账本恢复。

两个崩溃窗口均保守处理：Jev 已返回但回答尚未提交时，不承诺外部请求或收费 exactly-once；执行意图已提交但回执未保存时，动作可能发生也可能未发生，必须核验。恢复不通过自动重放解决任何一种不确定性。
