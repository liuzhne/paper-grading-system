import json
import os

import requests
import streamlit as st
import streamlit.components.v1 as components

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000/api")


DEFAULT_CRITERIA = [
    {
        "code": "C01",
        "name": "选题意义",
        "max_score": 10,
        "description": "考察选题的理论意义、现实意义和问题价值。",
        "evidence_hints": ["绪论", "研究背景", "研究意义"],
        "deduction_rules": ["研究意义表述笼统，扣 1 到 3 分"],
        "display_order": 1,
    },
    {
        "code": "C02",
        "name": "文献综述",
        "max_score": 15,
        "description": "考察文献覆盖、归纳能力和研究空白识别。",
        "evidence_hints": ["文献综述", "国内外研究现状", "相关工作"],
        "deduction_rules": ["文献覆盖不足，扣 2 到 5 分"],
        "display_order": 2,
    },
    {
        "code": "C03",
        "name": "研究方法",
        "max_score": 20,
        "description": "考察方法合理性、实验设计和数据来源。",
        "evidence_hints": ["研究方法", "实验设计", "数据来源"],
        "deduction_rules": ["方法说明不清晰，扣 2 到 6 分"],
        "display_order": 3,
    },
    {
        "code": "C04",
        "name": "论文创新性",
        "max_score": 15,
        "description": "考察创新点、对比现有研究和贡献。",
        "evidence_hints": ["创新点", "贡献", "改进"],
        "deduction_rules": ["创新性不足，扣 2 到 5 分"],
        "display_order": 4,
    },
    {
        "code": "C05",
        "name": "论证与分析",
        "max_score": 20,
        "description": "考察逻辑严密性、数据分析和结论支撑。",
        "evidence_hints": ["实验结果", "结果分析", "讨论"],
        "deduction_rules": ["论证链条不完整，扣 2 到 6 分"],
        "display_order": 5,
    },
    {
        "code": "C06",
        "name": "写作规范",
        "max_score": 10,
        "description": "考察结构完整、格式、图表和语言规范。",
        "evidence_hints": ["摘要", "关键词", "目录", "结论"],
        "deduction_rules": ["结构或格式缺项，扣 1 到 4 分"],
        "display_order": 6,
    },
    {
        "code": "C07",
        "name": "参考文献",
        "max_score": 10,
        "description": "考察参考文献数量、格式、引用规范和时效性。",
        "evidence_hints": ["参考文献", "引用"],
        "deduction_rules": ["参考文献数量或格式不足，扣 1 到 4 分"],
        "display_order": 7,
    },
]

BATCH_STATUSES = ["draft", "parsing", "scoring", "scored", "scored_with_errors", "reviewed", "archived"]


def main():
    st.set_page_config(page_title="论文智能评分 MVP", layout="wide")
    st.title("毕业论文智能评分系统 MVP")
    st.caption("当前版本为本地单用户核心流程，无登录鉴权。")

    page = st.sidebar.radio(
        "功能",
        ["评分标准", "批次与上传", "评分与复核", "导出与报告"],
    )
    st.sidebar.text_input("API 地址", API_BASE_URL, key="api_base_url")

    if page == "评分标准":
        rubric_page()
    elif page == "批次与上传":
        batch_upload_page()
    elif page == "评分与复核":
        scoring_page()
    else:
        export_report_page()


def api_url(path):
    return st.session_state.get("api_base_url", API_BASE_URL).rstrip("/") + path


def request(method, path, **kwargs):
    try:
        response = requests.request(method, api_url(path), timeout=60, **kwargs)
    except requests.RequestException as exc:
        st.error("请求失败：%s" % exc)
        return None
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text
        st.error("接口错误：%s" % detail)
        return None
    return response


