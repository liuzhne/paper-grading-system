# Word 评分规范抽取与对应方案

状态：**分期 A 已实现，开关默认关闭**（2026-11-01），还没有用真实模板核对；分期 B–D 未实施。第 2 节的 10 项决定已由维护者确认；真实模板样本、验收通过线和真实模型评测预算还没有定。

边界：
- 本方案解决“Word 模板 → 评分规范 → 对应到评分项”这一段。评分规范和评分要求是一回事，都指 Word 模板解析出来的内容。
- **不改发布校验**（`rubrics/executable_validator.py`），**不改扣分细则起草**（`ai_rule_drafter.py` 与它的提示词）。
- 无数据库迁移，Alembic head 不变。
- 只覆盖“Excel + Word”双文件导入里的模板文档。只传 Word 的导入不变。

本方案取代 `docs/AI扣分细则起草改造方案.md` 的 P3（筛选词与批注）。那份方案的 P0–P2 暂停，不受影响。

---

## 1. 结论

现在把 Word 段落一条条送给 AI，AI 既看不到列表的引导句，也看不到表头和章节。而且有些内容根本没被读进来。

新流程先把内容读全，再由代码组成完整条目，最后交给 AI 判断：

```text
Word 文件
  │  第 1 层  读全内容（不用 AI）：补读图片、文本框、内容控件、嵌套表格、修订插入
  ▼
原文单元台账 source_ledger
  │  第 2 层  识别章节（不用 AI）：标题样式 > 大纲级别 > 文字序号 > 整段加粗
  ▼
章节树 outline
  │  第 3 层  组条目（不用 AI）：列表并入引导句、表格一行一条、批注并入所标注段落
  ▼
评分规范候选条目 requirement_items（只存单元 ID，不复制文字）
  │  第 4 层  AI 判断（最多两轮）：是否评分规范、对应哪个评分项；只输出标签
  ▼
AI 建议 requirement_judgments
  │  第 5 层  人工确认：按评分项查看，单列“未对应”
  ▼
台账 claimed_by 写入 T02.manual  ──→  起草读取（起草代码不变）
```

分四期发布，每期可单独回滚（第 11 节）。第一期不调模型、不加迁移，用开关隔离，对线上没有影响。

## 2. 已确认的决定

| # | 决定 | 放弃了什么 | 为什么 |
|---|---|---|---|
| D1 | 对应到评分项 | 对应到单条扣分规则 | 单条规则要到起草才生成，这一步还没有 |
| D2 | 用真实模板验证 | 只靠合成文档 | 第 5、6、7 节的启发式规则和批量大小都依赖真实版式 |
| D3 | 段落不拆；列表并入引导句；表格一行一条；批注并入所标注段落 | 让 AI 拆段；按单元格判断 | 拆段要改写或切分原文。允许一条对应多个评分项，代替拆段 |
| D4 | 条目保留原文 | AI 整理成规范说法 | 可追溯，不会被改走样 |
| D5 | 除标题、空行、页码、目录外全部送 AI | 继续用关键词预筛 | 预筛会漏掉没有信号词的规范，比如“接口文档应给出错误码说明” |
| D6 | 默认对应一个评分项，AI 最多建议两个，由人确认 | 只许对应一个；不限数量 | 代价是同一条规范可能在两个评分项里都被扣分，靠人工把关 |
| D7 | 对不上的进入“未对应”，起草前提示数量；不新增评分项，不新增发布阻断 | 自动新增评分项；对不上就阻断 | “模型不得新建评分项”是既定原则。规范没有分值，阻断会逼用户乱归类 |
| D8 | 第一版条目不建表，存在 `raw_parse_output` | 单独建表 | 见第 10 节 |
| D9 | 固定两轮，由代码取上下文 | AgentLoop（模型自己决定取什么） | 见第 8.5 节 |
| D10 | 图片保存并显示，模型只看到“此处有图片”和图题，纯图片条目转人工 | 第一版就把图片送模型 | LLM 三个适配器都没有图片输入，要先有样本证明值得做 |

## 3. 现状

### 3.1 核实过的问题

以下用合成文档实跑得出，没有查生产数据。文档是特意写来暴露问题的，真实模板不一定都有这些情况。

