import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(process.cwd(), "../..");
const workbook = execFileSync(process.env.PGS_PYTHON || resolve(root, ".venv/bin/python"), ["-c", `
from io import BytesIO
import sys
from openpyxl import load_workbook
from backend.app.tests.test_m8_web_rubric_lifecycle import _published_rule_workbook_bytes
book = load_workbook(BytesIO(_published_rule_workbook_bytes()))
sheet = book.active
row = [cell.value for cell in sheet[2]]
row[0] = 'thesis.method_reproducible.v1'
row[2] = '研究方法质量'
row[6] = '实验步骤应可复现。'
sheet.append(row)
row = list(row)
row[0] = 'thesis.result_quality.v1'
row[1] = 'RESULT'
row[2] = '结果分析'
row[6] = '分析结果应提供依据。'
sheet.append(row)
out = BytesIO()
book.save(out)
sys.stdout.buffer.write(out.getvalue())
`], { cwd: root });

async function importTemplate(page) {
  const name = `确认回归合成模板-${Date.now()}`;
  await page.goto("/workbench/rubrics");
  await page.getByRole("button", { name: "导入评分模板", exact: true }).click();
  const panel = page.locator(".import-panel");
  await panel.getByLabel("标准名称", { exact: true }).fill(name);
  await panel.getByLabel(/规则 Excel/).setInputFiles({ name: "review.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: workbook });
  const imported = page.waitForResponse((r) => r.url().endsWith("/rubrics/import-files") && r.request().method() === "POST");
  await panel.getByRole("button", { name: "导入", exact: true }).click();
  const response = await imported;
  expect(response.ok(), await response.text()).toBeTruthy();
  const id = (await response.json()).rubric.id;
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  await expect(page.locator("[data-test=rule-review-panel] tbody tr")).toHaveCount(2);
  return { id, name };
}

async function reopen(page, name) {
  await page.reload();
  await page.getByRole("button", { name: "模板库", exact: true }).click();
  await page.locator(".library-menu .lib-item", { hasText: name }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
}

test("导入→原文核对→单条及当前评分项批量确认→刷新保留；不自动发布", async ({ page, request }) => {
  const { id, name } = await importTemplate(page);
  const panel = page.locator("[data-test=rule-review-panel]");
  await expect(page.getByRole("navigation", { name: "评分标准编辑步骤" })).toBeVisible();
  await panel.locator(".rule-source summary").first().click();
  await expect(panel).toContainText("研究方法应完整且可复现");
  await panel.getByRole("button", { name: "确认", exact: true }).first().click();
  await expect(page.getByRole("status").filter({ hasText: "已确认 1 条规则" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "确认", exact: true })).toHaveCount(1);
  await panel.getByRole("button", { name: /确认并应用全部/ }).click();
  await expect(panel.getByRole("button", { name: "确认", exact: true })).toHaveCount(0);
  const workspace = await (await request.get(`/api/rubrics/${id}/review-workspace`)).json();
  expect(workspace.rules.filter((r) => r.status === "approved")).toHaveLength(2);
  expect(workspace.rules.find((r) => r.rule_code === "thesis.result_quality.v1").status).toBe("draft");
  expect((await (await request.get(`/api/rubrics/${id}`)).json()).status).toBe("draft");
  await reopen(page, name);
  await expect(panel.getByRole("button", { name: "确认", exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("rubric-confirmed-desktop.png"), fullPage: true });
});

test("批量确认失败立即停止、保留成功条款并支持核对后重试", async ({ page, request }) => {
  const { id } = await importTemplate(page);
  let confirmations = 0;
  await page.route(`**/rubrics/${id}/rules/*/confirm`, async (route) => {
    confirmations += 1;
    if (confirmations === 2) return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "合成的版本冲突" }) });
    await route.continue();
  });
  const panel = page.locator("[data-test=rule-review-panel]");
  await panel.getByRole("button", { name: /确认并应用全部/ }).click();
  await expect(page.getByRole("alert").filter({ hasText: "已完成 1 条，后续操作已停止" })).toBeVisible();
  expect(confirmations).toBe(2);
  const workspace = await (await request.get(`/api/rubrics/${id}/review-workspace`)).json();
  expect(workspace.rules.filter((r) => r.status === "approved")).toHaveLength(1);
  await panel.getByRole("button", { name: "确认", exact: true }).click();
  await expect(panel.getByRole("button", { name: "确认", exact: true })).toHaveCount(0);
});

