# Tasks: intent-routing-context-quality（意图识别：跨轮上下文 / 召回质量 / 误杀可观测 / 缺参追问 / 结构化澄清选项）

> 实现纪律：每个实现任务按 TDD（先写失败测试 → 跑红 → 实现 → 跑绿）；测试全部不依赖外部服务（chat/embedding 用 stub 或确定性 fake，消息历史用内存 FakeStore）。测试命令：`$env:PYTHONPATH="."; .venv\Scripts\python -m pytest`。
> 防漏纪律（吸取 skill-retrieval-routing 教训）：**横切承诺（metric / 日志标注 / 降级）必须单独成任务且带断言**，不得只写进功能任务的描述里。

## 1. 配置

- [x] 1.1 `config.py` 的 `RoutingSettings` 新增：`context_turns:int=3`、`query_rewrite:bool=true`、`refer_terms:list[str]`（内置常用中文指代词默认表）、`multi_vector:bool=true`、`llm_conf_high:float=0.7`、`arg_guard:bool=true`、`hybrid:bool=true`、`rrf_k:int=60`、`keyword_top_k:int=10`、`clarify_options:bool=true`、`clarify_option_max:int=4`；`EmbeddingSettings` 新增 `batch_size:int=64`（索引构建分批请求）；`config.yaml` 与 `.env.example` 补注释样例。验证：配置加载单测（默认值、yaml/env 覆盖、`context_turns=0`/各开关关闭含 `hybrid=false`/`clarify_options=false`）通过。

## 2. 多向量索引与 has_examples 标注（skill_router/index.py）

- [x] 2.1 先写测试（红）：① 含多条差异 example 的 Skill，query 匹配其中一种用法时按 max-sim 得高分、不被其余 example 拉低；② 无 examples 的 Skill 构建记录 `has_examples=false` 且可从状态/日志取到该清单；③ 多向量缓存写入后二次加载不调 embedder；④ 旧结构（单向量）缓存不兼容时触发重建；⑤ 多向量构建部分失败时降级（单向量或 ready=False）不抛。
- [x] 2.2 实现（绿）：每 Skill 产出多向量段（description 一条 + 每条 example 一条），`search` 改为段内 max-sim 后全局 top-k；缓存存扁平向量矩阵 + `spans:[{skill,has_examples,count}]` 对齐；版本键升级使旧缓存失效重建；构建容错降级。验证：2.1 测试全绿。
- [x] 2.3 embedding 分批（先测红：`embed_texts` 传入 > 批大小文本时按 `batch_size` 分多次 POST、顺序与输入一致、单批失败按既有错误路径抛出/容错）；实现：`EmbeddingClient.embed_texts` 按 `embedding.batch_size`（默认 64）分批请求并按序拼接，索引构建侧千级文本不发单请求。验证：测试全绿且不依赖真实端点（mock transport 断言请求次数与批次）。

## 2b. BM25 关键词路与 RRF 混合检索（新增 skill_router/keyword.py，改 router.py/app.py）

- [x] 2b.1 先写测试（红）：① 分词：含数字/型号/缩写的消息（"订单 123 退款""SKU-8800""OA 审批"）token 完整保留、中文 bi-gram 正确、停用字被剔除（纯函数单测）；② 含精确符号但用词与示例描述差异大的消息，BM25 把对应 Skill 排到前列；③ BM25 与向量结果经 RRF 融合后双路共同支持的 Skill 排名高于单路；④ `hybrid=false` 时关键词索引不构建/不参与；⑤ embedding 未就绪（index.ready=False）时 BM25 仍返回候选；⑥ BM25 索引构建为纯本地（断言未调用 embedder/网络）。
- [x] 2b.2 实现（绿）：`keyword.py` 的 `KeywordIndex`（零依赖分词 + tf/df/avgdl 统计 + BM25 k1=1.5/b=0.75；每 Skill 多段、段内 max 聚合）；app 启动时随 registry 构建（内存，不落盘）；`router.py` 双路检索 + RRF 融合（`rrf_k` 配置），高置信阈值判定仍只用向量分，放行集保底并入 BM25 rank ≤ 3（`keyword_top_k`），低置信兜底 prompt 注入 BM25 命中。验证：2b.1 测试全绿。
- [x] 2b.3 `semantic` 入参与降级接线：`SkillRouter` 增 `semantic: bool`，`app.py` 按 `index.ready and not is_stub` 传入；`semantic=false` 时跳过向量路、规则后直接 BM25 收窄 → LLM 兜底（details 标 `degraded_keyword`/`semantic_off`）；BM25 也无结果才全量 fallback；**运行中单次 query embed 抛错同样先走 BM25 收窄 → LLM 兜底**（不直接全量，reason=query_embed_failed 进 details）。测试（红→绿）：stub 模式断言向量 embed 未被路由调用、BM25 收窄生效；embedding 故障断言不全量平铺；**降级 path 归类断言——BM25 收窄后 LLM 兜底成功点名/选域的轮次 path=`llm`（非 degraded）、details 带 `degraded_keyword=true`，LLM 无法定夺澄清 path=`clarify`、BM25 无结果才 path=`degraded`**。