| 问题 | 结果 | 位置 |
|---|---|---|
| 纯图片段、段落和单元格里的图片 | 不进台账，没有提示 | `docx_adapter.py:65` 文字为空就跳过 |
| 文本框 | 文字丢失 | `paragraph.text` 不读 `w:txbxContent` |
| 内容控件 | 整块丢失 | `docx_adapter.py:59` 只遍历正文下的 `w:p`、`w:tbl` |
| 嵌套表格 | 丢失 | `cell.text` 不含嵌套表 |
| 修订插入 | 文字丢失，整段都是插入时整段丢失 | `paragraph.text` 不读 `w:ins` |
| 自动编号 | 序号不在文字里，列表身份也丢了 | `numPr` 没有读 |
| 大纲级别标题、“一、”“（一）”标题 | 被当成普通段落，章节路径为空 | `docx_adapter.py:22` |
| 列表项 | “分层设计”这种短语没有信号词，被标 context，引导句单独送 AI | `docx_extractor.py:84` |
| 表格单元 | 按单元格判断，没有同行文字和表头 | `docx_adapter.py:95` |
| 批注 | 直接标 consumed，不进待归类 | `pipeline.py:807` |

最关键的一点：**没读进来的内容不进台账，覆盖率门禁也就看不到。** 这和 `units.py` 文件头“漏登记的内容会在覆盖率报告里冒出来”的设计目标相反。

### 3.2 从未验证过的事

评分规范到评分项的对应准确率**从来没测过**。`eval/rubric_llm_eval.py` 只有 7 条标签用例，没有对应结果的指标。

### 3.3 现有约束

- AI 归类已经是后台任务（`ai_tasks/unit_classification.py`），条目在建任务时冻结，不能中途追加。
- 每批 3 条有历史原因：`DECISIONS.md` 记录过批次大了会超时，所以改批量要实测。
- 起草只读 `T02.manual`（`parse_state.assigned_rule_sources`），`segment` 可以带多个 `source_refs`（`ai_rule_drafter.py:325`）。
- 既定原则（`docs/评分规则解析模块重构方案.md` §3、§14）：LLM 只输出标签、不自动触发、固定流程不做 Agent、第一期不新增表。本方案全部沿用。

## 4. 第 1 层：读全内容

`load_docx` 加 `deep` 参数，只有模板文档且开关打开时为真。同一次遍历里读新内容，unit_id 算法只保留一份。

| 内容 | 新 unit_id | kind | 处理 |
|---|---|---|---|
| 段落和单元格文字 | 不变 | 不变 | 改用自己的取文字函数：包含 `w:ins`、`w:moveTo`、超链接、行内内容控件、公式；排除 `w:del`、`w:moveFrom`、文本框 |
| 文本框 | `docx:p[3]/txbx[0]`；单元格里是 `docx:tbl[0]/r1/c0/p[1]/txbx[0]` | paragraph | Word 同时写 `mc:Choice` 和 `mc:Fallback`，只取前者，否则同样的文字会出现两遍 |
| 内容控件 | `docx:sdt[0]/p[1]`、`docx:sdt[0]/tbl[0]/r0/c0`、`docx:sdt[0]/sdt[0]/p[0]` | paragraph / table_cell | 目录控件（`docPartGallery` 为 Table of Contents）不读；单元格里的控件算单元格文字 |
| 嵌套表格 | `docx:tbl[0]/r1/c0/tbl[0]/r0/c0` | table_cell | 递归读取 |
| 图片 | `docx:p[3]/img[0]`；单元格里是 `docx:tbl[0]/r1/c0/p[0]/img[0]` | **image（新增）** | 文字写 `[图片]` 或 `[图片：替代文字]`，因为 `SourceLedger.register` 不接受空文字。同时读 `a:blip` 和旧版 `v:imagedata`，`mc:Fallback` 里的不重复。`context.image` 存哈希、类型、大小和 part 名，文件本身分期 B 再存 |

约束：

