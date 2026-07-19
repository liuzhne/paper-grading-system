from backend.app.eval import metrics
from backend.app.eval.runner import EvalPrediction
from backend.app.eval.runner import EvalSample
from backend.app.eval.runner import assert_no_regression
from backend.app.eval.runner import baseline_from_report
from backend.app.eval.runner import evaluate
from backend.app.eval import run_eval


def test_qwk_perfect_agreement_is_one():
    assert metrics.quadratic_weighted_kappa([0, 1, 2, 3, 4], [0, 1, 2, 3, 4], 0, 4) == 1.0


def test_qwk_chance_agreement_is_zero():
    # 人工/系统边际相同但完全错配 → 等于随机 → 0。
    qwk = metrics.quadratic_weighted_kappa([0, 0, 2, 2], [0, 2, 0, 2], 0, 2)
    assert abs(qwk) < 1e-9


def test_qwk_partial_between_zero_and_one():
    qwk = metrics.quadratic_weighted_kappa([0, 1, 2, 3, 4], [0, 1, 2, 4, 3], 0, 4)
    assert 0.0 < qwk < 1.0


def test_mae_and_rmse():
    assert metrics.mae([10, 20], [12, 18]) == 2.0
    assert metrics.rmse([10, 20], [12, 18]) == 2.0


def test_grade_ordinal_boundaries():
    assert metrics.grade_ordinal(95) == 4
    assert metrics.grade_ordinal(90) == 4
    assert metrics.grade_ordinal(89.999) == 3
    assert metrics.grade_ordinal(60) == 1
    assert metrics.grade_ordinal(59.9) == 0


def test_evaluate_pairs_and_reports():
    predictions = [
        EvalPrediction("a", 88, {"C01": 13, "C02": 17}),
        EvalPrediction("b", 72, {"C01": 10, "C02": 12}),
        EvalPrediction("c", 50, {}),  # 无对应真值
    ]
    truths = [
        EvalSample("a", 86, {"C01": 14, "C02": 18}),
        EvalSample("b", 75, {"C01": 11, "C02": 13}),
    ]
    report = evaluate(predictions, truths)

    assert report["n"] == 2
    assert report["unmatched_predictions"] == ["c"]
    assert report["qwk"] == 1.0  # 等级 [3,2] 完全一致
    assert report["mae"] == 2.5
    assert report["exact_grade_agreement"] == 1.0
    # C01 系统比人工低 → bias 为负（偏严）。
    assert report["per_criterion"]["C01"]["bias"] == -1.0
    assert report["per_criterion"]["C01"]["mae"] == 1.0


def test_baseline_from_report_extracts_aggregate_only():
    report = {
        "qwk": 0.8, "mae": 2.0, "rmse": 3.0,
        "exact_grade_agreement": 0.7, "adjacent_grade_agreement": 0.9,
        "per_criterion": {"C01": {"bias": -1}}, "errors": [], "n": 5,
    }
    assert baseline_from_report(report) == {
        "qwk": 0.8, "mae": 2.0, "rmse": 3.0, "exact_grade_agreement": 0.7, "adjacent_grade_agreement": 0.9,
    }


def test_run_qwk_eval_script_imports():
    import backend.app.scripts.run_qwk_eval as script

    assert callable(script.main)


def test_regression_gate_passes_and_fails():
    baseline = {"qwk": 0.95, "mae": 2.0}
    assert assert_no_regression({"qwk": 0.94, "mae": 2.0}, baseline, qwk_drop_tol=0.02) == []
    issues = assert_no_regression({"qwk": 0.90, "mae": 2.0}, baseline, qwk_drop_tol=0.02)
    assert issues and "QWK 回退" in issues[0]
    mae_issues = assert_no_regression({"qwk": 0.96, "mae": 3.5}, baseline, mae_rise_tol=1.0)
    assert mae_issues and "MAE 上升" in mae_issues[0]


def test_blocked_final_total_is_excluded_instead_of_becoming_zero(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        '[{"key":"paper-1","human_total":88,"human_items":{}}]',
        encoding="utf-8",
    )

    class BlockedRun:
        final_total_score = None
        need_manual_review = True
        items = []

    monkeypatch.setattr(run_eval, "score_paper", lambda *_args, **_kwargs: BlockedRun())
    monkeypatch.setattr(run_eval, "_write_report", lambda _report: tmp_path / "report.json")

    report = run_eval.run_evaluation(object(), dataset)

    assert report["n"] == 0
    assert report["completed_runs"] == 1
    assert report["review_rate"] == 1.0
    assert report["blocked_rate"] == 1.0
    assert report["errors"] and "未进入指标" in report["errors"][0]["error"]
