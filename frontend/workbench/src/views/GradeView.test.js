import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import GradeView from "./GradeView.vue";

/** 按路径返回的假接口；`gate` 可以让材料列表先挂起，模拟冷启动。 */
function stubFetch(table, gate = null) {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url) => {
    const path = String(url).replace("/api", "").split("?")[0];
    if (gate && path === "/papers") await gate;
    const body = table[path];
    if (body === undefined) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

async function mountGrade() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/batches/:batchId/grade", name: "grade", component: GradeView },
    { path: "/tasks/:batchId/run", name: "task-run", component: { template: "<div />" } },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
  ] });
  await router.push("/batches/b1/grade");
  await router.isReady();
  return mount(GradeView, { global: { plugins: [router] } });
}

beforeEach(() => {
  setActivePinia(createPinia());
  globalThis.__PGS_CONFIG__ = undefined;
});
afterEach(() => vi.unstubAllGlobals());

const TABLE = {
  "/papers": [{ id: "p1", student_id: "22008020625", title: "老年安全监测系统", status: "parsed", file_name: "a.docx" }],
  "/batches/b1": { id: "b1", business_profile_key: "thesis" },
  "/scoring-runs": [],
  "/papers/p1/neighbors": { batch_id: "b1", paper_id: "p1", position: 1, total: 1, previous_paper_id: null, next_paper_id: null },
};

it("材料还没回来时显示加载中，而不是“0 份”“未命名材料”", async () => {
  let release;
  stubFetch(TABLE, new Promise((resolve) => { release = resolve; }));
  const wrapper = await mountGrade();
  await flushPromises();
  expect(wrapper.text()).toContain("正在加载");
  expect(wrapper.text()).not.toContain("未命名材料");
  expect(wrapper.text()).not.toContain("材料 · 0");
  release();
  await flushPromises();
  expect(wrapper.text()).toContain("22008020625");
  wrapper.unmount();
});

it("没有评分结果的材料给出下一步：到评分进度页查看原因或开始评分", async () => {
  stubFetch(TABLE);
  const wrapper = await mountGrade();
  await flushPromises();
  const hint = wrapper.get('[data-test="no-result"]');
  expect(hint.text()).toContain("还没有评分结果");
  expect(hint.get("a").attributes("href")).toBe("/tasks/b1/run");
  wrapper.unmount();
});

// ---------- AI 没评出有效结果的项（2026-10-19 生产反馈：改分「点了没反应」）----------

const ITEMS = [
  {
    id: "i1", criterion_code: "T01", criterion_name: "选题与开题", max_score: 20,
    ai_score: null, final_score: null, auto_score_status: "invalid", need_manual_review: true,
    confidence: null, evidence_view: [],
  },
  {
    id: "i2", criterion_code: "T06", criterion_name: "论文撰写与规范", max_score: 20,
    ai_score: 10, final_score: 10, auto_score_status: "calculated", need_manual_review: false,
    confidence: 0.8, evidence_view: [],
  },
];

