"""工具目录与工具搜索（Agent 化方案 §11.4–§11.6）。

目录就是这里的登记表。**不在登记表里的函数，模型既搜不到也调用不了**；删除、归档、发布评分标准、
改连接这类操作根本不登记。

- 常驻工具的定义放进请求的 `tools`，内容、顺序随部署固定；
- 延迟工具不进 `tools`：模型先用 `tool_search` 搜（关键词，或 `select:名称`），搜到的完整说明作为
  工具结果出现在对话里，再经常驻的 `call_tool(name, arguments_json)` 调用（§12.3）；
- 检索是自写的 BM25：中文按单字与相邻两字切分，英文名按下划线切分；关键词与评测说法只服务索引
  与测试，不给模型看。

同一份登记生成：原生工具定义、给模型看的说明文本、搜索索引、登记表检查。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from functools import lru_cache
from typing import Any
from typing import Callable

from pydantic import BaseModel

#: 分组：名称前缀 → 给模型看的说明。系统指令里列出这张表，告诉模型可以搜什么。
GROUPS: dict[str, str] = {
    "help": "帮助：系统怎么用、每种卡片怎么填",
    "task": "评分任务：列表、进度、分数分布、失败与复核数量",
    "paper": "材料：定位某一份、得分与扣分条目",
    "review": "复核：需要人工复核的材料与评分项",
    "rubric": "评分标准：可用的已发布评分标准",
    "action": "提议动作：开始评分、导入评分标准、重试、取消（只生成确认卡片，由老师确认）",
}

KINDS = ("read", "proposal")
RISKS = ("low", "medium", "high")
DEFAULT_ROLES = frozenset({"org_admin", "teacher"})
MAX_RESIDENT_BUSINESS = 4
MAX_SEARCH_RESULTS = 5
#: 说明文本的长度上限（字符）。一般约 200 token，复杂的不超过 400 token（§11.4）。
MAX_DEFINITION_CHARS = 900


@dataclass(frozen=True)
class AgentTool:
    name: str
    group: str
    title: str
    purpose: str
    when: str
    params: type[BaseModel]
    returns: str
    kind: str
    risk: str
    resident: bool
    keywords: tuple[str, ...]
    eval_phrases: tuple[str, ...]
    run: Callable[[Any, BaseModel], dict]
    roles: frozenset[str] = DEFAULT_ROLES
    phase: str = "A0"
    example: str | None = None
    param_notes: dict[str, str] = field(default_factory=dict)


_REGISTRY: dict[str, AgentTool] = {}


def register(tool: AgentTool) -> AgentTool:
    if tool.name in _REGISTRY:
        raise ValueError("duplicate assistant tool %s" % tool.name)
    _REGISTRY[tool.name] = tool
    return tool


def registry() -> dict[str, AgentTool]:
    _ensure_loaded()
    return dict(_REGISTRY)


def _ensure_loaded() -> None:
    # 业务工具在 agent/business.py 里登记；按需导入，避免循环依赖。
    from backend.app.services.assistant.agent import business  # noqa: F401


def visible(role: str | None) -> list[AgentTool]:
    """当前用户能用的工具，按登记顺序。角色过滤只影响能搜到什么，不改变 `tools` 数组。"""

    return [tool for tool in registry().values() if role in tool.roles]


def resident_business() -> list[AgentTool]:
    return [tool for tool in registry().values() if tool.resident]


# --- 给模型看的说明 ---------------------------------------------------------------

def _clean_schema(node):
    """Pydantic 的 JSON Schema → 各家都收的子集：去掉 title，可空的参数只留非空类型。

    Gemini 的函数声明只支持 OpenAPI 的一个子集（§11.5），不出现 `anyOf … null`、`$ref`。
    """

    if isinstance(node, dict):
        node = dict(node)
        node.pop("title", None)
        if node.get("default", ...) is None:
            node.pop("default")
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            concrete = [item for item in any_of if not (isinstance(item, dict) and item.get("type") == "null")]
            if len(concrete) == 1:
                node.pop("anyOf")
                node = {**_clean_schema(concrete[0]), **{key: value for key, value in node.items()}}
        return {key: _clean_schema(value) for key, value in sorted(node.items())}
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    return node


def parameters_schema(params: type[BaseModel], notes: dict[str, str] | None = None) -> dict:
    schema = _clean_schema(params.model_json_schema())
    schema.pop("$defs", None)
    schema.setdefault("properties", {})
    schema["type"] = "object"
    for name, note in (notes or {}).items():
        if name in schema["properties"]:
            schema["properties"][name] = {**schema["properties"][name], "description": note}
    return schema


def definition(tool: AgentTool) -> dict:
    """给模型看的完整说明（§11.4）。四处渲染同一份：工具结果、加载记录、③ 的汇总、常驻定义。"""

    body = {
        "name": tool.name,
        "用途": tool.purpose,
        "何时用": tool.when,
        "参数": parameters_schema(tool.params, tool.param_notes),
        "返回": tool.returns,
        "类型": "提议：只生成确认卡片，并结束本回合" if tool.kind == "proposal" else "只读",
    }
    if tool.example:
        body["示例"] = tool.example
    return body


def render(value) -> str:
    """确定性序列化：同一份内容每次渲染成相同的字节（§12.2）。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def native_definition(tool: AgentTool) -> dict:
    description = "%s\n何时用：%s\n返回：%s" % (tool.purpose, tool.when, tool.returns)
    if tool.kind == "proposal":
        description += "\n只生成确认卡片，并结束本回合。"
    return {"type": "function", "function": {
        "name": tool.name, "description": description,
        "parameters": parameters_schema(tool.params, tool.param_notes),
    }}


