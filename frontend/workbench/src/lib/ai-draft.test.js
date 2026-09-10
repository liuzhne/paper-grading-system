import { describe, expect, it } from "vitest";

import {
  SEVERITY_LABEL,
  draftRows,
  confirmedStructuredRules,
  mergeConfirmedDrafts,
  rowKey,
} from "./ai-draft.js";

/**
 * AI 起草结果 → 可执行扣分规则（V3-2 闭环）。
 *
 * 起草端点**不落库**（后端 docstring 写明 non-persistent）。确认之后必须由前端
 * 把规则合进评分项、再走 `recompile` 才能进入可执行版本。上一版前端拿到起草结果
 * 存进 store 就再没用过，用户点了按钮、花了一次真实调用，页面纹丝不动。
 */
const DRAFT = {
  schema_version: "ai-rule-draft@1",
  criterion_code: "T02",
  requires_confirmation: true,
  rule_groups: [
    {
      group_code: "G1",
      issue: "文献综述覆盖不足",
      mutex_group: "T02-coverage",
      cap_points: 6,
      rules: [
        {
          severity: "minor",
          trigger: "引用文献少于 15 篇",
          points: 2,
          reason: "覆盖面偏窄",
          repeat_policy: "once",
          source: "ai_inferred",
          source_refs: ["/criteria/T02/deduction_rules/0"],
        },
        {
          severity: "severe",
          trigger: "缺少近三年文献",
          points: 5,
          reason: "时效性不足",
          repeat_policy: "once",
          source: "user_text",
          source_refs: ["/criteria/T02/deduction_rules/1"],
        },
      ],
    },
  ],
  generation_metadata: {
    provider: "openai_compatible",
    model_name: "glm-4",
    prompt_version: "2026-09-01",
    fingerprint: "f".repeat(64),
  },
};

describe("起草结果展开成表格行", () => {
  it("每条严重程度规则一行，带上所属规则组的问题类型", () => {
    const rows = draftRows(DRAFT);

    expect(rows).toHaveLength(2);
    expect(rows[0].issue).toBe("文献综述覆盖不足");
    expect(rows[0].trigger).toBe("引用文献少于 15 篇");
    expect(rows[0].points).toBe(2);
  });

  it("严重程度译成中文，未知值原样透出而不是显示空白", () => {
    expect(SEVERITY_LABEL.minor).toBe("轻微");
    expect(SEVERITY_LABEL.severe).toBe("严重");
    const rows = draftRows({
      ...DRAFT,
      rule_groups: [
        { ...DRAFT.rule_groups[0], rules: [{ ...DRAFT.rule_groups[0].rules[0], severity: "blocker" }] },
      ],
    });
    // 显示成空白等于把一条真实存在的规则藏起来。
    expect(rows[0].severityLabel).toBe("blocker");
  });

  it("来源分「原文」与「AI 起草」——用户要能看出哪几条是模型编的", () => {
    const rows = draftRows(DRAFT);

    expect(rows[0].sourceLabel).toBe("AI 起草");
    expect(rows[1].sourceLabel).toBe("原文");
  });

  it("行键在同一评分项内唯一，排除某一行不会顺带排除另一行", () => {
    const rows = draftRows(DRAFT);

    expect(rowKey(rows[0])).not.toBe(rowKey(rows[1]));
  });
});

describe("确认后的结构化规则", () => {
  it("只收被确认的行，被排除的不进入可执行版本", () => {
    const rows = draftRows(DRAFT);
    const excluded = new Set([rowKey(rows[0])]);

    const rules = confirmedStructuredRules(DRAFT, excluded);

    expect(rules).toHaveLength(1);
    expect(rules[0].trigger).toBe("缺少近三年文献");
  });

  it("带上组级的互斥标识与扣分上限——丢了这两项，同一问题会被重复扣分", () => {
    const rules = confirmedStructuredRules(DRAFT, new Set());

    expect(rules[0].mutex_group).toBe("T02-coverage");
    expect(rules[0].cap_points).toBe(6);
  });

  it("带上生成指纹，确认记录能追回是哪一次生成", () => {
    const rules = confirmedStructuredRules(DRAFT, new Set());

    expect(rules[0].generation_fingerprint).toBe("f".repeat(64));
    expect(rules[0].confirmed).toBe(true);
  });

  it("`match` 与 `trigger` 同时给出：编译器读 match，界面读 trigger", () => {
    const rules = confirmedStructuredRules(DRAFT, new Set());

    expect(rules[0].match).toBe(rules[0].trigger);
  });

  it("全部排除时返回空数组，而不是 undefined", () => {
    const rows = draftRows(DRAFT);
    const excluded = new Set(rows.map(rowKey));

    expect(confirmedStructuredRules(DRAFT, excluded)).toEqual([]);
  });
});

describe("合并进评分项", () => {
  const CRITERIA = [
    { code: "T01", name: "选题", max_score: 10, scoring_mode: "llm_direct", deduction_rules_structured: [] },
    { code: "T02", name: "文献综述", max_score: 15, scoring_mode: "review_only", deduction_rules_structured: [] },
  ];

  it("只改有起草结果的那一项，其它项原样带过", () => {
    const merged = mergeConfirmedDrafts(CRITERIA, [{ criterion_code: "T02", draft: DRAFT }], new Set());

    expect(merged[0]).toEqual(CRITERIA[0]);
    expect(merged[1].deduction_rules_structured).toHaveLength(2);
  });

  it("确认后的评分项改为逐项扣分——规则写进去了却还按整体判分，等于没生效", () => {
    const merged = mergeConfirmedDrafts(CRITERIA, [{ criterion_code: "T02", draft: DRAFT }], new Set());

    expect(merged[1].scoring_mode).toBe("deductive");
  });

  it("一条都没确认的评分项保持原样，不把 scoring_mode 改成 deductive", () => {
    const rows = draftRows(DRAFT);
    const excluded = new Set(rows.map(rowKey));

    const merged = mergeConfirmedDrafts(CRITERIA, [{ criterion_code: "T02", draft: DRAFT }], excluded);

    // 改成 deductive 却没有任何规则，评分时这一项恒得满分——比不改更糟。
    expect(merged[1].scoring_mode).toBe("review_only");
    expect(merged[1].deduction_rules_structured).toEqual([]);
  });

  it("起草结果指向一个不存在的评分项时忽略它，不凭空造出一项", () => {
    const merged = mergeConfirmedDrafts(CRITERIA, [{ criterion_code: "T99", draft: DRAFT }], new Set());

    expect(merged).toHaveLength(2);
  });

  it("不改动传入的评分项数组", () => {
    const before = JSON.stringify(CRITERIA);
    mergeConfirmedDrafts(CRITERIA, [{ criterion_code: "T02", draft: DRAFT }], new Set());

    expect(JSON.stringify(CRITERIA)).toBe(before);
  });
});
