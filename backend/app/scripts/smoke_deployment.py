"""Authenticated deployment smoke test for the running HTTP service.

The caller is responsible for applying migrations and seeding the disposable
smoke database.  This script uses synthetic content only and exercises the
same login, upload, score, report, and export routes used by the Web UI.

It also starts a background scoring job and waits for it to finish.  Without a
queue service the job only advances when a batch worker is running, so a stack
that forgot the worker fails here instead of leaving users at "queued".
"""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import time

from docx import Document
import httpx

from backend.app.core.config import settings


DEFAULT_RUBRIC_NAME = "本科毕业论文通用评分标准"
DEFAULT_BATCH_NAME = "2026 届论文评分开发批次"
DEFAULT_BACKGROUND_JOB_TIMEOUT_SECONDS = 180.0
TERMINAL_JOB_STATUSES = ("completed", "completed_with_errors", "canceled", "failed")
# 受保护部署不回落 Mock（D-028）。没有配置平台模型、也没有绑定连接的冒烟环境里，
# 评分必须明确拒绝，而不是悄悄出分；`fail-closed` 模式验证的正是这一点。
SCORING_MODES = ("succeed", "fail-closed")
PLATFORM_MODEL_MISSING = "PLATFORM_MODEL_MISSING"


def _parser():
    parser = argparse.ArgumentParser(prog="smoke_deployment")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rubric-name", default=DEFAULT_RUBRIC_NAME)
    parser.add_argument("--batch-name", default=DEFAULT_BATCH_NAME)
    parser.add_argument(
        "--scoring",
        choices=SCORING_MODES,
        default="succeed",
        help="succeed: a model is configured and papers must be scored; "
        "fail-closed: no model is configured and scoring must be refused explicitly",
    )
    parser.add_argument(
        "--background-job-timeout",
        type=float,
        default=DEFAULT_BACKGROUND_JOB_TIMEOUT_SECONDS,
        help="seconds to wait for the background scoring job to finish",
    )
    return parser


def _response(response, label):
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text[:500]
        raise RuntimeError(
            f"{label} failed with HTTP {response.status_code}: {body}"
        ) from exc
    return response