test("排除可撤销且不进入当前评分项批量确认", async ({ page, request }) => {
  const { id } = await importTemplate(page);
  const panel = page.locator("[data-test=rule-review-panel]");
  await panel.getByRole("button", { name: "排除", exact: true }).first().click();
  await expect(panel.getByRole("button", { name: /确认并应用全部/ })).toContainText("1");
  await panel.getByRole("button", { name: /确认并应用全部/ }).click();
  await expect(panel.getByRole("button", { name: "撤销排除" })).toBeVisible();
  expect((await (await request.get(`/api/rubrics/${id}/review-workspace`)).json()).rules.filter((r) => r.status === "approved")).toHaveLength(1);
  await panel.getByRole("button", { name: "撤销排除" }).click();
  await panel.getByRole("button", { name: "确认", exact: true }).click();
  await expect(panel.getByRole("button", { name: "确认", exact: true })).toHaveCount(0);
});

test("AI 补全保留原文输入，单条应用不覆盖前一条、不应用其它评分项", async ({ page, request }) => {
  const created = await request.post("/api/rubrics", { data: {
    name: `AI核对合成模板-${Date.now()}`, version: "v1", total_score: 20,
    criteria: ["T01", "T02"].map((code) => ({ code, name: code, max_score: 10,
      description: "核对方案论证", scoring_mode: "deductive", deduction_rules: ["缺少方案论证扣2至6分"] })),
  } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const rubric = await created.json();
  await page.route("**/api/ai-connections", (route) => route.fulfill({ json: [{ id: "synthetic", name: "合成测试连接", model_name: "fixture" }] }));
  const generatedCodes = [];
  let failSecond = true;
  await page.route(`**/api/rubrics/${rubric.id}/draft-deduction-rules`, async (route) => {
    const input = route.request().postDataJSON();
    expect(input.criteria).toHaveLength(1);
    generatedCodes.push(input.criteria[0].code);
    if (input.criteria[0].code === "T02" && failSecond) {
      failSecond = false;
      await route.fulfill({ status: 422, json: { detail: { code: "MUTEX_GROUP_MISSING", message: "缺少互斥标识。", user_action: "请重新生成。" } } });
      return;
    }
    expect(input.criteria[0].deduction_rules).toEqual(["缺少方案论证扣2至6分"]);
    expect(input.criteria[0].description).toBe("核对方案论证");
    await route.fulfill({ json: { items: input.criteria.map((c) => ({ criterion_code: c.code,
      draft: { criterion_code: c.code, generation_metadata: { provider: "fixture", model_name: "fixture", fingerprint: "f".repeat(64) },
        rule_groups: [{ group_code: "G1", issue: "方案论证不足", cap_points: 6, mutex_group: `${c.code}-G1`, rules: [
          { severity: "minor", trigger: "论证不充分", points: 2, reason: "轻微论证不足", source: "ai_interpreted_user_text", source_refs: ["原文扣分下限"] },
          { severity: "severe", trigger: "没有论证", points: 6, reason: "严重论证不足", source: "ai_interpreted_user_text", source_refs: ["原文扣分上限"] },
        ] }],
      },
    })) } });
  });
  await page.goto("/workbench/rubrics");
  await expect(page.getByRole("heading", { level: 1, name: rubric.name })).toBeVisible();
  await page.getByLabel("用哪个 AI 连接起草").selectOption("synthetic");
  await page.getByRole("button", { name: /生成全部缺失细则/ }).click();
  await expect(page.getByRole("alert").filter({ hasText: "已保留 1 项结果" })).toContainText("缺少互斥标识。");
  await page.getByRole("button", { name: "生成全部缺失细则（1）", exact: true }).click();
  await expect(page.getByRole("button", { name: /生成全部缺失细则|起草中/ })).toHaveCount(0);
  expect(generatedCodes).toEqual(["T01", "T02", "T02"]);
  const panel = page.locator("[data-test=draft-panel]");
  await expect(panel.locator("tbody tr")).toHaveCount(2);
  await panel.getByRole("button", { name: "确认", exact: true }).first().click();
  await expect(page.getByRole("status").filter({ hasText: "已应用并确认 1 条建议" })).toBeVisible();
  let full = await (await request.get(`/api/rubrics/${rubric.id}`)).json();
  expect(full.criteria.find((c) => c.code === "T01").deduction_rules_structured).toHaveLength(1);
  expect(full.criteria.find((c) => c.code === "T02").deduction_rules_structured).toHaveLength(0);
  await expect(panel.locator("tbody tr")).toHaveCount(1);
  await panel.getByRole("button", { name: "确认", exact: true }).click();
  await expect(panel).toHaveCount(0);
  // Recompile removes the draft panel before the separate confirm request finishes.
  await expect(page.getByRole("status").filter({ hasText: "已应用并确认 1 条建议" })).toBeVisible();
  full = await (await request.get(`/api/rubrics/${rubric.id}`)).json();
  expect(full.criteria.find((c) => c.code === "T01").deduction_rules_structured).toHaveLength(2);
  expect(full.criteria.find((c) => c.code === "T02").deduction_rules_structured).toHaveLength(0);
  const review = await (await request.get(`/api/rubrics/${rubric.id}/review-workspace`)).json();
  expect(review.rules.filter((r) => r.status === "approved")).toHaveLength(2);
  expect(review.structural_blockers.filter((b) => b.code === "deduct_rule_invalid")).toEqual([]);
  for (const rule of review.rules.filter(r => r.direction === "deduct")) {
    expect(rule.cap_points).toBeNull();
    expect(rule.origin.group_cap_points).toBe(6);
  }
  await page.locator(".criteria-nav .lib-item", { hasText: "T02" }).click();
  await expect(panel.locator("tbody tr")).toHaveCount(2);
});

test("保存失败保留修改，重试生成新草稿；完成确认后显式审核发布并冻结", async ({ page, request }) => {
  const { id } = await importTemplate(page);
  const before = await (await request.get(`/api/rubrics/${id}/review-workspace`)).json();
  await page.getByText("编辑当前评分项的原子规则", { exact: true }).click();
  const second = page.getByRole("group", { name: "thesis.method_reproducible.v1", exact: true });
  await second.getByLabel("条款正文", { exact: true }).fill("实验步骤应可复现，缺少步骤时转人工复核。");
  await second.getByRole("combobox", { name: "评分方向", exact: true }).selectOption("none");
  await second.getByRole("combobox", { name: "生效方式", exact: true }).selectOption("review");
  await second.getByRole("button", { name: "删除此档位" }).first().click();
  await second.getByRole("button", { name: "删除此档位" }).first().click();
  await page.getByRole("button", { name: /基本信息与评分项/ }).click();
  await page.getByLabel("标准说明", { exact: true }).fill("仅用于端到端验证的合成说明");
  await page.getByLabel("总分", { exact: true }).fill("20");
  await expect(page.getByText(/有未保存的修改/)).toBeVisible();
  await page.route(`**/api/rubrics/${id}/recompile`, (route) => route.fulfill({ status: 409, json: { detail: "合成保存冲突" } }));
  await page.getByRole("button", { name: "保存并重新校验" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "合成保存冲突" })).toBeVisible();
  await expect(page.getByLabel("标准说明", { exact: true })).toHaveValue("仅用于端到端验证的合成说明");
  await page.unroute(`**/api/rubrics/${id}/recompile`);
  await page.getByRole("button", { name: "保存并重新校验" }).click();
  await expect(page.getByText(/有未保存的修改/)).toHaveCount(0);
  const after = await (await request.get(`/api/rubrics/${id}/review-workspace`)).json();
  expect(after.rules.map(r => r.rule_code)).toEqual(before.rules.map(r => r.rule_code));
  expect(after.rules).toHaveLength(3);
  for (const rule of after.rules) expect(rule.sources).toEqual(before.rules.find(r => r.rule_code === rule.rule_code).sources);
  expect(after.rules.find(r => r.rule_code === 'thesis.method_reproducible.v1').rule_text).toContain('转人工复核');
  await page.getByRole("button", { name: /2 评分规则/ }).click();
  await page.locator("[data-test=rule-review-panel]").getByRole("button", { name: /确认并应用全部/ }).click();
  await expect(page.locator("[data-test=rule-review-panel]").getByRole("button", { name: "确认", exact: true })).toHaveCount(0);
  await page.locator(".criteria-nav .lib-item", { hasText: "RESULT" }).click();
  await page.locator("[data-test=rule-review-panel]").getByRole("button", { name: /确认并应用全部/ }).click();
  await expect(page.getByRole("button", { name: "提交模板审核" })).toBeEnabled();
  expect((await (await request.get(`/api/rubrics/${id}`)).json()).status).toBe("draft");
  await page.getByRole("button", { name: "提交模板审核" }).click();
  await expect(page.getByRole("button", { name: "发布", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "发布", exact: true }).click();
  await expect(page.getByRole("button", { name: "复制为新版本" })).toBeVisible();
  expect((await (await request.get(`/api/rubrics/${id}`)).json()).status).toBe("published");
  await page.getByRole("button", { name: /2 评分规则/ }).click();
  await expect(page.locator("[data-test=rule-review-panel]").getByRole("button", { name: /确认并应用全部/ })).toBeDisabled();
});

test("存量单次上限冲突可显式清除并保存新草稿，其它评分项保持原样", async ({ page, request }) => {
  const { id, name } = await importTemplate(page);
  const before = await (await request.get(`/api/rubrics/${id}/review-workspace`)).json();
  const full = await (await request.get(`/api/rubrics/${id}`)).json();
  const invalid = await request.post(`/api/rubrics/${id}/recompile`, { data: {
    version: 'legacy-conflict', criteria: full.criteria.map(c => ({...c, scoring_mode:'deductive', rubric_levels:[]})), total_score: full.criteria.reduce((sum, c) => sum + Number(c.max_score), 0),
    supersedes_compilation_id: before.compilation_id,
    atomic_rules: before.rules.map(r => ({id:r.id, content_token:r.content_token,
      changes:{direction:'deduct', effect_type:'score', max_points:2, repeat_policy:'once', cap_points:6, levels:[]}})),
  } });
  expect(invalid.ok(), await invalid.text()).toBeTruthy();
  await reopen(page, name);
  await expect(page.getByRole('alert').filter({hasText:'3 个校验阻断'})).toContainText('单条累计上限');
  await page.getByText('编辑当前评分项的原子规则', {exact:true}).click();
  const editor = page.locator('.atomic-editor');
  await expect(editor).toHaveCount(2);
  await expect(editor.first().getByLabel('单条累计上限', {exact:true})).toBeDisabled();
  await editor.first().getByRole('button', {name:'清除不适用的单条上限',exact:true}).click();
  await editor.nth(1).getByRole('button', {name:'清除不适用的单条上限',exact:true}).click();
  await page.getByRole('button', {name:'保存并重新校验',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'仍有 1 个校验阻断'})).toBeVisible();
  const after = await (await request.get(`/api/rubrics/${id}/review-workspace`)).json();
  expect(after.rules.filter(r => r.cap_points === null)).toHaveLength(2);
  expect(after.rules.find(r => r.rule_code === 'thesis.result_quality.v1').cap_points).toBe('6');
  for (const r of after.rules) {
    expect(r.max_points).toBe('2');
    expect(r.repeat_policy).toBe('once');
    expect(r.sources).toEqual(before.rules.find(b => b.rule_code === r.rule_code).sources);
  }
});