## 3. 路由跨轮上下文与按需 query 改写（新增 skill_router/context.py，改 router.py）

- [x] 3.1 先写测试（红）：① 不触发改写时仅历史拼接构造向量 query、断言未调用改写 LLM；② 消息命中指代词（refer_terms）或拼接向量检索低置信时触发改写，fake 改写 LLM 返回独立 query 并用于重检、命中正确 Skill；③ 改写 LLM 异常/返回不可解析时静默降级为拼接 query、轮次正常；④ `context_turns=0` 或 `query_rewrite=false` 时仅用当前轮原文；⑤ **双检索控制流**：拼接低置信→改写→重检后达高置信时直接 vector 收窄、**未调用路由 LLM**（断言路由 LLM 调用次数为 0）；⑥ BM25 关键词路全程只对**当前消息原文**检索一次（断言改写/拼接不触发 BM25 重跑）。
- [x] 3.2 实现（绿）：新增 `context.py`（`build_context_query(history, message)` 拼接；`rewrite_query(llm, history, message)` 调 temperature=0 LLM 输出 `{query}`，失败抛出让路由降级）；`route()` 增 `history` 入参，按 design D2 控制流编排——规则匹配**当前轮原文**；BM25 对当前消息检索一次；向量用拼接 query 检索并与 BM25 RRF 融合判首次高置信门（未命中指代词且过门→vector 路径 0 LLM）；未过门且（指代词或低置信）且 `query_rewrite` 开→改写后 embed 重检、与同一 BM25 融合，重检高置信则直接 vector 收窄跳过路由 LLM，否则带融合候选进兜底；details 记录 `query_rewritten`。验证：3.1 测试全绿。

## 4. 澄清闭环、结构化选项与澄清状态持久化（router.py + chat 接线 + message_store/mysql_client）