1. **已有 unit_id 不变。** 段落和表格的计数方式不动，`deep=False` 时行为和现在完全一样。`structure_state.py:71` 用 `doc_role="rules"` 重新解析，不受影响。
2. **`kind` 和 `heading_path` 的判断不变。** `_paragraph_extraction` 要把“一、需求分析（10分）：说明”当成段落来抽评分项，把它改成标题会破坏现有抽取。章节结果单独存放（第 5 节）。唯一的差别来自修订插入：文字变了或整段插入的标题多出来，结果会跟着变。没有修订的文档，`deep` 开关前后完全相同（测试保证）。
3. **嵌套表格不进 `view.tables`。** 否则表格编号和 `_word_conflicts` 都会受影响。新内容放进新字段 `view.structure`。
4. **目录段落标 `structural`**，理由写“目录”。样式名为 `TOC N`、`TOC Heading` 或“目录 N”的段落现在可能因为信号词造成误阻断，比如“3.1 扣分规则……12”。手写目录（以 `...`、`…`、`···` 加页码结尾）同样处理。单元 `context.toc` 记下这一点，目录行也不进 `view.blocks`，否则“创新性（5分）……3”会被当成 Word 独有的评分项，造成假冲突。

### 读取完整性自检

导入时按字符比较 `word/document.xml` 里的文字（去掉删除内容、`mc:Fallback` 和目录控件）和台账里的文字。对不上就写一条 `TEMPLATE_READ_INCOMPLETE` 警告，带缺失字数和样例。

页眉、页脚、脚注和尾注第一版不读，但统计字数写进同一条警告；图表、SmartArt 和嵌入对象（`c:chart`、`dgm:relIds`、`w:object`）的内容在别的 part 里，第一版也不读，统计个数。这样以后再有读不到的结构，也不会再是静默丢失。比如用内容控件包住表格行（`w:tbl/w:sdt/w:tr`），python-docx 读不到，现在会进警告。

结果存在 `requirement_items.read_report`。按确认的结构重新解析时文件不再读一遍，警告从这里补回去。

## 5. 第 2 层：识别章节

新模块 `rubric_import/requirements/outline.py`，纯函数，不用 AI。按顺序判断，先命中的算数：

| 优先级 | 依据 | 层级 | `source` |
|---|---|---|---|
| 1 | `Title`、`Heading N`、`标题 N` 样式 | N（`Title` 为 0） | `style` |
| 2 | 段落或样式继承的 `w:outlineLvl` | 值 + 1 | `outline` |
| 3 | `第N章` / `第N篇` / `第N部分` | 1 | `text_number` |
| 3 | `第N节` | 2 | `text_number` |
| 3 | `一、` | 1 | `text_number` |
| 3 | `（一）`：前一段不是引导句，也不是同类序号 | 2 | `text_number` |
| 3 | `1 概述`（数字、空格、文字；后面是“个、天、人”等量词的不算） | 1 | `text_number` |
| 3 | `1.1`、`1.1.1` | 点数 + 1 | `text_number` |
| 3 | `1、` / `1.`：前一段不是引导句，前后都不是同类序号，后面还有内容 | 3 | `text_number` |
| 3 | 整段就是“摘要、引言、绪论、结论、总结、参考文献、致谢” | 1 | `keyword` |
| 4 | 整段加粗（含样式继承）、不超过 30 字，下一段不加粗或是表格 | 4 | `bold` |

第 3、4 项的前置条件：不超过 40 字、不以句末标点结尾、没有自动编号。文本框不算标题。

**标题不能变成绕过门禁或漏掉规范的漏洞：**
- 文字里有分值（`score` 信号，如“三、格式错误扣 5 分”）的，第 3、4 项不算标题，留作正文。
- 带任何规则信号（分值、扣分动词、规范用语，如“三、扣分规则”“3.2 接口须统一编码”）或批注、图片的标题，算标题，同时生成一条 `heading` 条目。前者否则一直是待处理的阻断项，用户却没有地方处理；后者本身可能就是一条规范，不能因为被当成标题就消失。“需求”里的“需”不算规范用语。
- 没有阻断信号的标题，台账标 `context`。

第一版没有人工修改章节的功能。章节错了只影响 AI 的上下文和显示，不影响原文、覆盖率和门禁，人工可以直接改对应结果。如果样本显示错误很多，再加修改功能或 AI 兜底。

存储：

```json
"outline": {
  "version": 1,
  "nodes": [
    {"section_id": "S3", "parent_id": null, "level": 1, "title": "三、接口设计要求",
     "unit_id": "docx:p[7]", "source": "style"}
  ]
}
```

## 6. 第 3 层：组条目