def rubric_page():
    st.subheader("评分标准")
    with st.expander("新建评分标准", expanded=True):
        name = st.text_input("名称", "本科毕业论文通用评分标准")
        version = st.text_input("版本", "v1.0")
        description = st.text_area("说明", "MVP 默认评分标准。")
        criteria_json = st.text_area("评分项 JSON", json.dumps(DEFAULT_CRITERIA, ensure_ascii=False, indent=2), height=360)
        if st.button("创建评分标准"):
            try:
                criteria = json.loads(criteria_json)
            except json.JSONDecodeError as exc:
                st.error("评分项 JSON 格式错误：%s" % exc)
                return
            if not isinstance(criteria, list) or not criteria:
                st.error("评分项 JSON 必须是非空数组")
                return
            try:
                total_score = sum(float(item["max_score"]) for item in criteria)
            except (KeyError, TypeError, ValueError):
                st.error("每个评分项都需含有效的 max_score")
                return
            payload = {
                "name": name,
                "version": version,
                "total_score": total_score,
                "description": description,
                "criteria": criteria,
            }
            response = request("POST", "/rubrics", json=payload)
            if response:
                st.success("评分标准已创建")
                st.json(response.json())

    with st.expander("从 Word 模板和 Excel 评分规则导入"):
        import_name = st.text_input("导入标准名称", "导入的论文评分标准")
        import_version = st.text_input("导入版本", "v1.0-import")
        import_description = st.text_area("导入说明", "由 Word 论文模板和 Excel 评分规则解析生成。")
        template_file = st.file_uploader("Word 论文模板（.docx）", type=["docx"], key="rubric_template_file")
        rules_file = st.file_uploader("Excel 评分规则（.xlsx/.xlsm）", type=["xlsx", "xlsm"], key="rubric_rules_file")
        if st.button("解析文件并创建草稿标准"):
            if not rules_file:
                st.error("请上传 Excel 评分规则。")
                return
            files = {
                "rules_file": (
                    rules_file.name,
                    rules_file.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            }
            if template_file:
                files["template_file"] = (
                    template_file.name,
                    template_file.getvalue(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            response = request(
                "POST",
                "/rubrics/import-files",
                data={"name": import_name, "version": import_version, "description": import_description},
                files=files,
            )
            if response:
                result = response.json()
                st.success("已从文件生成草稿评分标准")
                if result.get("warnings"):
                    st.warning("；".join(result["warnings"]))
                st.json(result)

    rubrics = _get_json("/rubrics", [])
    st.write("已有评分标准")
    st.dataframe(_rubric_rows(rubrics), use_container_width=True)

    with st.expander("编辑草稿评分标准"):
        draft_rubrics = [item for item in rubrics if item.get("status") == "draft"]
        draft = _select_by_name(
            "选择草稿标准",
            draft_rubrics,
            lambda item: "%s / %s" % (item["name"], item["version"]),
            key="edit_rubric_select",
        )
        if draft:
            rubric_id = draft["id"]
            edit_name = st.text_input("草稿名称", draft.get("name") or "", key="edit_rubric_name_%s" % rubric_id)
            edit_version = st.text_input("草稿版本", draft.get("version") or "", key="edit_rubric_version_%s" % rubric_id)
            edit_description = st.text_area(
                "草稿说明",
                draft.get("description") or "",
                key="edit_rubric_description_%s" % rubric_id,
            )
            edit_criteria_json = st.text_area(
                "草稿评分项 JSON",
                json.dumps(_criteria_payload_rows(draft.get("criteria") or []), ensure_ascii=False, indent=2),
                height=320,
                key="edit_rubric_criteria_%s" % rubric_id,
            )
            if st.button("保存草稿评分标准", key="save_rubric_%s" % rubric_id):
                try:
                    criteria = json.loads(edit_criteria_json)
                except json.JSONDecodeError as exc:
                    st.error("评分项 JSON 格式错误：%s" % exc)
                    return
                payload = {
                    "name": edit_name.strip(),
                    "version": edit_version.strip(),
                    "description": edit_description,
                    "total_score": sum(float(item["max_score"]) for item in criteria),
                    "criteria": criteria,
                }
                response = request("PATCH", "/rubrics/%s" % rubric_id, json=payload)
                if response:
                    st.success("草稿评分标准已保存")
                    st.json(response.json())
        else:
            st.caption("暂无可编辑草稿；可先复制已发布标准为新版本。")

    selected = _select_by_name("发布评分标准", rubrics, lambda item: "%s / %s / %s" % (item["name"], item["version"], item["status"]))
    if selected and st.button("发布选中标准"):
        response = request("POST", "/rubrics/%s/publish" % selected["id"])
        if response:
            st.success("已发布")
            st.json(response.json())

    with st.expander("复制为新版本"):
        source = _select_by_name("源评分标准", rubrics, lambda item: "%s / %s / %s" % (item["name"], item["version"], item["status"]))
        new_version = st.text_input("新版本号", "v1.1")
        new_name = st.text_input("新标准名称（留空沿用原名称）", "")
        clone_description = st.text_area("新版本说明（留空沿用原说明）", "")
        if source and st.button("复制评分标准"):
            payload = {"new_version": new_version}
            if new_name.strip():
                payload["name"] = new_name.strip()
            if clone_description.strip():
                payload["description"] = clone_description.strip()
            response = request("POST", "/rubrics/%s/clone" % source["id"], json=payload)
            if response:
                st.success("已复制为新版本")
                st.json(response.json())


def batch_upload_page():
    st.subheader("批次与论文上传")
    rubrics = _get_json("/rubrics", [])
    if not rubrics:
        st.info("请先创建评分标准。")
        return

    with st.expander("新建评分批次", expanded=True):
        rubric = _select_by_name("绑定评分标准", rubrics, lambda item: "%s / %s" % (item["name"], item["version"]))
        name = st.text_input("批次名称", "2026 届论文评分开发批次")
        department = st.text_input("学院", "计算机学院")
        major = st.text_input("专业", "软件工程")
        academic_year = st.text_input("年级", "2026")
        paper_type = st.text_input("论文类型", "本科毕业论文")
        if st.button("创建批次") and rubric:
            payload = {
                "name": name,
                "rubric_id": rubric["id"],
                "department": department,
                "major": major,
                "academic_year": academic_year,
                "paper_type": paper_type,
            }
            response = request("POST", "/batches", json=payload)
            if response:
                st.success("批次已创建")
                st.json(response.json())

    batches = _get_json("/batches", [])
    st.write("已有批次")
    st.dataframe(batches, use_container_width=True)

    batch = _select_by_name("上传到批次", batches, lambda item: item["name"])
    if batch:
        _render_batch_editor(batch)

    uploaded_files = st.file_uploader("上传 .docx 或文本型 .pdf", type=["docx", "pdf"], accept_multiple_files=True)
    if st.button("上传并解析论文") and batch and uploaded_files:
        files = [
            ("files", (uploaded_file.name, uploaded_file.getvalue(), uploaded_file.type))
            for uploaded_file in uploaded_files
        ]
        response = request("POST", "/papers/bulk-upload", data={"batch_id": batch["id"]}, files=files)
        if response:
            papers = response.json()
            failed = [paper for paper in papers if paper["status"] == "failed"]
            if failed:
                st.warning("上传完成，其中 %s 篇解析失败。" % len(failed))
            else:
                st.success("上传并解析完成")
            st.dataframe(_paper_rows(papers), use_container_width=True)

    if batch:
        st.write("批次概览")
        summary = _get_json("/batches/%s/summary" % batch["id"], None)
        if summary:
            _render_batch_summary(summary)

        st.write("批次论文")
        papers = _get_json("/papers?batch_id=%s" % batch["id"], [])
        st.dataframe(_paper_rows(papers), use_container_width=True)
        rescore = st.checkbox("重新评分已有评分任务", value=False)
        if st.button("一键评分当前批次"):
            response = request("POST", "/batches/%s/score?rescore=%s" % (batch["id"], str(rescore).lower()))
            if response:
                summary = response.json()
                st.success(
                    "批次评分完成：新增 %s，跳过 %s，失败 %s"
                    % (summary["scored_count"], summary["skipped_count"], summary["failed_count"])
                )
                st.json(summary)
        selected_paper = _select_by_name("选择论文发起评分", papers, _paper_label)
        if selected_paper:
            _render_paper_editor(selected_paper)
            c1, c2 = st.columns([1, 1])
            with c1:
                if st.button("重新解析选中论文"):
                    response = request("POST", "/papers/%s/parse" % selected_paper["id"])
                    if response:
                        st.success("重新解析完成")
                        st.json(response.json())
            with c2:
                if st.button("对选中论文评分"):
                    response = request("POST", "/papers/%s/score" % selected_paper["id"])
                    if response:
                        st.success("评分完成")
                        st.json(response.json())


def scoring_page():
    st.subheader("评分与人工复核")
    batches = _get_json("/batches", [])
    if not batches:
        st.info("请先创建批次并上传论文。")
        return

    batch = _select_by_name("选择批次", batches, lambda item: item["name"])
    papers = _get_json("/papers?batch_id=%s" % batch["id"], []) if batch else []
    selected_paper = _select_by_name("选择论文", papers, _paper_label)
    if selected_paper:
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("查看论文解析信息"):
                response = request("GET", "/papers/%s/parsed" % selected_paper["id"])
                if response:
                    parsed = response.json()["parsed"]
                    st.json(
                        {
                            "title": parsed.get("title"),
                            "student_id": parsed.get("student_id"),
                            "student_name": parsed.get("student_name"),
                            "parse_quality": parsed.get("parse_quality"),
                            "structure_checks": parsed.get("structure_checks"),
                        }
                    )
        with c2:
            if st.button("发起同步评分"):
                response = request("POST", "/papers/%s/score" % selected_paper["id"])
                if response:
                    st.success("评分完成")
                    st.json(response.json())

    runs = []
    if selected_paper:
        runs = _get_json("/scoring-runs?paper_id=%s" % selected_paper["id"], [])
    elif batch:
        runs = _get_json("/scoring-runs?batch_id=%s" % batch["id"], [])

    selected_run = _select_by_name("选择评分任务", runs, _run_label)
    manual_run_id = st.text_input("或手动输入评分任务 ID")
    run_id = selected_run["id"] if selected_run else manual_run_id
    if run_id:
        render_scoring_run(run_id)
        if st.button("重试当前评分任务"):
            response = request("POST", "/scoring-runs/%s/retry" % run_id)
            if response:
                st.success("已生成新的评分任务")
                st.json(response.json())

    with st.expander("修改单项分数"):
        current_items = st.session_state.get("current_score_items", [])
        selected_item = _select_by_name("选择评分项", current_items, _score_item_label)
        manual_item_id = st.text_input("或手动输入评分项 ID")
        item_id = selected_item["id"] if selected_item else manual_item_id
        default_score = float((selected_item or {}).get("final_score") or 0)
        final_score = st.number_input("最终得分", min_value=0.0, value=default_score, step=0.5)
        reason = st.text_area("修改原因")
        if st.button("保存单项修改") and item_id and reason:
            response = request("PATCH", "/score-items/%s" % item_id, json={"final_score": final_score, "reason": reason})
            if response:
                st.success("已保存修改")
                st.json(response.json())

    with st.expander("提交整体验收复核"):
        review_reason = st.text_area("复核意见")
        if st.button("提交复核") and run_id and review_reason:
            response = request("POST", "/scoring-runs/%s/review" % run_id, json={"reason": review_reason})
            if response:
                st.success("复核已提交")
                st.json(response.json())


def render_scoring_run(run_id):
    run_response = request("GET", "/scoring-runs/%s" % run_id)
    items_response = request("GET", "/scoring-runs/%s/items" % run_id)
    logs_response = request("GET", "/scoring-runs/%s/review-logs" % run_id)
    if not run_response or not items_response:
        return
    run = run_response.json()
    items = items_response.json()
    logs = logs_response.json() if logs_response else []
    chunks = _get_json("/papers/%s/chunks" % run["paper_id"], []) if run.get("paper_id") else []
    chunks_by_id = {chunk["id"]: chunk for chunk in chunks}
    st.session_state["current_score_items"] = items
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("AI 总分", run.get("ai_total_score"))
    c2.metric("最终总分", run.get("final_total_score"))
    c3.metric("等级", run.get("grade"))
    c4.metric("需复核", "是" if run.get("need_manual_review") else "否")
    for item in items:
        name = item.get("criterion_name") or item["criterion_id"]
        with st.expander("%s / %s 分" % (name, item["final_score"])):
            st.write(item["reason"])
            st.write("扣分：", "；".join(item.get("deductions") or []))
            st.write("建议：", item.get("suggestion"))
            st.write("证据：")
            for evidence in item.get("evidence") or []:
                st.info("%s\n\n%s" % (evidence.get("location"), evidence.get("quote")))
                chunk = chunks_by_id.get(evidence.get("chunk_id"))
                if chunk:
                    st.caption("原文上下文")
                    st.code(_format_chunk_context(chunk), language=None)
                else:
                    st.caption("未找到对应原文分块。")
            st.caption("评分项 ID: %s" % item["id"])

    if logs:
        st.write("复核日志")
        st.dataframe(_review_log_rows(logs), use_container_width=True)


def export_report_page():
    st.subheader("导出与报告")
    batches = _get_json("/batches", [])
    batch = _select_by_name("导出批次", batches, lambda item: item["name"])
    if st.button("导出 Excel") and batch:
        response = request("GET", "/batches/%s/export.xlsx" % batch["id"])
        if response:
            st.download_button(
                "下载 Excel",
                response.content,
                file_name="batch_%s_scores.xlsx" % batch["id"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    runs = _get_json("/scoring-runs?batch_id=%s" % batch["id"], []) if batch else []
    selected_run = _select_by_name("选择报告评分任务", runs, _run_label)
    manual_run_id = st.text_input("或手动输入评分任务 ID")
    run_id = selected_run["id"] if selected_run else manual_run_id
    write_target_id = st.text_input("写表目标标识", "local-preview")
    if st.button("写入工作表预览") and run_id:
        response = request("POST", "/scoring-runs/%s/write-sheet" % run_id, json={"target_id": write_target_id})
        if response:
            st.success("写表预览已记录")
            st.json(response.json())

    if st.button("生成并查看报告") and run_id:
        response = request("GET", "/scoring-runs/%s/report" % run_id)
        if response:
            components.html(response.text, height=700, scrolling=True)
            st.download_button("下载 HTML 报告", response.text, file_name="scoring_report_%s.html" % run_id)

    if batch:
        st.write("导出日志")
        logs = _get_json("/export-logs?batch_id=%s" % batch["id"], [])
        if logs:
            st.dataframe(_export_log_rows(logs), use_container_width=True)
        else:
            st.caption("当前批次暂无导出日志。")


def _get_json(path, default):
    response = request("GET", path)
    if not response:
        return default
    return response.json()


def _select_by_name(label, items, formatter, key=None):
    if not items:
        return None
    options = {formatter(item): item for item in items}
    selected_label = st.selectbox(label, list(options.keys()), key=key)
    return options[selected_label]


def _rubric_rows(rubrics):
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "version": item["version"],
            "total_score": item["total_score"],
            "status": item["status"],
            "criteria_count": len(item.get("criteria") or []),
        }
        for item in rubrics
    ]


def _criteria_payload_rows(criteria):
    return [
        {
            "code": item.get("code"),
            "name": item.get("name"),
            "max_score": item.get("max_score"),
            "weight": item.get("weight"),
            "description": item.get("description"),
            "evidence_hints": item.get("evidence_hints") or [],
            "deduction_rules": item.get("deduction_rules") or [],
            "display_order": item.get("display_order") or index + 1,
        }
        for index, item in enumerate(criteria)
    ]


def _paper_rows(papers):
    return [
        {
            "id": item["id"],
            "title": item.get("title"),
            "student_id": item.get("student_id"),
            "student_name": item.get("student_name"),
            "status": item.get("status"),
            "parse_quality": item.get("parse_quality"),
            "file_name": item.get("file_name"),
        }
        for item in papers
    ]


def _render_paper_metrics(papers):
    total = len(papers)
    parsed = sum(1 for item in papers if item.get("status") == "parsed")
    scored = sum(1 for item in papers if item.get("status") in ["scored", "reviewed", "pending_review"])
    failed = sum(1 for item in papers if item.get("status") == "failed")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("论文数", total)
    c2.metric("已解析", parsed)
    c3.metric("已评分/待复核", scored)
    c4.metric("失败", failed)


def _render_batch_summary(summary):
    paper_stats = summary.get("paper_stats", {})
    run_stats = summary.get("run_stats", {})
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("论文数", len(summary.get("papers") or []))
    c2.metric("已解析", paper_stats.get("parsed", 0))
    c3.metric("已评分", run_stats.get("scored", 0) + run_stats.get("reviewing", 0) + run_stats.get("reviewed", 0))
    c4.metric("待复核", run_stats.get("need_manual_review", 0))
    c5.metric("失败", paper_stats.get("failed", 0))
    st.caption(
        "评分标准：%s / %s；批次状态：%s"
        % (summary.get("rubric_name"), summary.get("rubric_version"), summary.get("batch", {}).get("status"))
    )
    if summary.get("papers"):
        st.dataframe(_batch_summary_rows(summary["papers"]), use_container_width=True)


def _render_batch_editor(batch):
    batch_id = batch["id"]
    with st.expander("编辑当前批次信息"):
        name = st.text_input("批次名称", batch.get("name") or "", key="edit_batch_name_%s" % batch_id)
        department = st.text_input("学院", batch.get("department") or "", key="edit_batch_department_%s" % batch_id)
        major = st.text_input("专业", batch.get("major") or "", key="edit_batch_major_%s" % batch_id)
        academic_year = st.text_input("年级", batch.get("academic_year") or "", key="edit_batch_year_%s" % batch_id)
        paper_type = st.text_input("论文类型", batch.get("paper_type") or "", key="edit_batch_type_%s" % batch_id)
        status = batch.get("status") or "draft"
        status_options = BATCH_STATUSES if status in BATCH_STATUSES else [status] + BATCH_STATUSES
        status_value = st.selectbox(
            "批次状态",
            status_options,
            index=status_options.index(status),
            key="edit_batch_status_%s" % batch_id,
        )
        if st.button("保存批次信息", key="save_batch_%s" % batch_id):
            payload = {
                "name": name.strip(),
                "department": department.strip() or None,
                "major": major.strip() or None,
                "academic_year": academic_year.strip() or None,
                "paper_type": paper_type.strip() or None,
                "status": status_value,
            }
            response = request("PATCH", "/batches/%s" % batch_id, json=payload)
            if response:
                st.success("批次信息已更新")
                st.json(response.json())


def _render_paper_editor(paper):
    paper_id = paper["id"]
    with st.expander("校正选中论文信息"):
        title = st.text_input("论文题目", paper.get("title") or "", key="edit_paper_title_%s" % paper_id)
        c1, c2 = st.columns([1, 1])
        with c1:
            student_id = st.text_input("学号", paper.get("student_id") or "", key="edit_paper_student_id_%s" % paper_id)
            department = st.text_input("学院", paper.get("department") or "", key="edit_paper_department_%s" % paper_id)
            advisor = st.text_input("指导教师", paper.get("advisor") or "", key="edit_paper_advisor_%s" % paper_id)
        with c2:
            student_name = st.text_input("姓名", paper.get("student_name") or "", key="edit_paper_student_name_%s" % paper_id)
            major = st.text_input("专业", paper.get("major") or "", key="edit_paper_major_%s" % paper_id)
        if st.button("保存论文信息", key="save_paper_%s" % paper_id):
            payload = {
                "title": title.strip() or None,
                "student_id": student_id.strip() or None,
                "student_name": student_name.strip() or None,
                "department": department.strip() or None,
                "major": major.strip() or None,
                "advisor": advisor.strip() or None,
            }
            response = request("PATCH", "/papers/%s" % paper_id, json=payload)
            if response:
                st.success("论文信息已更新")
                st.json(response.json())


def _batch_summary_rows(papers):
    return [
        {
            "论文": item.get("title") or item.get("file_name"),
            "学生": item.get("student_name") or item.get("student_id"),
            "论文状态": item.get("paper_status"),
            "解析质量": item.get("parse_quality"),
            "最新评分状态": item.get("latest_run_status"),
            "最终分": item.get("latest_final_score"),
            "等级": item.get("latest_grade"),
            "需复核": item.get("latest_need_manual_review"),
            "评分任务 ID": item.get("latest_run_id"),
        }
        for item in papers
    ]


def _review_log_rows(logs):
    return [
        {
            "时间": item.get("created_at"),
            "评分项 ID": item.get("score_item_id"),
            "修改前": item.get("before_score"),
            "修改后": item.get("after_score"),
            "原因": item.get("reason"),
            "复核人": item.get("reviewer_id"),
        }
        for item in logs
    ]


def _export_log_rows(logs):
    return [
        {
            "时间": item.get("created_at"),
            "评分任务 ID": item.get("scoring_run_id"),
            "目标类型": item.get("target_type"),
            "状态": item.get("status"),
            "文件/目标": item.get("target_id"),
            "错误": item.get("error_message"),
        }
        for item in logs
    ]


def _paper_label(item):
    title = item.get("title") or item.get("file_name") or item["id"]
    student = item.get("student_name") or item.get("student_id") or "未知学生"
    return "%s / %s / %s" % (student, title, item.get("status"))


def _run_label(item):
    return "%s / %s / %s / %s" % (
        item.get("created_at", "")[:19],
        item.get("status"),
        item.get("final_total_score"),
        item.get("grade"),
    )


def _score_item_label(item):
    return "%s / AI %s / 最终 %s" % (
        item.get("criterion_name") or item.get("criterion_id"),
        item.get("ai_score"),
        item.get("final_score"),
    )


def _format_chunk_context(chunk):
    title = chunk.get("section_title") or "未知章节"
    page_start = chunk.get("page_start")
    page_end = chunk.get("page_end")
    if page_start and page_end and page_start != page_end:
        location = "%s，第%s-%s页" % (title, page_start, page_end)
    elif page_start:
        location = "%s，第%s页" % (title, page_start)
    else:
        location = title
    return "%s\n\n%s" % (location, chunk.get("text") or "")


if __name__ == "__main__":
    main()
