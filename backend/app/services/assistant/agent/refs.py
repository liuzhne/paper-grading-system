"""引用编号与一次 Agent 回合的上下文（Agent 化方案 §11.1、§11.3）。

模型看不到数据库 ID：工具结果里的每个对象带一个引用编号（`t1` 任务、`p1` 材料、`r1` 评分标准、
`i1` 评分项），映射表只在代码里。模型传回的 `*_ref` 必须出现在本回合的工具结果里，或者是焦点里的
`t0`（当前任务）、`p0`（当前材料），否则拒绝——模型编不出不存在的对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from backend.app.services.assistant.context import RunContext

KINDS = {"t": "任务", "p": "材料", "r": "评分标准", "i": "评分项"}


class ToolError(Exception):
    """给模型看的错误：说清楚错在哪、下一步怎么办（§11.1），不含内部细节。"""


@dataclass
class TurnRefs:
    _by_ref: dict[str, tuple[str, str]] = field(default_factory=dict)
    _by_object: dict[tuple[str, str], str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)
    #: 编号 → 回复里引用它时显示的数据卡片（卡片只有类型与 ID，分数由界面现取）。
    _cards: dict[str, dict] = field(default_factory=dict)
    #: 本回合 `help_lookup` 返回过的帮助条目：回复的 help_refs 只能引用这些。
    help_seen: set[str] = field(default_factory=set)
    #: 本回合工具返回过的状态：(种类, 对象 ID, 类别)。回复里的状态说法要对得上它说的那个对象（claims）。
    evidence: set[tuple[str, str, str]] = field(default_factory=set)
    #: 工具结果里出现过的名称（任务名、材料名）→ 对象：判断回复里的状态说的是谁。
    names: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: 材料 → 所属任务：任务评完了，其中每一份也就评完了。
    parents: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    #: 本回合出过的确认卡片类型（提议工具）。
    proposed: set[str] = field(default_factory=set)
    #: 本回合工具结果的原文：回复里的说法原样出现在这里，也算工具返回过。
    seen_text: list[str] = field(default_factory=list)

    def name(self, kind: str, object_id: str, name: str | None, *, parent: tuple[str, str] | None = None) -> None:
        """登记工具结果里给出的名称（不分配编号）。"""

        key = (kind, str(object_id))
        if name:
            self.names[str(name)] = key
        if parent is not None:
            self.parents[key] = (parent[0], str(parent[1]))

    def saw_status(self, status: str | None, kind: str, object_id: str) -> None:
        from backend.app.services.assistant.agent.claims import category_of

        category = category_of(status)
        if category:
            self.evidence.add((kind, str(object_id), category))

    def add(self, kind: str, object_id: str, *, card: dict | None = None) -> str:
        key = (kind, str(object_id))
        if key in self._by_object:
            ref = self._by_object[key]
        else:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            ref = "%s%d" % (kind, self._counters[kind])
            self._by_ref[ref] = key
            self._by_object[key] = ref
        if card is not None:
            self._cards[ref] = card
        return ref

    def card_of(self, ref: str) -> dict | None:
        card = self._cards.get(str(ref or "").strip())
        return dict(card) if card else None

    def pin(self, ref: str, kind: str, object_id: str) -> None:
        """焦点里的对象用固定编号（`t0`、`p0`），每回合开头登记。"""

        key = (kind, str(object_id))
        self._by_ref[ref] = key
        self._by_object.setdefault(key, ref)

    def resolve(self, ref: str | None, kind: str) -> str:
        found = self._by_ref.get(str(ref or "").strip())
        if found is None or found[0] != kind:
            raise ToolError(
                "没有找到%s“%s”：只能用本回合工具结果里出现过的编号，或当前状态里的编号。" % (KINDS.get(kind, ""), ref)
            )
        return found[1]

    def kind_of(self, ref: str) -> str | None:
        found = self._by_ref.get(str(ref or "").strip())
        return found[0] if found else None

    def object_of(self, ref: str) -> tuple[str, str] | None:
        return self._by_ref.get(str(ref or "").strip())


def agent_role(principal) -> str | None:
    """目录按角色过滤用的角色。平台管理员与开发模式按组织管理员处理（与接口的守卫一致）。"""

    if principal.platform_role == "platform_admin" or principal.organization_id is None:
        return "org_admin"
    return principal.organization_role


@dataclass
class TurnContext:
    run: RunContext
    refs: TurnRefs = field(default_factory=TurnRefs)

    @property
    def role(self) -> str | None:
        return agent_role(self.run.principal)


__all__ = ["KINDS", "ToolError", "TurnContext", "TurnRefs", "agent_role"]
