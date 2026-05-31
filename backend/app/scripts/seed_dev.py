from sqlalchemy import select

from backend.app.db.models import GradingBatch
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCriterion
from backend.app.db.session import SessionLocal
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.storage.local import ensure_storage_dirs


DEFAULT_CRITERIA = [
    ("C01", "选题意义", 10, ["绪论", "研究背景", "研究意义"], ["研究意义表述笼统，扣 1 到 3 分"]),
    ("C02", "文献综述", 15, ["文献综述", "国内外研究现状", "相关工作"], ["文献覆盖不足，扣 2 到 5 分"]),
    ("C03", "研究方法", 20, ["研究方法", "实验设计", "数据来源"], ["方法说明不清晰，扣 2 到 6 分"]),
    ("C04", "论文创新性", 15, ["创新点", "贡献", "改进"], ["创新性不足，扣 2 到 5 分"]),
    ("C05", "论证与分析", 20, ["实验结果", "结果分析", "讨论"], ["论证链条不完整，扣 2 到 6 分"]),
    ("C06", "写作规范", 10, ["摘要", "关键词", "目录", "结论"], ["结构或格式缺项，扣 1 到 4 分"]),
    ("C07", "参考文献", 10, ["参考文献", "引用"], ["参考文献数量或格式不足，扣 1 到 4 分"]),
]


def seed(db=None):
    """造默认 dev user + rubric + 批次。`db=None` 走 SessionLocal（脚本用）；传入则用该会话（CLI 用本地 sqlite）。"""
    ensure_storage_dirs()
    if db is None:
        with SessionLocal() as owned:
            _seed_into(owned)
        return
    _seed_into(db)


def _seed_into(db):
    user = ensure_dev_user(db)
    rubric = db.scalar(select(Rubric).where(Rubric.name == "本科毕业论文通用评分标准", Rubric.version == "v1.0"))
    if rubric is None:
        rubric = Rubric(
            name="本科毕业论文通用评分标准",
            version="v1.0",
            total_score=100,
            status="published",
            description="MVP 默认评分标准，用于本地开发和演示。",
            created_by=user.id,
        )
        for order, (code, name, max_score, hints, rules) in enumerate(DEFAULT_CRITERIA, start=1):
            rubric.criteria.append(
                RubricCriterion(
                    code=code,
                    name=name,
                    max_score=max_score,
                    display_order=order,
                    description="考察%s相关质量。" % name,
                    evidence_hints=hints,
                    deduction_rules=rules,
                )
            )
        db.add(rubric)
        db.flush()

    batch = db.scalar(select(GradingBatch).where(GradingBatch.name == "2026 届论文评分开发批次"))
    if batch is None:
        db.add(
            GradingBatch(
                name="2026 届论文评分开发批次",
                department="计算机学院",
                major="软件工程",
                academic_year="2026",
                paper_type="本科毕业论文",
                rubric_id=rubric.id,
                status="draft",
                created_by=user.id,
            )
        )
    db.commit()


if __name__ == "__main__":
    seed()
    print("Seeded dev user, default rubric, and demo batch.")