新模块 `rubric_import/requirements/items.py`，纯函数，不用 AI。

| kind | 组法 | 拿不准时 |
|---|---|---|
| `paragraph` | 一段一条；文本框一个一条。没有引导句的序号段各自成条，它们通常是各自独立的要求 | |
| `list` | 引导句（以 `：` 结尾，或含“如下”“以下”“下列”）加连续的列表项。列表项看同一 `numId` 或同类文字序号 | 超过 8 项时每项单独成条，引导句作上下文，否则一条会同时对应太多评分项 |
| `table_row` | 一行一条，第一行作表头。嵌套表格的行排在所在行后面。全是噪声的行排除 | 第一行有 `score` 信号、整行合并成一格、含图片或嵌套内容时不算表头；单列表格没有表头 |
| `comment` | 并入所标注段落或表格行的条目（用 `commentRangeStart` 定位）；标在标题上的生成 `heading` 条目 | 标在表头或目录上的就地单独成条；定位不到的排在最后，带标注的原文 |
| `figure` | 纯图片段并入相邻图题（`图 1`、`Figure 1`）或它前面的引导句。图文同段的图片直接属于那一段 | 都没有时单独成 `image` 条目 |
| `heading` | 见第 5 节 | |

存储。条目只存有序的单元 ID，不复制文字，显示和送模型时都从台账取，所以原文不可能被改写：

```json
"requirement_items": {
  "version": 1,
  "items": [{
    "item_id": "R-3fa91c0a2b",
    "kind": "table_row",
    "section_id": "S3",
    "unit_ids": ["docx:tbl[0]/r2/c1", "docx:tbl[0]/r2/c2"],
    "context_unit_ids": ["docx:tbl[0]/r0/c0", "docx:tbl[0]/r0/c1", "docx:tbl[0]/r0/c2", "docx:tbl[0]/r1/c0"],
    "comment_unit_ids": [],
    "image_unit_ids": [],
    "header_unit_ids": ["docx:tbl[0]/r0/c0", "docx:tbl[0]/r0/c1", "docx:tbl[0]/r0/c2"],
    "prefill": null,
    "exclusion": null
  }],
  "table_headers": ["docx:tbl[0]/r0/c0", "docx:tbl[0]/r0/c1", "docx:tbl[0]/r0/c2"],
  "excluded": [{"unit_id": "docx:p[0]", "reason": "目录"}],
  "read_report": {"body_chars": 331, "missing_chars": 0, "missing_sample": "", "unread_parts": {"页眉": 12}, "unread_objects": 0}
}
```

- `item_id` 由 kind 和单元 ID 算哈希，同一文件重新解析结果相同。
- `context_unit_ids` 放表头、纵向合并的单元格和超长列表的引导句，只作上下文，不算这条拥有。`header_unit_ids` 只有表格行才有，是其中的表头，渲染时当标签用。
- `prefill`：Word 表格行按编号或名称精确匹配 Excel（已有 `T02.word_evidence`）时预填为 `{"criterion_codes": ["T02"], "source": "word_evidence"}`，不送 AI，仍要人工确认。按确认的结构重新解析时重算。
- `exclusion`：人工判定“不是评分规范”时写入（第 9.2 节）。归入结果不存这里，只存在台账，避免两处数据。

**不变量（测试保证）：** 每个 Word 单元只能属于一处：某条的 `unit_ids` / `comment_unit_ids` / `image_unit_ids`，或者章节标题，或者表头，或者带理由的排除项（目录、噪声、总分说明）；条目顺序和文档顺序一致。

不变量用 150 份随机文档测试（`test_requirement_items.py`）。`heading` 条目的单元同时也是章节标题，这是唯一的例外。

文本化规则也在这里统一实现，页面和起草共用：表格行渲染成“检查项：错误码；要求：须统一编码”，列表逐行显示，批注写成“（批注：…）”。这些只加结构标记，不改原文。

## 7. 批注和门禁

批注的台账状态保持现状（`consumed`，`template_items.WORD-COMMENT-n`），同时进入条目。

这样做是因为 `coverage.py` 把 `comment` 列为阻断信号。如果把批注改回 `unclaimed`，每条批注都会变成新的发布阻断，违反 D7。