- [x] 4.1 先写测试（红）：① 传入 `prev_clarify={categories:[订单,退款],turn_id:Tn}` 且当前回答指向"退款" → 收窄到退款相关工具、path 非 clarify；② 当前回答仍无法对应任一候选方向 → 再次澄清但 details 携带历史候选方向；③ 澄清轮持久化的 assistant 行带 `meta={"kind":"clarify","categories":[...],"options":[...]}`（FakeStore 断言 append 载荷含 meta，且 options value 带前缀）；④ chat 层从历史末条 assistant 行的 meta 构造 `prev_clarify`（含 options/turn_id；meta 缺失/NULL → None），`load_messages` 返回含 `meta`/`turn_id`；⑤ 存量行无 meta（NULL/无列）读取不报错（兼容）；⑥ 点选确定性收窄成功后调用 `update_clarify_selected`，上轮澄清行（turn_id=Tn）的 `meta.selected` 被回写为所选带前缀 value、已有 options 不被覆盖（FakeStore/假 pool 断言 UPDATE 定位键与载荷）。
- [x] 4.2 实现（绿，补齐 design D3 的 6 处存储断点）：
  - **DDL/迁移**：`mysql_client.SCHEMA_DDL` 建表语句加 `meta JSON NULL`（新库一步到位）；`init_schema` 查 `information_schema.columns`，缺列则惰性 `ALTER TABLE agent_message ADD COLUMN meta JSON NULL`（不依赖 `ADD COLUMN IF NOT EXISTS`，兼容低版本）。
  - **写 options**：`message_store.append_message(...)` 增 `meta: dict|None=None` 参数、INSERT 增 meta 列（`json.dumps`）；`run_turn` 与 `_produce` 在 `direct_reply` 之外并列增 `clarify_meta: dict|None=None` 参数，runner 澄清分支落 assistant 行时写 `meta=clarify_meta`（`{kind:"clarify",categories,options}`）；chat 同步段把 decision 的 categories/options 装入 clarify_meta 透传（不再只传 clarify_text）。
  - **读 prev_clarify**：`load_messages` 的 SELECT 补 `meta, turn_id`（喂图 `_row_to_message` 忽略多余键）；`route()` 增 `prev_clarify` 入参，存在时优先在候选 categories 内裁决；**chat 层**（非 runner）路由前 load 最近 `context_turns*2+1` 条历史，据末条 assistant 行 `meta.kind=="clarify"` 构造 `prev_clarify={categories,options,turn_id}` 传入 route（meta 缺失/NULL→None）。
  - **回写 selected**：新增 `message_store.update_clarify_selected(service,env,user,session_id,turn_id,selected)`——按归属键 + `turn_id` 定位上轮澄清行，`UPDATE ... SET meta=JSON_SET(COALESCE(meta,'{}'),'$.selected',%s) WHERE 归属键 AND turn_id=%s AND role='assistant'`（不覆盖 options、NULL meta 可写、归属 WHERE 防越权）；chat 同步段在选项确定性收窄成功后调用（turn_id 取自 prev_clarify）。
  - **历史回放**：`load_web_messages` SELECT 补 `meta`，消息 DTO 从 meta 解出 `options`/`selected` 透出（存量 NULL 行字段缺省、不报错）。
  验证：4.1 测试全绿。
- [x] 4.3 测试替身 FakeStore 改造（横切，单列，先于功能任务实施；不改则 4.1/4b.1/11.1 一写一读即撞 AttributeError）：`tests/conftest.py` 的 `FakeStore`——append 的行补存 `turn_id`/`meta`；`load_messages` 返回 dict 含 `meta`/`turn_id`；`load_web_messages` DTO 从行 meta 透出 `options`/`selected`；补 `update_clarify_selected` 的内存实现（按归属+turn_id 定位行、读-改-写合并 selected 到 meta）。验证：澄清落库/点选回写/历史回放 e2e 在 FakeStore 上跑通。
- [x] 4.4 结构化选项产出（先测红：① 澄清分支 decision.details 与 `RouteDecision.clarify_options` 含 `options:[{label,value}]`，value **统一带前缀**——LLM 候选 category → `category:<名>`、点名/融合 Skill → `skill:<名>`，`<名>` 全为 env 候选集内合法标识，一张卡片可混两种粒度，数量 ≤ `clarify_option_max`；② `clarify_options=false` 时不产出 options、回落纯文本澄清）；实现（绿）：澄清分支从 LLM 候选 category（`category:` 前缀）/点名 Skill 与向量+BM25 融合 top-N Skill（`skill:` 前缀）构造 options，label 取 category 显示名/Skill 短描述，挂 decision 与 details；runner 澄清分支把 options（带前缀 value）写入 meta。验证：测试全绿。
- [x] 4.5 选项回传确定性收窄（先测红：① 请求 `message`=所选项 label 且携带 `clarify_selection.value` 命中 prev_clarify.options 并经 env 合法 → 按前缀收窄：`category:<名>`→该域全部工具、`skill:<名>`→单个 Skill，断言**未调用**向量检索与路由 LLM、path 标记选项收窄、不再澄清，且 **label 作为 user 行 content 落库并进入 ReAct 历史、带前缀 value 不出现在 content**；② value 为空/前缀非法/不在 options/集外非法（含 `prev_clarify` 缺失却携带回传字段的情况，如点了旧卡片）→ 忽略标记回落 4.2 文本闭环（label 仍作普通消息参与）、不暴露集外工具；③ 无回传字段的手打消息（`message`=原话、无 `clarify_selection`）走文本闭环）；实现（绿）：`api/chat.py` ChatRequest 增可选 `clarify_selection:{value}` 并传入 route；router 识别回传后解析前缀定粒度、在候选交集内确定性收窄，非法回落。验证：测试全绿。

