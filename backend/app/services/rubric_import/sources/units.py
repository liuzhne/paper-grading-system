"""与格式无关的原文单元与单元台账（解析重构方案 §4.1）。

单元是原文里的一个位置（单元格、段落、批注……），不是规则项；二者多对多。
所有单元登记后默认 ``unclaimed``，只有抽取器显式登记才会改变状态——
漏登记的内容会在覆盖率报告里冒出来，而不是被静默吞掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterator, Mapping

DOC_ROLES = ("rules", "template", "reference")
UNIT_KINDS = ("cell", "paragraph", "heading", "table_cell", "comment")
STATUSES = ("consumed", "context", "structural", "ignored_by_rule", "unclaimed")
_MARKABLE = ("context", "structural", "ignored_by_rule")
_REASON_REQUIRED = ("structural", "ignored_by_rule")
EXTRACTORS = ("code", "llm", "human")


@dataclass(frozen=True)
class SourceUnit:
    unit_id: str
    doc_id: str
    doc_role: str
    kind: str
    text: str
    context: Mapping[str, object] = field(default_factory=dict)

    def to_mapping(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "doc_id": self.doc_id,
            "doc_role": self.doc_role,
            "kind": self.kind,
            "text": self.text,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class UnitStatus:
    status: str = "unclaimed"
    claimed_by: tuple[str, ...] = ()
    reason: str | None = None
    extracted_by: str | None = None

    def to_mapping(self) -> dict:
        return {
            "status": self.status,
            "claimed_by": list(self.claimed_by),
            "reason": self.reason,
            "extracted_by": self.extracted_by,
        }


class SourceLedger:
    def __init__(self) -> None:
        self._units: dict[str, SourceUnit] = {}
        self._status: dict[str, UnitStatus] = {}

    def register(self, unit: SourceUnit) -> SourceUnit:
        if unit.unit_id in self._units:
            raise ValueError(f"duplicate unit id: {unit.unit_id}")
        if not str(unit.text or "").strip():
            raise ValueError("source unit text must not be blank")
        if unit.doc_role not in DOC_ROLES:
            raise ValueError(f"unsupported document role: {unit.doc_role}")
        if unit.kind not in UNIT_KINDS:
            raise ValueError(f"unsupported unit kind: {unit.kind}")
        frozen = SourceUnit(
            unit_id=unit.unit_id,
            doc_id=unit.doc_id,
            doc_role=unit.doc_role,
            kind=unit.kind,
            text=unit.text,
            context=MappingProxyType(dict(unit.context or {})),
        )
        self._units[unit.unit_id] = frozen
        self._status[unit.unit_id] = UnitStatus()
        return frozen

    def has(self, unit_id: str) -> bool:
        return unit_id in self._units

    def unit(self, unit_id: str) -> SourceUnit:
        return self._units[unit_id]

    def status(self, unit_id: str) -> UnitStatus:
        return self._status[unit_id]

    def units(self, *, doc_id: str | None = None) -> list[SourceUnit]:
        return [u for u in self._units.values() if doc_id is None or u.doc_id == doc_id]

    def documents(self) -> dict[str, str]:
        documents: dict[str, str] = {}
        for unit in self._units.values():
            documents.setdefault(unit.doc_id, unit.doc_role)
        return documents

    def __iter__(self) -> Iterator[SourceUnit]:
        return iter(self._units.values())

    def claim(self, unit_id: str, field_ref: str, *, extracted_by: str = "code") -> None:
        current = self._status[unit_id]
        if extracted_by not in EXTRACTORS:
            raise ValueError(f"unsupported extractor: {extracted_by}")
        claimed = current.claimed_by if field_ref in current.claimed_by else (*current.claimed_by, field_ref)
        self._status[unit_id] = UnitStatus(
            status="consumed",
            claimed_by=claimed,
            reason=None,
            extracted_by=current.extracted_by if current.status == "consumed" else extracted_by,
        )

    def mark(self, unit_id: str, status: str, *, reason: str | None = None, extracted_by: str = "code") -> None:
        current = self._status[unit_id]
        if status not in _MARKABLE:
            raise ValueError(f"cannot mark unit as {status}")
        if status in _REASON_REQUIRED and not str(reason or "").strip():
            raise ValueError(f"{status} requires a reason")
        if current.status == "consumed":
            return
        self._status[unit_id] = UnitStatus(status=status, reason=reason, extracted_by=extracted_by)

    def to_mapping(self) -> dict:
        return {
            "units": [
                {**unit.to_mapping(), **self._status[unit.unit_id].to_mapping()}
                for unit in self._units.values()
            ]
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SourceLedger":
        ledger = cls()
        for item in value.get("units") or []:
            ledger.register(
                SourceUnit(
                    unit_id=item["unit_id"],
                    doc_id=item["doc_id"],
                    doc_role=item["doc_role"],
                    kind=item["kind"],
                    text=item["text"],
                    context=item.get("context") or {},
                )
            )
            status = item.get("status") or "unclaimed"
            if status not in STATUSES:
                raise ValueError(f"unsupported unit status: {status}")
            ledger._status[item["unit_id"]] = UnitStatus(
                status=status,
                claimed_by=tuple(item.get("claimed_by") or ()),
                reason=item.get("reason"),
                extracted_by=item.get("extracted_by"),
            )
        return ledger