门禁的实际变化只有两处，都只影响打开开关之后的新导入：
- **新增阻断：** 文本框等新读到的内容里写了分值或扣分动词，会变成发布阻断。这是应有的行为，以前只是看不见。图片不算：`[图片：错误码扣分表]` 只是替代文字，图片单元一律标 `context`。
- **减少阻断：** 目录、被识别成标题且没有阻断信号的段落、表头（比如“扣分标准”这一格）不再是待处理项。带分值的第一行不算表头，所以不会被这条放过。

Word 和 Excel 冲突的单元（`keep_unclaimed`）不受这些标记影响，仍然要由人处理。

## 8. 第 4 层：AI 判断

新任务种类 `requirement_judgment`（`ai_tasks/requirement_judgment.py`），提示词版本 `rubric-requirement-judge@1`，同时升 `llm_cache.PROMPT_VERSION`。旧的 `unit_classification` 保留给旧草稿。

### 8.1 送哪些条目

送：除下面几类以外的所有条目。

不送：`prefill` 条目、纯图片条目（转人工）、已归入或已排除的条目。单次最多 500 条，超过时在页面按章节选范围。

### 8.2 第一轮

按章节顺序分批，默认每批 3 条。章节切换时尽量另起一批。

```json
{
  "criteria": [{"code": "T02", "name": "总体架构", "max_score": 10, "description": "…",
                "evidence_hints": ["架构图"], "deduction_rules": ["架构不清扣分"]}],
  "items": [{"item_id": "R-…", "kind": "list", "section_path": ["二、总体架构要求"],
             "text": "总体架构须包括以下内容：\n1、分层设计\n2、部署拓扑",
             "table_header": [], "comments": [], "has_image": false}]
}
```

```json
{"items": [{"item_id": "R-…", "label": "requirement", "criterion_codes": ["T02"],
            "confidence": "high", "needs_context": false, "reason": "要求架构文档包含的内容"}]}
```

- 评分项说明和规则每项截到 300 字，条目超过 1500 字截断并注明。模型只输出标签，截断不影响原文。
- `label` 沿用 `rule` / `requirement` / `context` / `noise`，前两种都算评分规范。
- 输入是不可信数据，提示词沿用现有声明。
- 沿用归类的 JSON 模式，**不传带枚举的复杂 Schema**（#72 的教训：Gemini 会报通用 400）。

代码校验：`item_id` 属于本批且不重复；标签和置信度合法；`criterion_codes` 都是已有编号、不超过 2 个、不重复；`context` / `noise` 不能带编号；`reason` 不为空。不合格的条目记入 `rejected`，可重试。

失败处理沿用 `unit_classification`：429 和熔断延后，超时重试 1 次，输出不合格修正 1 次。

### 8.3 第二轮

只处理第一轮的这些条目：`confidence=low`、`needs_context=true`，或者判定为规范但没有对应评分项且不是高置信。

代码补充：同章节前后各一条（各 300 字内）、同级章节标题、第一轮的结论和理由。第二轮不再输出 `needs_context`。仍然不确定的直接交给人工，不再有第三轮。

### 8.4 第二轮是用户点击启动的

第一轮结束后，页面提示“还有 N 条缺上下文，可以再判一次（约 M 次调用）”，点击后建第二个任务。

没有做成自动接力，有三个原因：任务框架的条目建任务时就冻结了；既定原则是“LLM 不自动触发”；费用对用户可见。用户不点也不会损失什么，这些条目直接走人工。

### 8.5 为什么不用 AgentLoop

- 费用和调用次数有上限，结果可复现，能沿用现有任务队列、重试和计量。
- `assistant/agent/turn.py` 按对话回合设计（每回合最多 8 次工具调用、60 秒），每条目一个回合既贵又慢。
- `docs/评分规则解析模块重构方案.md` §3 已定“固定流程，不做 Agent”。

**什么时候再考虑：** 评测显示错误主要来自代码想不到的上下文（比如要读另一章节的定义），且第二轮修不好。

### 8.6 存储

写入 `raw_model_output.requirement_judgments`，形状和 `unit_classifications` 相同，用同样的“锁行 → 指纹校验 → 合并”。指纹包含提示词版本、条目内容和评分项。评分项变化后结果标记过期。`MODEL_OUTPUT_KEYS` 和 `LEDGER_KEYS` 要加上新键，否则重新编译时会丢。

