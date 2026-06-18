from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.config import settings
from backend.app.db.models import Base
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.services.storage.local import ensure_storage_dirs


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    original_storage_root = settings.STORAGE_ROOT
    original_runtime_settings = {
        "LLM_PROVIDER": settings.LLM_PROVIDER,
        "LLM_FALLBACK_TO_MOCK": settings.LLM_FALLBACK_TO_MOCK,
        "SHEET_WRITER_PROVIDER": settings.SHEET_WRITER_PROVIDER,
        "SHEET_FALLBACK_TO_MOCK": settings.SHEET_FALLBACK_TO_MOCK,
    }
    settings.STORAGE_ROOT = tmp_path / "storage"
    settings.LLM_PROVIDER = "mock"
    settings.LLM_FALLBACK_TO_MOCK = True
    settings.SHEET_WRITER_PROVIDER = "mock"
    settings.SHEET_FALLBACK_TO_MOCK = True
    ensure_storage_dirs()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()  # 释放该用例的引擎连接，避免跨用例连接堆积
    settings.STORAGE_ROOT = original_storage_root
    for key, value in original_runtime_settings.items():
        setattr(settings, key, value)


def make_sample_docx():
    document = Document()
    document.add_paragraph("基于机器学习的教学质量评价研究")
    document.add_paragraph("姓名：张三")
    document.add_paragraph("学号：20260001")
    document.add_paragraph("中文摘要")
    document.add_paragraph("本文围绕教学质量评价问题展开研究，说明研究背景、研究意义和应用价值。")
    document.add_paragraph("关键词：教学质量；机器学习；评价模型")
    document.add_paragraph("目录")
    document.add_paragraph("第一章 绪论")
    document.add_paragraph("本章介绍研究背景、研究意义、国内外相关工作和论文结构。")
    document.add_paragraph("第二章 文献综述")
    document.add_paragraph("国内外研究现状表明，教学质量评价需要结合多源数据和可解释分析。")
    document.add_paragraph("第三章 研究方法")
    document.add_paragraph("本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模。")
    document.add_paragraph("第四章 实验结果与分析")
    document.add_paragraph("实验结果显示，模型能够识别关键影响因素，并通过对比实验支撑结论。")
    document.add_paragraph("第五章 创新点")
    document.add_paragraph("本文提出改进的指标加权方法，并增强评价结果解释能力。")
    document.add_paragraph("结论")
    document.add_paragraph("研究结论表明，该方法具有一定应用价值，后续可扩展到更多专业。")
    document.add_paragraph("参考文献")
    document.add_paragraph("[1] 张某某. 教学评价研究综述[J]. 教育研究, 2024.")
    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


def make_dalian_neusoft_cover_docx():
    document = Document()
    document.add_paragraph("大连东软信息学院")
    document.add_paragraph("毕业设计（论文）")
    document.add_paragraph("论文题目： 基于 Spring Boot 的网上图书商城管理系统的")
    document.add_paragraph("设计与实现")
    table = document.add_table(rows=7, cols=2)
    rows = [
        ("学    院：", "软件学院"),
        ("专    业：", "软件工程（专升本）"),
        ("学生姓名：", "王子铭"),
        ("学生学号：", "24201023601"),
        ("指导教师：", "刘真    周绍斌"),
        ("导师职称：", "助教    副教授"),
        ("完成日期：", "2026 年 4 月 13 日"),
    ]
    for row, (label, value) in zip(table.rows, rows):
        row.cells[0].text = label
        row.cells[1].text = value
    document.add_paragraph("中文摘要")
    document.add_paragraph("本文围绕网上图书商城管理系统展开研究，说明研究背景、研究意义和应用价值。")
    document.add_paragraph("关键词：Spring Boot；图书商城；管理系统")
    document.add_paragraph("目录")
    document.add_paragraph("第一章 绪论")
    document.add_paragraph("本章介绍研究背景、研究意义、国内外相关工作和论文结构。")
    document.add_paragraph("第二章 需求分析")
    document.add_paragraph("本章说明用户管理、图书管理、订单管理和后台管理等功能需求。")
    document.add_paragraph("第三章 系统设计")
    document.add_paragraph("本章说明系统架构、数据库设计、接口设计和核心模块划分。")
    document.add_paragraph("第四章 系统实现")
    document.add_paragraph("本章说明基于 Spring Boot 的业务模块实现、页面交互和测试结果。")
    document.add_paragraph("结论")
    document.add_paragraph("研究结论表明，该系统能够支撑网上图书商城管理的基本业务流程。")
    document.add_paragraph("参考文献")
    document.add_paragraph("[1] 王某某. Java Web 应用开发研究[J]. 软件工程, 2024.")
    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


def make_template_docx():
    document = Document()
    document.add_heading("本科毕业论文模板", level=1)
    document.add_heading("中文摘要", level=2)
    document.add_paragraph("摘要应说明研究背景、研究意义、研究方法和主要结论。")
    document.add_heading("第一章 绪论", level=2)
    document.add_paragraph("说明研究背景、研究意义和论文结构。")
    document.add_heading("第二章 文献综述", level=2)
    document.add_paragraph("梳理国内外研究现状和相关工作。")
    document.add_heading("第三章 研究方法", level=2)
    document.add_paragraph("说明数据来源、实验设计和模型方法。")
    document.add_heading("第四章 结果分析", level=2)
    document.add_heading("第五章 创新点", level=2)
    document.add_heading("结论", level=2)
    document.add_heading("参考文献", level=2)
    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


def make_rules_xlsx():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "评分规则"
    sheet.append(["编号", "评分项", "分值", "评分说明", "证据提示", "扣分规则"])
    sheet.append(["C01", "研究方法", 20, "方法合理，数据来源清楚。", "研究方法；实验设计", "方法说明不足扣分"])
    sheet.append(["C02", "文献综述", 15, "综述覆盖充分，能归纳研究现状。", "文献综述", "文献覆盖不足扣分"])
    sheet.append(["C03", "参考文献", 10, "引用规范，格式完整。", "参考文献", "参考文献不足扣分"])
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