def _sample_docx():
    document = Document()
    document.add_paragraph("部署冒烟测试论文")
    document.add_paragraph("姓名：测试用户")
    document.add_paragraph("学号：SMOKE-0001")
    document.add_paragraph("中文摘要")
    document.add_paragraph("本文使用合成内容验证部署后的上传、解析和评分链路。")
    document.add_paragraph("关键词：部署；冒烟测试；评分")
    document.add_paragraph("目录")
    document.add_paragraph("第一章 绪论")
    document.add_paragraph("本章说明研究背景、研究意义和相关工作。")
    document.add_paragraph("第二章 文献综述")
    document.add_paragraph("本章综述与系统部署和质量验证有关的研究。")
    document.add_paragraph("第三章 研究方法")
    document.add_paragraph("本文采用可重复的接口测试方法验证服务行为。")
    document.add_paragraph("第四章 实验结果与分析")
    document.add_paragraph("测试覆盖登录、上传、解析、评分、报告和导出。")
    document.add_paragraph("第五章 创新点")
    document.add_paragraph("测试只使用合成文档，不包含真实学生信息。")
    document.add_paragraph("结论")
    document.add_paragraph("部署链路在预期接口上完成闭环。")
    document.add_paragraph("参考文献")
    document.add_paragraph("[1] Synthetic deployment smoke fixture, 2026.")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def await_background_job(
    client,
    headers,
    *,
    batch_id,
    paper_id,
    timeout_seconds,
    scoring="succeed",
    poll_seconds=2.0,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """Start a background scoring job for the batch and wait for its terminal state.

    Reaching a terminal state at all proves a worker picked the job up.  With
    ``scoring="succeed"`` the paper must also be scored; with ``"fail-closed"``
    the worker must have attempted it and recorded the missing-model refusal.
    """

    job = _response(
        client.post(
            f"/api/batches/{batch_id}/score-jobs",
            headers=headers,
            json={"rescore": False, "max_workers": 1},
        ),
        "background scoring job creation",
    ).json()
    deadline = clock() + timeout_seconds
    while job.get("status") not in TERMINAL_JOB_STATUSES:
        if clock() >= deadline:
            raise RuntimeError(
                f"background scoring job {job.get('id')} stayed {job.get('status')!r} "
                f"for {timeout_seconds:g}s; is the batch worker running?"
            )
        sleep(poll_seconds)
        job = _response(
            client.get(f"/api/batch-scoring-jobs/{job['id']}", headers=headers),
            "background scoring job status",
        ).json()
    item = next(
        (value for value in job.get("items") or [] if value.get("paper_id") == paper_id),
        None,
    )
    if item is None:
        raise RuntimeError("background scoring job did not include the uploaded paper")
    if scoring == "fail-closed":
        if (
            item.get("status") != "failed"
            or not item.get("attempt_count")
            or item.get("error_code") != PLATFORM_MODEL_MISSING
        ):
            raise RuntimeError(
                "background scoring without a configured model must fail closed with "
                f"{PLATFORM_MODEL_MISSING}; got job {job['status']!r}, item "
                f"{item.get('status')!r} {item.get('error_code') or ''}".rstrip()
            )
        return job, item
    if (
        job["status"] != "completed"
        or item.get("status") != "succeeded"
        or not item.get("scoring_run_id")
    ):
        raise RuntimeError(
            "background scoring job ended as "
            f"{job['status']!r} with item {item.get('status')!r}: "
            f"{item.get('error_code') or ''} {item.get('error_message') or ''}".rstrip()
        )
    return job, item


def run_smoke(
    *,
    base_url,
    rubric_name,
    batch_name,
    scoring="succeed",
    background_job_timeout=DEFAULT_BACKGROUND_JOB_TIMEOUT_SECONDS,
):
    if not settings.AUTH_ENABLED:
        raise RuntimeError("deployment smoke requires AUTH_ENABLED=true")
    if not settings.AUTH_PASSWORD:
        raise RuntimeError("deployment smoke requires AUTH_PASSWORD")

    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=120.0,
        trust_env=False,
    ) as client:
        integrations = _response(
            client.get("/api/system/integrations"),
            "integration status",
        ).json()
        if not integrations.get("frontend", {}).get("static_web_ready"):
            raise RuntimeError("static Web frontend is not ready")

        auth_status = _response(
            client.get("/api/auth/status"),
            "auth status",
        ).json()
        if auth_status.get("auth_required") is not True:
            raise RuntimeError("protected deployment did not require authentication")

        login = _response(
            client.post(
                "/api/auth/login",
                json={
                    "username": settings.AUTH_USERNAME,
                    "password": settings.AUTH_PASSWORD,
                },
            ),
            "login",
        )
        # 登录只下发 HttpOnly 会话 Cookie（204，无响应体）。它默认带 Secure，
        # 客户端不会经明文 http://127.0.0.1 自动回传，所以显式放进 Cookie 头。
        session = login.cookies.get(settings.AUTH_COOKIE_NAME)
        if not session:
            raise RuntimeError("login response did not set a session cookie")
        headers = {"Cookie": f"{settings.AUTH_COOKIE_NAME}={session}"}

        _response(
            client.get("/api/auth/me", headers=headers),
            "authenticated identity",
        )
        readiness = _response(
            client.get("/api/system/ops-readiness", headers=headers),
            "operations readiness",
        ).json()
        if readiness.get("signals", {}).get("security", {}).get("status") != "pass":
            raise RuntimeError("deployment security readiness did not pass")

        rubrics = _response(
            client.get("/api/rubrics", headers=headers),
            "rubric listing",
        ).json()
        rubric = next(
            (item for item in rubrics if item.get("name") == rubric_name),
            None,
        )
        if rubric is None:
            raise RuntimeError(f"smoke rubric not found: {rubric_name}")

        batches = _response(
            client.get("/api/batches", headers=headers),
            "batch listing",
        ).json()
        batch = next(
            (
                item
                for item in batches
                if item.get("name") == batch_name
                and item.get("rubric_id") == rubric.get("id")
            ),
            None,
        )
        if batch is None:
            raise RuntimeError(f"smoke batch not found: {batch_name}")

        uploaded = _response(
            client.post(
                "/api/papers/upload",
                headers=headers,
                data={"batch_id": batch["id"]},
                files={
                    "file": (
                        "deployment-smoke.docx",
                        _sample_docx(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            ),
            "synthetic document upload",
        ).json()
        if uploaded.get("status") != "parsed":
            raise RuntimeError("synthetic document was not parsed")

        job, job_item = await_background_job(
            client,
            headers,
            batch_id=batch["id"],
            paper_id=uploaded["id"],
            timeout_seconds=background_job_timeout,
            scoring=scoring,
        )
        background = {
            "id": job.get("id"),
            "status": job.get("status"),
            "item_status": job_item.get("status"),
            "item_error_code": job_item.get("error_code"),
            "scoring_run_id": job_item.get("scoring_run_id"),
        }

        if scoring == "fail-closed":
            refused = client.post(f"/api/papers/{uploaded['id']}/score", headers=headers)
            if refused.status_code != 503 or "平台模型" not in refused.text:
                raise RuntimeError(
                    "synchronous scoring without a configured model must be refused with "
                    f"HTTP 503; got HTTP {refused.status_code}: {refused.text[:300]}"
                )
            return {
                "schema_version": "deployment-smoke@2",
                "status": "passed",
                "scoring": "fail-closed",
                "frontend": "ready",
                "authentication": "passed",
                "database": readiness.get("signals", {}).get("database", {}).get("dialect"),
                "llm_provider": integrations.get("llm", {}).get("provider"),
                "paper_status": uploaded.get("status"),
                "background_job": background,
                "synchronous_scoring": "refused",
            }

        run = _response(
            client.post(
                f"/api/papers/{uploaded['id']}/score",
                headers=headers,
            ),
            "synchronous scoring",
        ).json()
        if run.get("final_total_score") is None:
            raise RuntimeError("scoring response did not contain a final score")

        report = _response(
            client.get(
                f"/api/scoring-runs/{run['id']}/report",
                headers=headers,
            ),
            "HTML report",
        )
        if "text/html" not in report.headers.get("content-type", ""):
            raise RuntimeError("report endpoint did not return HTML")

        export = _response(
            client.get(
                f"/api/batches/{batch['id']}/export.xlsx",
                headers=headers,
            ),
            "Excel export",
        )
        if "spreadsheetml" not in export.headers.get("content-type", ""):
            raise RuntimeError("batch export endpoint did not return XLSX")

    return {
        "schema_version": "deployment-smoke@2",
        "status": "passed",
        "scoring": "succeed",
        "frontend": "ready",
        "authentication": "passed",
        "database": readiness.get("signals", {}).get("database", {}).get("dialect"),
        "llm_provider": integrations.get("llm", {}).get("provider"),
        "paper_status": uploaded.get("status"),
        "scoring_run_id": run.get("id"),
        "background_job": background,
        "report": "passed",
        "excel_export": "passed",
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    result = run_smoke(
        base_url=args.base_url,
        rubric_name=args.rubric_name,
        batch_name=args.batch_name,
        scoring=args.scoring,
        background_job_timeout=args.background_job_timeout,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