function withRun(items, patchResponse = null) {
  const table = {
    ...TABLE,
    "/scoring-runs": [{ id: "r1", coherence_findings: [], format_findings: [] }],
    "/scoring-runs/r1/items": items,
    "/scoring-runs/r1/document-view": null,
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url, init = {}) => {
    const path = String(url).replace("/api", "").split("?")[0];
    if (init.method === "PATCH" && patchResponse) {
      return new Response(JSON.stringify(patchResponse.body), {
        status: patchResponse.status, headers: { "Content-Type": "application/json" },
      });
    }
    const body = table[path];
    if (body === undefined) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

it("AI 没评出有效结果的项不给「改分」，当场说明并指向「重试失败项」", async () => {
  withRun(ITEMS);
  const wrapper = await mountGrade();
  await flushPromises();

  const [invalid, calculated] = wrapper.findAll(".item");
  const note = invalid.get('[data-test="item-unresolved"]');
  expect(note.text()).toContain("AI 没有评出这一项的有效结果，不能直接改分");
  expect(note.get("a").attributes("href")).toBe("/tasks/b1/run");
  expect(invalid.find(".edit-trigger").exists()).toBe(false);
  expect(calculated.find(".edit-trigger").exists()).toBe(true);
  wrapper.unmount();
});

it("还有无效项时这份评分确认不了：按钮禁用并说明原因", async () => {
  withRun(ITEMS);
  const wrapper = await mountGrade();
  await flushPromises();

  expect(wrapper.get('[data-test="run-unresolved"]').text()).toContain("有 1 项 AI 没评出有效结果");
  const confirm = wrapper.findAll(".run-actions .form-actions button");
  expect(confirm).toHaveLength(2);
  for (const button of confirm) expect(button.attributes("disabled")).toBeDefined();
  wrapper.unmount();
});

it("改分失败的提示出现在「保存」旁边，并且是中文", async () => {
  withRun([ITEMS[1]], { status: 400, body: { detail: "final_score out of range" } });
  const wrapper = await mountGrade();
  await flushPromises();

  await wrapper.get(".edit-trigger").trigger("click");
  await wrapper.get(".edit-box textarea").setValue("复核后调整");
  await wrapper.get(".edit-box .btn-primary").trigger("click");
  await flushPromises();

  const error = wrapper.get('.edit-box [data-test="edit-error"]');
  expect(error.text()).toBe("分数要在 0 到 20 之间。");
  expect(wrapper.find('.run-actions [role="alert"]').exists()).toBe(false);
  wrapper.unmount();
});

it("分数越界当场拦下，不发请求", async () => {
  withRun([ITEMS[1]]);
  const wrapper = await mountGrade();
  await flushPromises();

  await wrapper.get(".edit-trigger").trigger("click");
  await wrapper.get('.edit-box input[type="number"]').setValue("25");
  await wrapper.get(".edit-box textarea").setValue("复核后调整");
  await wrapper.get(".edit-box .btn-primary").trigger("click");
  await flushPromises();

  expect(wrapper.get('[data-test="edit-error"]').text()).toBe("分数要在 0 到 20 之间。");
  expect(fetch.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
  wrapper.unmount();
});

it("没对上的英文报错不原样给用户，给中文说明和状态码", async () => {
  withRun([ITEMS[1]], { status: 400, body: { detail: "some unexpected backend failure" } });
  const wrapper = await mountGrade();
  await flushPromises();

  await wrapper.get(".edit-trigger").trigger("click");
  await wrapper.get(".edit-box textarea").setValue("复核后调整");
  await wrapper.get(".edit-box .btn-primary").trigger("click");
  await flushPromises();

  const text = wrapper.get('[data-test="edit-error"]').text();
  expect(text).toBe("改分没有保存（HTTP 400），请稍后重试；问题持续请联系管理员。");
  expect(text).not.toContain("unexpected");
  wrapper.unmount();
});

it("并发冲突（409）提示先刷新看清当前分数，不叫用户「稍后重试」", async () => {
  withRun([ITEMS[1]], { status: 409, body: { detail: "review revision conflict" } });
  const wrapper = await mountGrade();
  await flushPromises();

  await wrapper.get(".edit-trigger").trigger("click");
  await wrapper.get(".edit-box textarea").setValue("复核后调整");
  await wrapper.get(".edit-box .btn-primary").trigger("click");
  await flushPromises();

  const text = wrapper.get('[data-test="edit-error"]').text();
  expect(text).toBe("这份评分刚被别人改过，请刷新页面、看清当前分数后再操作。");
  expect(text).not.toContain("稍后重试");
  wrapper.unmount();
});

it("每个评分项下显示扣分理由与修改建议：助手按这些说“怎么改”，老师在这里核对依据", async () => {
  withRun([
    { ...ITEMS[1], id: "i3", deduction_items: [{ reason: "未说明研究方法", points: 2 }],
      revision_suggestion: "补充研究方法的步骤与依据。" },
    { ...ITEMS[1], id: "i4", deduction_items: [], deductions: ["文献综述不充分"], revision_suggestion: null },
  ]);
  const wrapper = await mountGrade();
  await flushPromises();

  const [withAdvice, withoutAdvice] = wrapper.findAll(".item");
  expect(withAdvice.get('[data-test="item-deductions"]').text()).toContain("未说明研究方法");
  expect(withAdvice.get('[data-test="item-suggestion"]').text()).toBe("修改建议：补充研究方法的步骤与依据。");
  expect(withoutAdvice.get('[data-test="item-deductions"]').text()).toContain("文献综述不充分");
  expect(withoutAdvice.find('[data-test="item-suggestion"]').exists()).toBe(false);
  wrapper.unmount();
});

// ---------- Core 评分项：逐条规则判定与证据充分度（docs/评分证据与理由改造方案.md）----------

const JUDGEMENT = {
  schema_version: "judgement-view@1",
  rules: [
    { rule_code: "R1", rule_name: "研究方法描述不清", judge_type: "semantic", outcome: "deducted", label: "扣分",
      effect: "扣 2 分", rationale: "方法部分没有说明抽样过程。",
      evidence: [{ quote: "本文采用问卷调查", section_title: "第3章 研究方法", location_status: "verified", anchor_id: "u1" }],
      basis_evidence: [] },
    { rule_code: "R2", rule_name: "结论未回应研究问题", judge_type: "semantic", outcome: "not_triggered", label: "未发现问题",
      effect: null, rationale: null,
      evidence: [], basis_evidence: [{ quote: null, section_title: "第6章 结论", location_status: "unit_context", anchor_id: "u2" }] },
  ],
  evidence_sufficiency: { level: "medium", label: "中", reasons: ["规则「研究方法描述不清」的证据受长度上限限制，少放了 3 段。"], notes: [] },
};

it("Core 评分项显示证据充分度和逐条规则判定，未扣分的规则也有说明，不再显示“未提供”的置信度", async () => {
  withRun([
    { ...ITEMS[1], id: "i5", confidence: null, judgement_view: JUDGEMENT,
      deduction_items: [{ reason: "规则「研究方法描述不清」扣 2 分", points: "2" }], evidence_view: [] },
  ]);
  const wrapper = await mountGrade();
  await flushPromises();

  const item = wrapper.get(".item");
  expect(item.get('[data-test="evidence-sufficiency"]').text()).toContain("证据充分度");
  expect(item.get('[data-test="evidence-sufficiency"]').text()).toContain("中");
  expect(item.text()).toContain("少放了 3 段");
  expect(item.text()).not.toContain("未提供");
  expect(item.text()).not.toContain("该项没有记录证据");
  // 扣分理由由判定清单承担，不再重复一份扣分列表。
  expect(item.find('[data-test="item-deductions"]').exists()).toBe(false);

  const rules = item.get('[data-test="rule-judgements"]');
  expect(rules.text()).toContain("研究方法描述不清");
  expect(rules.text()).toContain("扣 2 分");
  expect(rules.text()).toContain("方法部分没有说明抽样过程。");
  expect(rules.text()).toContain("结论未回应研究问题");
  expect(rules.text()).toContain("未发现问题");
  expect(rules.get('[data-test="rule-quote"]').text()).toContain("“本文采用问卷调查”");
  expect(rules.get('[data-test="rule-context"]').text()).toContain("第6章 结论 · 上下文");
  wrapper.unmount();
});

it("旧评分路径的评分项照旧显示模型置信度和扣分理由", async () => {
  withRun([{ ...ITEMS[1], id: "i6", judgement_view: null, deduction_items: [{ reason: "未说明研究方法", points: 2 }] }]);
  const wrapper = await mountGrade();
  await flushPromises();
  const item = wrapper.get(".item");
  expect(item.text()).toContain("置信度");
  expect(item.text()).toContain("80%");
  expect(item.find('[data-test="rule-judgements"]').exists()).toBe(false);
  expect(item.get('[data-test="item-deductions"]').text()).toContain("未说明研究方法");
  wrapper.unmount();
});