### 8.7 费用（粗算，未实测）

按 200 条、20 个评分项、`gemini-2.5-flash` 单价（`eval/assistant_eval.py:56`，输入 0.30、输出 2.50 美元/百万 token）、第二轮占 20% 估算：

| 每批条数 | 第一轮调用次数 | 一份模板约花费 |
|---|---|---|
| 3 | 67 | 0.13 美元 |
| 10 | 20 | 0.08 美元 |

评分项说明在每批都重复一遍，所以批量对费用的影响没有调用次数那么大。第一版用 3 条，分期 C 再测 6 条和 10 条的超时和准确率。

## 9. 第 5 层：人工确认

### 9.1 页面

新组件 `RequirementReviewPanel.vue`，放在现有的“待归类原文”入口。

- **三个视图：** 按章节、按评分项、未对应。
- **筛选：** 必须处理（含阻断信号）、AI 已建议、低置信、已处理。
- **操作：** 单条归入（1–2 个评分项）、不是评分规范、恢复；一次采纳全部高置信建议；AI 判断；补上下文再判。
- **图片：** 显示预览。纯图片条目标“需要人工查看”，并提示“图片内容不会进入 AI 起草，如有要求请手动录入”。
- **Excel 未识别的内容：** 继续用 `SourceReviewPanel`，只传 Excel 单元。
- **起草前提示：** 评分项页面显示“还有 N 条评分规范未对应”（D7）。“归入的原文要求”改按条目显示，“移出”也按条目。

### 9.2 写入

`POST /api/rubrics/{id}/requirement-items/resolve`，一次请求可以带多条决定，整体原子：

| 动作 | 台账 | 条目 |
|---|---|---|
| `assign` | `unit_ids`、`comment_unit_ids`、`image_unit_ids` 全部写 `T02.manual` | 清除 `exclusion` |
| `not_requirement` | 只把 `unclaimed` 的单元改成 `context`；已被其他来源认领的不动 | 写 `exclusion` |
| `restore` | 移除 `.manual`（可指定评分项）；没有其他认领时按导入规则重算状态 | 清除 `exclusion` |

- `not_requirement` 不能用 `_override_status`，它会清掉批注的 `template_items.*` 认领。
- 写之前用和 `merge_unit_classification` 相同的方式锁行。现在的 `resolve_units` 没有锁，整个 JSON 一起覆盖，一键采纳和并发操作时可能互相覆盖。
- 每条决定追加到 `human_changes`，保留审计记录。
- 有条目的草稿，Word 单元的单元级写接口返回 409，避免一条规范只归入一半。Excel 单元不受影响。

### 9.3 交给起草

**这是本方案唯一碰到起草入口的地方。** `parse_state.assigned_rule_sources` 在草稿有条目时按条目合并：

```python
{"text": "检查项：错误码；要求：须统一编码", "source_refs": ["docx:tbl[0]/r1/c0", "docx:tbl[0]/r1/c1"], "reason": "assigned_source"}
```

- 不合并的话，起草拿到的是“错误码”“须统一编码”这样的碎片，`_draft_batches` 还可能把它们切到不同批次，前面做的组装就白做了。
- 返回的形状没变，起草代码和提示词都不用改，没有条目的旧草稿结果也完全不变。
- 纯图片条目不进入起草。

## 10. 存储

| 内容 | 位置 | 改动 |
|---|---|---|
| 原文单元 | `raw_parse_output.source_ledger` | 新增 image 类型和新单元 |
| 章节树 | `raw_parse_output.outline` | 新增 |
| 条目 | `raw_parse_output.requirement_items` | 新增 |
| AI 建议 | `raw_model_output.requirement_judgments` | 新增 |
| 人工决定 | 台账 `claimed_by`、条目 `exclusion`、`human_changes` | 沿用 |
| 图片 | 文件存储 `rubrics/{id}/template-images/{sha256}.{ext}` | 新增 |
| 原文件 | 不变 | 不变 |

### 10.1 条目为什么不建表

实测（Mock 数据、内存 SQLite，**没有测 Postgres**）：

| 正文段数 | `raw_parse_output` | 读覆盖率 | 归入一条 |
|---|---|---|---|
| 200 | 217 KB | 10 ms | 7 ms |
| 1000 | 995 KB | 29 ms | 24 ms |
| 3000 | 2.9 MB | 80 ms | 134 ms |