def registry_fingerprint() -> str:
    """登记表的指纹：说明或参数一变就变（前缀身份的一部分）。"""

    payload = [definition(tool) | {"resident": tool.resident, "roles": sorted(tool.roles)} for tool in registry().values()]
    return hashlib.sha256(render(payload).encode("utf-8")).hexdigest()[:16]


# --- 检索 -----------------------------------------------------------------------

_CJK = re.compile(r"[一-鿿]+|[a-z0-9]+")
# 出现得太多、区分不了工具的单字。
_STOP_CHARS = frozenset("的了吗呢是我你他她它这那个一有在和与及或就都也还把被给让请吧啊")
FIELD_WEIGHTS = {"name": 3.0, "group": 2.0, "title": 2.0, "keywords": 2.0, "purpose": 1.0, "when": 1.0, "params": 0.5}
K1 = 1.2
B = 0.75
#: 只返回得分不低于第一名这个比例的工具：“评分”“哪些”这类常见字会把不相干的工具也带出来，
#: 多返回一个就多一段说明进上下文（§11.5）。0.75 由离线检索测试核对（评测说法命中率 ≥ 95%）。
RELATIVE_CUTOFF = 0.75


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for chunk in _CJK.findall(str(text or "").lower()):
        if "一" <= chunk[0] <= "鿿":
            tokens.extend(char for char in chunk if char not in _STOP_CHARS)
            tokens.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
        else:
            tokens.append(chunk)
    return tokens


def _fields(tool: AgentTool) -> dict[str, str]:
    schema = parameters_schema(tool.params, tool.param_notes)
    param_text = " ".join(
        "%s %s" % (name, spec.get("description", "")) for name, spec in schema.get("properties", {}).items()
    )
    return {
        "name": tool.name.replace("_", " "),
        "group": "%s %s" % (tool.group, GROUPS.get(tool.group, "")),
        "title": tool.title,
        "keywords": " ".join(tool.keywords),
        "purpose": tool.purpose,
        "when": tool.when,
        "params": param_text,
    }


@dataclass(frozen=True)
class _Index:
    names: tuple[str, ...]
    term_freqs: tuple[dict[str, float], ...]
    lengths: tuple[float, ...]
    doc_freq: dict[str, int]
    average_length: float


