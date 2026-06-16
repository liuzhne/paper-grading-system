from backend.app.services.calibration.curate import anchor_rationale
from backend.app.services.calibration.curate import desensitize
from backend.app.services.calibration.curate import select_anchor_targets


def _rows():
    # 6 篇，R03 分布 10..28；filename 仅作 key（无 PII）。
    return [
        {"filename": "p10", "total": 60, "items": {"R03": 10}},
        {"filename": "p15", "total": 65, "items": {"R03": 15}},
        {"filename": "p19", "total": 70, "items": {"R03": 19}},
        {"filename": "p22", "total": 75, "items": {"R03": 22}},
        {"filename": "p25", "total": 80, "items": {"R03": 25}},
        {"filename": "p28", "total": 88, "items": {"R03": 28}},
    ]


def test_select_spans_low_mid_high():
    targets = select_anchor_targets(_rows(), holdout_filenames=[], criterion_codes=["R03"])
    picked = targets["R03"]
    assert [a["level"] for a in picked] == ["低", "中", "高"]
    scores = [a["score"] for a in picked]
    # 低 < 中 < 高，覆盖量程
    assert scores[0] < scores[1] < scores[2]
    assert scores[0] <= 15 and scores[2] >= 25


def test_holdout_excluded():
    # 把高分档的两篇放入留出集 → 不得被选为锚点
    targets = select_anchor_targets(_rows(), holdout_filenames=["p28", "p25"], criterion_codes=["R03"])
    chosen = {a["filename"] for a in targets["R03"]}
    assert "p28" not in chosen and "p25" not in chosen


def test_deterministic():
    a = select_anchor_targets(_rows(), [], ["R03"])
    b = select_anchor_targets(_rows(), [], ["R03"])
    assert a == b


def test_no_anchor_picked_twice_for_one_criterion():
    targets = select_anchor_targets(_rows(), [], ["R03"])
    files = [a["filename"] for a in targets["R03"]]
    assert len(files) == len(set(files))


def test_desensitize_removes_name_and_id():
    text = "本文作者张三（学号22008020323）研究了系统设计。张三认为……"
    cleaned = desensitize(text, names=["张三"], ids=["22008020323"])
    assert "张三" not in cleaned
    assert "22008020323" not in cleaned
    assert "系统设计" in cleaned  # 正文内容保留


def test_desensitize_skips_short_tokens():
    text = "本系统的设计与实现"
    # 单字 token 不应误删正文
    assert desensitize(text, names=["李"]) == text


def test_rationale_mentions_score_and_level():
    r = anchor_rationale("解决策略与技术路线", 19, 30, "中")
    assert "19" in r and "30" in r and "中" in r