约每单元 1 KB，其中约 45% 是 `coverage` 里重复的内容。这个规模下，性能不是建表的理由。而建表有实际成本：

- 起草读台账里的 `T02.manual`，建表后有两处数据要保持一致。
- 每次重新编译生成新的 `RubricCompilation`，表里的行也得跟着复制或改外键。
- 新表要给 `pgs_app` 授权并建 RLS，漏了本地和 CI 都测不出来。

不建表的代价：整个 JSON 一起读写，所以写入必须加锁（第 9.2 节）；也不能跨评分标准查询。

**改建表的条件（任一满足）：** 真实模板超过约 3000 个单元；Postgres 上一次归入的 p95 超过 500 ms；需要跨评分标准查询条目。条目的读写都收在 `requirements/state.py` 里，到时只改这一个模块。

如果要先减负，优先处理重复的 `coverage`（有 `rubrics.py:1081` 和 `import_sessions.py:102` 两处读取，不能直接删），比建表便宜。

### 10.2 图片

- **写入时机：** `persist_prepared_import` 确认导入时写。导入前的临时草稿不写，否则过期后会留下孤儿文件。
- **失败处理：** 现有原文件写入的 `except: pass` 不能照抄。写失败时单元标 `stored=false` 并写警告，导入继续。
- **读取：** `GET /api/rubrics/{id}/source-units/{unit_id}/image`，用 `_visible_rubric` 守卫，`private, no-store`。
- **类型：** 只有 png、jpeg、gif、webp 内联显示。emf、wmf 浏览器显示不了；svg 有脚本风险。这些都提示“请在 Word 中查看”。一律带 `nosniff`。

### 10.3 原文件

这轮不动。现状是两处都有：`rubric_import_sessions` 的二进制列，加上确认导入时写一份到 `rubrics/{id}/artifacts/`（失败被吞掉，目前也没有代码读它）。本方案不依赖原文件，整理它们可以单独做。

## 11. 兼容与回滚

- **开关：** `REQUIREMENT_ITEMS_ENABLED`，默认关闭，只决定新导入走不走新流程。关闭时 `deep=False`，行为和现在完全一样。分期 D 才接进 `scripts/production_env.py` 和部署流水线（方式和 `SITE_LEADS_ENABLED` 相同）：分期 B 之前打开只会多出新单元，没有页面处理它们，所以现在生产打不开。
- **旧草稿：** 没有 `requirement_items`，继续用旧面板和 `unit_classification`。不做回填，要用新流程就重新导入。
- **按确认的结构重新解析：** 只看草稿有没有条目，不看开关。条目沿用，目录和标题的标记、预填和读取警告重算，因为这条路径的台账状态是新生成的。
- **回滚：** 关掉开关。已用新流程的草稿仍然按它们的数据显示，不会坏。没有迁移，不存在降级问题。
- **接口：** 新增端点和 AI 任务种类，升 `API_CONTRACT_VERSION`，前端 `api/contract.js` 同步。

## 12. 分期与验收

### 分期 A：读全、章节、条目（不调模型，线上无感）

**已实现（2026-11-01），真实模板还没有核对。**

- 改 `docx_adapter.py`、`units.py`，新增 `sources/docx_deep.py`、`requirements/outline.py`、`requirements/items.py`、`requirements/state.py`，只读脚本 `python -m backend.app.scripts.inspect_requirement_items <docx> [--rules <xlsx>] [--json]` 打印章节树、条目、排除项和会阻断发布的单元。
- 测试：
  - 每种内容类型的读取；`w:del` 不进台账；`mc:Fallback` 不重复；目录被排除。
  - 同一文件 `deep=False` 和 `deep=True` 的已有 unit_id 相同。
  - 文字完整性：`document.xml` 的文字全部进台账。
  - 第 6 节的不变量，用随机生成的文档序列测试。
  - 开关关闭时全套现有测试不变。
- **用真实模板跑脚本，人工核对。** 这一步决定第 5、6 节的规则要不要调。

### 分期 B：AI 判断与人工确认（只用假模型测试）

