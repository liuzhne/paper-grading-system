import pytest

from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.units import SourceUnit


def _unit(unit_id="xlsx:S!R1C1", text="选题意义", **kw):
    return SourceUnit(unit_id=unit_id, doc_id=kw.pop("doc_id", "rules"), doc_role=kw.pop("doc_role", "rules"),
                      kind=kw.pop("kind", "cell"), text=text, context=kw.pop("context", {}))


def test_registered_units_default_to_unclaimed():
    ledger = SourceLedger()
    ledger.register(_unit())
    assert ledger.status("xlsx:S!R1C1").status == "unclaimed"
    assert ledger.status("xlsx:S!R1C1").claimed_by == ()


def test_claim_marks_consumed_and_accumulates_fields_without_duplicates():
    ledger = SourceLedger()
    ledger.register(_unit())
    ledger.claim("xlsx:S!R1C1", "C01.name")
    ledger.claim("xlsx:S!R1C1", "C02.dimension")
    ledger.claim("xlsx:S!R1C1", "C01.name")
    state = ledger.status("xlsx:S!R1C1")
    assert state.status == "consumed"
    assert state.claimed_by == ("C01.name", "C02.dimension")
    assert state.extracted_by == "code"


def test_mark_requires_reason_for_structural_and_ignored():
    ledger = SourceLedger()
    ledger.register(_unit())
    with pytest.raises(ValueError):
        ledger.mark("xlsx:S!R1C1", "structural", reason="")
    with pytest.raises(ValueError):
        ledger.mark("xlsx:S!R1C1", "ignored_by_rule", reason=None)
    ledger.mark("xlsx:S!R1C1", "context")
    assert ledger.status("xlsx:S!R1C1").status == "context"


def test_mark_rejects_unknown_status_and_cannot_mark_unclaimed_or_consumed():
    ledger = SourceLedger()
    ledger.register(_unit())
    for status in ("bogus", "unclaimed", "consumed"):
        with pytest.raises(ValueError):
            ledger.mark("xlsx:S!R1C1", status, reason="x")


def test_consumed_takes_precedence_over_later_mark():
    ledger = SourceLedger()
    ledger.register(_unit())
    ledger.claim("xlsx:S!R1C1", "C01.name")
    ledger.mark("xlsx:S!R1C1", "structural", reason="表头")
    assert ledger.status("xlsx:S!R1C1").status == "consumed"


def test_register_rejects_duplicates_blank_text_and_bad_role_or_kind():
    ledger = SourceLedger()
    ledger.register(_unit())
    with pytest.raises(ValueError):
        ledger.register(_unit())
    with pytest.raises(ValueError):
        ledger.register(_unit(unit_id="x2", text="   "))
    with pytest.raises(ValueError):
        ledger.register(_unit(unit_id="x3", doc_role="other"))
    with pytest.raises(ValueError):
        ledger.register(_unit(unit_id="x4", kind="image"))


def test_unknown_unit_ids_raise_key_error():
    ledger = SourceLedger()
    with pytest.raises(KeyError):
        ledger.claim("missing", "C01.name")
    with pytest.raises(KeyError):
        ledger.mark("missing", "context")
    assert ledger.has("missing") is False


def test_units_preserve_registration_order_and_filter_by_document():
    ledger = SourceLedger()
    ledger.register(_unit("a"))
    ledger.register(_unit("b", doc_id="tpl", doc_role="template", kind="comment"))
    ledger.register(_unit("c"))
    assert [u.unit_id for u in ledger.units()] == ["a", "b", "c"]
    assert [u.unit_id for u in ledger.units(doc_id="rules")] == ["a", "c"]
    assert ledger.documents() == {"rules": "rules", "tpl": "template"}


def test_round_trip_through_plain_mapping():
    ledger = SourceLedger()
    ledger.register(_unit("a", context={"header": "评分项"}))
    ledger.register(_unit("b"))
    ledger.claim("a", "C01.name", extracted_by="llm")
    ledger.mark("b", "ignored_by_rule", reason="非首张规则表")
    restored = SourceLedger.from_mapping(ledger.to_mapping())
    assert restored.to_mapping() == ledger.to_mapping()
    assert restored.status("a").extracted_by == "llm"
    assert restored.unit("a").context == {"header": "评分项"}