## 4b. 澄清选项 SSE 下发与历史回放（events.py + api/chat.py + api/sessions.py）

- [x] 4b.1 先写测试（红）：① 澄清轮事件流含 `clarify` 事件，data 带 `question`/`options:[{label,value}]`（value 带 `category:`/`skill:` 前缀）与公共 turnId/traceId/eventSeq，且该轮无工具事件、以 turn_end 收尾；② `clarify` 事件写入 ring buffer，`Last-Event-ID` 断线重连重放时含该事件与 options（`with_seq` 兼容）；③ 历史消息 API（`/sessions/{id}/messages`）回放的澄清消息 DTO 透出 options 与已选 `selected`，value 带前缀原样透出、分页边界不重不漏；④ 存量 meta=NULL 消息回放不报错；⑤ 点选请求 `message`=label 且 `clarify_selection.value`=带前缀值，经 ChatRequest 正确装配进路由（与 4.5 联调断言 label 落库、value 不进 content）。
- [x] 4b.2 实现（绿）：`events.py` 新增 `clarify` 事件类型（携带 question/options，走 `with_seq` 编号与 ring-buffer 回放）；runner 澄清分支在 turn_delta 文本后下发 clarify 事件；`api/sessions.py` 历史消息 DTO 从 meta 透出 options/selected；`api/chat.py` 解析 `clarify_selection` 传入路由。验证：4b.1 测试全绿（stub chat + 内存 FakeStore，不依赖前端）。

## 5. 兜底 LLM 三级分流与 confidence 生效（router.py）

- [x] 5.1 先写测试（红）：fake 路由 LLM 分别返回——① 高 confidence + 候选集内 skills=[A] → 工具集仅 {A}；② 中 confidence 或仅 category → 该域全部候选 Skill；③ skills 含候选集外非法名 → 丢弃非法名、按 category/澄清兜底；④ chitchat/unknown 分流不变。
- [x] 5.2 实现（绿）：`_classify` 输出扩展 `{skills, category, confidence, reason, clarify_question}`；按 `llm_conf_high` 三级分流；点名 skills 与 env 候选集取交集；confidence 显式参与判定（不再空转）。验证：5.1 测试全绿。

## 6. 缺参校验守卫 / 半槽位填充（skills/base.py 挂校验回调 + runner.py 事件识别）

- [x] 6.1 先写测试（红）：① Skill 带必填 args_schema，模型缺必填参数调用 → 框架校验失败**未调用** `skill.run`，回流的工具结果为 `ToolMessage(status="error")` 且 content JSON 含 `errorCode="MISSING_ARGS"` 与缺失字段名/引导语（经 `astream_events` 的 `on_tool_end` 事件观测，`out.status=="error"`，**不是** `on_tool_error`）；② 参数齐全 → 正常执行返回结果（业务 `run` 被调用）；③ `arg_guard=false` 时构造的工具**不安装**校验回调，缺参回落框架默认（异常经既有 `handle_tool_errors` 转普通错误消息、`errorCode` 为 `INTERNAL`、无缺参引导话术）。
- [x] 6.2 实现（绿）：落点**不在 `arun` 内**（探针证实框架 `_parse_input` 先于 `arun` 校验并 re-raise，`arun` 内守卫是死代码）——改为 `to_tool` 构造 `StructuredTool.from_function(...)` 时，按 `routing.arg_guard` 传 `handle_validation_error` 回调：回调收 `ValidationError`，从 `e.errors()` 提取 `type=="missing"`/非法字段名，**返回**（非抛出）`{"errorCode":"MISSING_ARGS","missing":[{"field","hint"}],"message":引导语}` JSON 字符串（构造期开关：`arg_guard=false` 不传此参数）；**同步改 `runner.py` 的 `on_tool_end` 分支**：现状写死 `"success"`，需识别 `isinstance(out,ToolMessage) and out.status=="error"`（或解析 content 内 `errorCode`）→ 发 `tool_end(status="error", error_code=...)`，业务异常 re-raise 仍走 `on_tool_error`（不变）；`agent.system_prompt` 补充"工具报缺参数时先向用户询问、勿编造参数"。验证：6.1 测试全绿。