- 后端：判断任务、状态模块、三个新接口、`assigned_rule_sources` 合并。
- 前端：`RequirementReviewPanel.vue`、`RubricsView.vue` 接线。
- 测试：
  - 校验规则，包括模型编造编号、超过两个编号、输出原文。
  - 两轮的选择范围；任务重试和延后；过期判断。
  - `assign` / `not_requirement` / `restore` 的台账结果，批注认领不被清除。
  - 并发确认不互相覆盖；发布门禁结果符合第 7 节。
  - 起草输入按条目合并，旧草稿不变。
  - 前端单元测试、OpenAPI 合同、Playwright 走完整流程。

### 分期 C：真实模型评测（需要维护者单独授权和预算）

- `backend/app/eval/requirement_mapping_eval.py` 和 `scripts/run_requirement_mapping_eval.py`，必须传 `--budget-usd`，超出即停。
- 标准答案和真实模板放在仓库外，用参数传路径，**不提交真实模板**。
- 比较每批 3、6、10 条。预计总花费不超过 1 美元（待授权）。

| 指标 | 算法 | 建议通过线（跑完基线后确认） |
|---|---|---|
| 读取完整性 | 文字进台账的比例 | 100% |
| 章节识别 | 对标准答案的准确率和召回率 | 90% |
| 条目边界 | 和人工分组一致的比例 | 90% |
| 规范召回 | 真规范被判成规范的比例 | 95% |
| 对应准确率 | 真规范的第一候选评分项正确的比例 | 80% |
| 高置信准确率 | 高置信建议中完全正确的比例 | 95%（一键采纳依赖它） |

高置信准确率达不到时，先关掉一键采纳，其他照常。

### 分期 D：上线

打开生产开关，用一份真实模板走完“导入 → 判断 → 确认 → 起草”。同步三份文档，见第 14 节。

## 13. 风险与未验证

| 风险 | 影响 | 处理 |
|---|---|---|
| 启发式规则不适合真实版式（比如全用“1.”作标题） | 章节错、列表合错 | 分期 A 用真实模板核对；错误只影响上下文，不影响原文 |
| 引导句判断过宽，把普通段落和后面的序号合并 | 条目过大，对应不准 | 只认结尾冒号和三个固定词；超过 8 项不合并 |
| 全部送 AI 后调用次数变多 | 费用和等待增加 | 页面先显示调用次数；单次 500 条上限 |
| 每批 3 条仍可能超时 | 部分批次失败 | 已有重试和超时设置；失败不影响其他批次 |
| 同一规范对应两个评分项 | 可能重复扣分 | 人工确认；AI 最多建议两个 |
| 图片里的规范没有进入起草 | 该规范不生效 | 页面明确提示；样本显示常见时加图片输入 |
| 文本框内容多为装饰文字 | 噪声变多 | 分期 A 用样本看 |

**还没验证：** 真实模板、真实模型、Postgres 上的读写耗时。第 8.7 节的费用只是估算。

条目的 JSON 增量已用合成文档实测，没有查生产数据（Mock、不连数据库，每段一条）：

| 正文段数 | `raw_parse_output`（开关关 → 开） | 其中章节 / 条目 | 导入解析耗时（开关关 → 开） |
|---|---|---|---|
| 200 | 144 KB → 186 KB | 1 KB / 40 KB | 102 ms → 69 ms |
| 1000 | 691 KB → 899 KB | 6 KB / 201 KB | 452 ms → 305 ms |
| 3000 | 2065 KB → 2691 KB | 19 KB / 606 KB | 1353 ms → 875 ms |

约每条 200 字节，增加 30%。开关打开反而更快，因为深度读取按样式 ID 缓存了样式，而现有路径每段都走一次很慢的 `paragraph.style`。

## 14. 落地时要同步

- `ARCHITECTURE.md`：新的 `requirements/` 模块、`requirement_judgment` 任务、数据流。
- `DECISIONS.md`：第 2 节的 10 项，每项写放弃了什么；更正 `docs/评分规则解析模块重构方案.md` §5.2 与代码的偏离（批注送 LLM）。
- `RUNBOOK.md`：开关、脚本、评测命令；排错条目（比如“没有 AI 建议”“图片显示不了”）。
- `CLAUDE.md`：架构一节的 `rubric_import/` 说明。
- 三份文档的维护记录表追加同一日期、同一主题的条目。