@lru_cache(maxsize=16)
def _index(names: tuple[str, ...]) -> _Index:
    tools = registry()
    freqs = []
    for name in names:
        weighted: Counter[str] = Counter()
        for field_name, text in _fields(tools[name]).items():
            for token in tokenize(text):
                weighted[token] += FIELD_WEIGHTS[field_name]
        freqs.append(dict(weighted))
    doc_freq: Counter[str] = Counter()
    for freq in freqs:
        doc_freq.update(freq.keys())
    lengths = tuple(sum(freq.values()) for freq in freqs)
    average = (sum(lengths) / len(lengths)) if lengths else 1.0
    return _Index(names, tuple(freqs), lengths, dict(doc_freq), average or 1.0)


def search(query: str, role: str | None, *, limit: int = MAX_SEARCH_RESULTS) -> list[AgentTool]:
    """关键词检索延迟工具（常驻工具本来就在 `tools` 里，不参与检索）。"""

    candidates = tuple(tool.name for tool in visible(role) if not tool.resident)
    if not candidates:
        return []
    index = _index(candidates)
    terms = set(tokenize(query))
    total = len(candidates)
    scored = []
    for position, name in enumerate(index.names):
        freq = index.term_freqs[position]
        length = index.lengths[position]
        score = 0.0
        for term in terms:
            tf = freq.get(term)
            if not tf:
                continue
            df = index.doc_freq.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / index.average_length))
        if score > 0:
            scored.append((-score, position, name))
    scored.sort()
    if not scored:
        return []
    floor = -scored[0][0] * RELATIVE_CUTOFF
    kept = [entry for entry in scored if -entry[0] >= floor]
    tools = registry()
    return [tools[name] for _score, _position, name in kept[: max(1, min(limit, MAX_SEARCH_RESULTS))]]


def select(names: list[str], role: str | None) -> tuple[list[AgentTool], list[str]]:
    """`select:名称1,名称2`：按名称直接加载；返回 (找到的工具, 找不到或无权使用的名称)。"""

    allowed = {tool.name: tool for tool in visible(role) if not tool.resident}
    found, missing = [], []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name in allowed and allowed[name] not in found:
            found.append(allowed[name])
        elif name not in allowed:
            missing.append(name)
    return found, missing


# --- 登记表检查（CI，不调用模型，§11.6） --------------------------------------------------

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


def registry_problems() -> list[str]:
    problems = []
    tools = registry()
    for tool in tools.values():
        where = tool.name
        if not _NAME.match(tool.name):
            problems.append("%s: 名称不合法" % where)
        if tool.group not in GROUPS or not tool.name.startswith(tool.group + "_"):
            problems.append("%s: 分组前缀不合法" % where)
        if tool.kind not in KINDS:
            problems.append("%s: 类型不合法" % where)
        if tool.risk not in RISKS:
            problems.append("%s: 风险等级不合法" % where)
        if tool.kind == "proposal" and tool.risk == "low":
            problems.append("%s: 提议工具至少是中风险" % where)
        if tool.kind == "read" and tool.risk != "low":
            problems.append("%s: 只读工具应为低风险" % where)
        if not (tool.title and tool.purpose and tool.when and tool.returns):
            problems.append("%s: 标题、用途、何时用、返回不能为空" % where)
        if len(tool.keywords) < 3:
            problems.append("%s: 关键词至少 3 个" % where)
        if len(tool.eval_phrases) < 2:
            problems.append("%s: 评测说法至少 2 条" % where)
        if len(render(definition(tool))) > MAX_DEFINITION_CHARS:
            problems.append("%s: 说明超过 %d 字符" % (where, MAX_DEFINITION_CHARS))
        if not tool.roles:
            problems.append("%s: 没有可见角色" % where)
    if len(resident_business()) > MAX_RESIDENT_BUSINESS:
        problems.append("常驻业务工具超过 %d 个" % MAX_RESIDENT_BUSINESS)
    return problems


__all__ = [
    "AgentTool", "GROUPS", "MAX_SEARCH_RESULTS", "definition", "native_definition", "parameters_schema",
    "register", "registry", "registry_fingerprint", "registry_problems", "render", "resident_business",
    "search", "select", "tokenize", "visible",
]
