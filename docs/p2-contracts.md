# P2 上下文与知识契约

版本：0.3.0。保留 P1 的五个工具和执行协议，以下字段均为向后兼容的增量。`SKILL.md` 与 `AGENTS.md` 的宿主流程迁移留给 P3。

## 自动构建的决策输入

Runtime 从 SQLite 账本补齐任务原文、当前观察、行动反馈和当前游戏知识。宿主继续只提交本轮观察与相关反馈；无需读取整库、整理历史或创建中间 JSON 文件。

- 当前 `observation.facts` 只包含本轮事实。跨页面旧事实进入 `historical_known`，标记 `historical_not_current`；这不是当前页面事实。
- `recent_verified_results` 默认保留最近 6 条视觉验证结果。`active_blockers` 独立保留未恢复的语义目标失败和工具拒绝，超出窗口仍保留；同目标的视觉成功解除阻塞。工具接受、Jev 选择和主模型解释不计为视觉成功。
- 知识条目附带游戏命名空间、文档及标题、文档 SHA-256、状态、匹配词和历史来源标记。知识不能作为消费授权或当前账号进度。
- 字符预算覆盖实际 SDK 请求的 `model/state/questions` 紧凑 Unicode JSON，默认 12,000 字符。按完整记录裁剪旧成功、旧页面、可选知识和旧资源；任务、约束、当前证据、未知项、候选、有效阻塞及显式必需知识受保护。保护内容仍超预算时返回 `ANALYZE/split_context`，不发送 Jev 请求。

`TaskResult.context_summary` 与 `status.context_summary` 显示预算、输入字符数、裁剪记录、检索引用、资源变化和知识问题。其中 `context_version/observation_id` 指明所对应的决策上下文；完成阶段可能保留最后一次决策摘要。完整输入保存在 `decisions.request`，摘要保存在 `context_built` 事件。`state.json/report.json` 是派生文件，恢复以账本为准。

## 新增观察字段

`resources` 是资源名称到 `{value, unit, source_text}` 的映射。只填写当前可见且读完整单位的数值，拒绝 NaN/Infinity。同资源、同单位的新旧读数产生差值；未再次观察的读数标作历史，不补为 0；单位变化不直接相减。极端有限数相减溢出时，差值明确为 null，前后读数仍保留。

`game_identity` 可提供 `{game_id, fact_indices}`，通过本轮事实补充未知游戏身份。已识别身份不能在同一 run 切换到另一游戏。`start.game_id` 已知时沿用；P1 的 `unknown` 运行仍可进行无知识判别，但请求知识时必须先识别身份。

`knowledge_query` 可一次补充稳定语义 `terms`，或指定 `required_refs`。引用格式为 `{document: "systems/tower.md", heading: "逐层开放与结算选项"}`，不用流水编号。正常检索依据当前页面、可见对象、候选语义目标、候选前提和未知项，排除全局目标；标题及文件名用于相关性过滤，正文用于排序。默认最多 6 个可选片段，显式必需片段另行保留。

显式引用缺失、重名、文件不可读、相关知识有争议或已失效时请求 `resolve_knowledge`；身份未知时请求 `identify_game`。`hypothesis/user_provided/observed` 等状态原样保留，不升级为验证事实。索引可随 Markdown 内容哈希变化重建，不需要向量服务。

## 知识草案和冲突

`advance` 和带最终新观察的 `finish` 接受 `knowledge_drafts`、`knowledge_conflicts`。没有增量就不传，普通操作完成不会自动写知识。

草案需要以下证据及判断：

- `ref`、`expected_document_hash`：指定现有文档标题和刚读取的文档原始字节哈希；新文档使用空字节的 SHA-256。
- `statement/visible_checks/scope`：独立可读的结论、界面判断点，以及环境、构建、账号阶段、前置、特权；未知项明确写“未知”。
- `evidence_kind/fact_indices/status`：引用本轮事实；`verified_result` 还必须关联本轮视觉验证成功。`visible_rule` 由宿主核对当前界面说明，程序验证引用关系，不宣称能判断自然语言规则真假。
- `admission_reason/increment_reason`：主线、新机制、纠正或预防方法的具体新增价值；主线写入 onboarding。`reusable=true`、`sensitive_content_removed=true`、`contains_account_progress=false` 是宿主完成语义检查的声明，默认不满足。
- `operation`：append、replace 或 dispute。替换需要 `correction` 理由及明确的 `resolution_explanation`，保留修订说明；同结论追加不重复写入。

仅 `evidence_mode=live` 可申请更新共享知识。这个声明不能单独证明事实真实：游戏宿主承担截图与语义核验。测试虽会检查该分支，只操作临时知识目录；模拟验收不能更新仓库中的游戏资料。

当前按已读取的 `knowledge/fengyu/maintenance.md` 实现维护政策，并校验规范化文本指纹。其他游戏可以隔离检索；其维护规则缺失或与已实现政策不一致时，写入返回分析要求，需先实现对应规则。不能把任意 Markdown 当作可执行政策。

同标题的新旧结论不同，或宿主明确报告冲突时，旧结论标为“有争议”，`questions.md` 保存双方说法及标题链接；Runtime 请求分析，不自动选择哪一方正确。跨标题的自然语言冲突和是否真正可复用由宿主判断，可通过 `knowledge_conflicts` 提交；程序不使用关键词过滤假装解决语义问题。

共享条目不保存账号余额/进度、固定坐标、凭据或本机证据链接。确定性检查拒绝常见敏感模式，语义检查由上述宿主声明承担。运行证据另存在账本及知识写入日志，生成的 Markdown 可独立阅读。

## 并发与恢复

同一知识根下使用游戏级文件锁。落盘前核对 SHA-256 并重读，保留其他会话改动。知识发布先在 `run/knowledge-journal/<game>/` 保存准备日志，再逐文件替换并标为 published；系统条目和 questions 的部分发布可恢复。文件系统与 SQLite 不承诺一个原子事务，日志保存原证据，支持发布后 SQLite 回滚的同内容重试。

若准备日志与外部编辑冲突，停止恢复并要求分析；不覆盖外部内容。原始日志是本地审计资料，不复制进知识 Markdown。其他编辑器不遵守本项目锁时，仍需通过重读与哈希发现冲突；不承诺跨任意外部编辑器的分布式事务。

P1 旧账本不改结构；新增字段使用默认值，旧 submission 规范化后比较，保持相同请求可重放且不再许可执行。新上下文可从旧 observations/events 重建，包括补读旧工具拒绝的决策语义目标。