## 7. 误杀比对：推荐集 vs 实际调用（runner.py + api/chat.py）

- [x] 7.1 先写测试（红）：① 收窄路径（vector/llm/rule/**option**）推荐集 {A,B}，模型实际调用集外工具 C → 调用误杀上报且 span/审计标注 `missed_tool=C`（option 路径：选项确定性收窄到 {B}，模型越界调 C 同样计误杀）；② 实际调用 A（集内）→ 不上报；③ degraded/fallback（全量）路径 → 不上报；④ **BM25 降级轮次（embedding 故障/stub，BM25 收窄后 LLM 兜底成功）模型调用集外工具 → 误杀照计**（path=llm、retrieval=keyword），不因降级豁免。
- [x] 7.2 实现（绿）：chat 装配把路由 `path` 与推荐工具名集合传入 `_produce`；runner 在工具调用事件中拿到实际工具名后比对，越界（path ∈ {rule,vector,llm,option}）则 `record_intent_miss(path, env, retrieval)`——retrieval 由 details 的 `degraded_keyword`/`semantic_off` 推得（keyword|vector）；chitchat/clarify/degraded/fallback/徒手作答不计。验证：7.1 测试全绿。

## 8. 可观测指标（observability.py）——横切，单列

- [x] 8.1 先写测试（红）：InMemory/假 meter 断言新增——`agent.intent.miss.count`（counter，标签 env/path/retrieval，path 含 rule/vector/llm/**option**、retrieval 含 vector/**keyword**）、`agent.intent.score` 与 `agent.intent.score_gap`（histogram，分数带检索路标签 vector/bm25/rrf）、`agent.intent.rewrite.count`（counter，标签 env/result=success|failed）、`agent.tool.missing_args.count`（counter，标签 env/tool）；路由路径计数 `record_intent_route` 的 path 标签可取值新增 **`option`**（选项确定性收窄，与 rule/vector/llm 同属收窄路径）；澄清率计数 `record_clarify` 增 `result=option|text|repeat` 维度（选项闭环/手打成功/再次澄清，由 chat 层据 router details 的 `clarify_outcome` 触发，与 `record_intent_route(path=option)` 是不同 counter）；**防双计断言**——`record_intent_route(path="clarify")` 不再自动累加澄清计数（移除 observability.py 内嵌 `if path=="clarify"` 分支），repeat 轮澄清计数仅经 `record_clarify(result="repeat")` +1 一次；**首轮澄清不计数断言**——无 `clarify_outcome` 的首轮澄清（path=clarify）不触发 `record_clarify`，其总量由 path 维度承担；断言所有标签无 userId/sessionId/turnId。
- [x] 8.2 实现（绿）：新增 `record_intent_miss` / `record_intent_score(top1,gap,route)` / `record_intent_rewrite(result)` / `record_missing_args(tool)`；`record_intent_route` 接受新 path 值 `option`，**删除其内嵌的 path=="clarify" 自动澄清累加分支**（改由显式 `record_clarify` 承担）；澄清计数 `record_clarify(result)` 支持 option/text/repeat，**仅当 details 带 `clarify_outcome`（存在上一轮澄清且产生闭环结局）时由 chat 层触发，首轮澄清不调用**；接线：router 在选项确定性收窄轮置 `path="option"` 并在 details 带 `clarify_outcome`（点选闭环=option / 手打候选内裁决成功=text / 仍无法对应再澄清=repeat），**chat 层**据 `decision.details["clarify_outcome"]` 调 `record_clarify(result)`（route counter 照常 `record_intent_route(decision.path)`）；误杀比对的收窄 path 集合含 `option`、miss counter 带 retrieval 标签；runner 侧缺参 MISSING_ARGS 经 6.2 改造后的 `on_tool_end` 分支识别记 `record_missing_args(tool)`，业务异常 re-raise 的 `on_tool_error` 路径不记缺参。验证：8.1 测试全绿。

## 9. 轮次接线与 health（runner.py / api/chat.py / api/health.py）

- [x] 9.1 接线修正（落点以 D1/D3 为准）：**chat 层**（`api/chat.py`，非 runner）在路由前 load 最近 `context_turns*2+1` 条历史（内存 FakeStore 可断言 load 调用），截取最近 N 轮 {role,content} 传入 `route(history=...)`、据末条 assistant meta 传入 `prev_clarify`；runner 喂图的全量历史加载与澄清 direct_reply 持久化（含 meta）行为不变；缺参 MISSING_ARGS 以错误 ToolMessage 回流 ReAct、模型下一轮追问、用户补齐后重试成功（两轮 e2e）。
- [x] 9.2 `/health` 的 `routing` 状态补充 `multi_vector`、`hybrid`（BM25 关键词路是否启用/就绪）、`semantic`（向量路是否语义可用）、`clarify_options`（结构化澄清选项是否开启）与 `has_examples=false` 的 Skill 数（可筛）；stub/降级模式标注正确（mode 体现 rule+keyword / rule-only）。验证：health 测试断言新字段。
- [x] 9.3 路由 details 可回放序列化（横切修复）：chat 层写 `intent_route` span 与审计前，将 list/dict 型 details（`top_k`/BM25/RRF 结果/`categories`/`clarify_options`/`clarify_selection` 等）显式序列化为紧凑字符串（如 `route.top_k="refund:0.81,order:0.77"`）。测试（红→绿）：断言 span attributes 与审计日志中含 top-k 工具名+分数（含 BM25/RRF 两路）与澄清选项，不再被标量过滤静默丢弃。

## 10. 文档与配置同步

- [x] 10.1 更新 README「意图识别与 Skill 路由」、`docs/00`（路由分级补充上下文/改写/闭环/多向量/BM25 混合/三级分流/缺参追问/degraded 关键词收窄；**SSE 协议章节新增 `clarify` 事件、`options:[{label,value}]` 字段与 `clarify_selection` 回传约定，标注 additive/旧端忽略降级**）、`docs/01`（落点与新 metric）、`docs/02`（前端侧仅说明协议已就绪、卡片渲染归 `structured-clarification-options`，不在本 change 实现）；Skill 接入规范强调 examples 对召回的影响。
- [x] 10.2 文档与代码一致性自检：配置键、模块名（context.py）、metric 名、`MISSING_ARGS` code、行为描述全部对齐。

## 11. 全量验证

- [x] 11.1 新增 e2e（stub chat + 确定性 fake embedding + 多域 DemoSkill）覆盖：指代消解经改写命中、澄清闭环不重复反问、**澄清产出 options 并下发 clarify 事件、选项回传确定性收窄（断言未走向量/LLM）、非法 value 回落文本闭环、断线重放与历史回放含 options**、缺参两轮追问补齐、多用法 Skill 多向量召回、收窄路径误杀上报、含订单号/型号消息经 BM25 关键词路命中、stub 无语义模式下 BM25 收窄且不发生向量伪高置信、**BM25 降级轮 LLM 兜底成功记 path=llm 且误杀以 retrieval=keyword 上报**，新路径全绿。
- [x] 11.2 确定性与回归：相同输入两轮路由工具集/path 一致；`routing.enabled=false` 与无 Skill 两情况下既有全部测试通过。
- [x] 11.3 运行 `openspec validate --strict intent-routing-context-quality` 与全量 `pytest`，确认真实输出 0 失败、validate 通过；**并执行 design↔spec 可追溯性核对**：逐条确认 design.md 每个 Decision 与每条 Risk 缓解都有对应 spec Scenario（关卡 A），逐条 grep 代码/测试确认落地（关卡 B）。
