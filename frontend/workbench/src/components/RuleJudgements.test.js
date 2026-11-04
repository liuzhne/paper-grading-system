import { mount } from "@vue/test-utils";
import { expect, it } from "vitest";

import RuleJudgements from "./RuleJudgements.vue";

const VIEW = {
  rules: [
    { rule_code: "R1", rule_name: "研究方法描述不清", outcome: "deducted", label: "扣分", effect: "扣 2 分", rationale: null,
      evidence: [{ quote: "本文采用问卷调查", section_title: "第3章", location_status: "verified", anchor_id: "u1" }],
      basis_evidence: [{ quote: "样本量为 240", section_title: "第3章", location_status: "verified", anchor_id: "u1" }] },
    { rule_code: "R2", rule_name: "格式", outcome: "invalid", label: "判定无效", effect: null, rationale: null,
      evidence: [{ quote: null, section_title: null, location_status: "anchor_missing", unlocatable_reason: "冻结正文读不到", anchor_id: "u9" }],
      basis_evidence: [] },
  ],
};

it("引文按钮点击后发出定位；依据引文标“依据”；定位不了的证据禁用并说明原因", async () => {
  const wrapper = mount(RuleJudgements, { props: { view: VIEW } });
  const quotes = wrapper.findAll('[data-test="rule-quote"]');
  expect(quotes.map((node) => node.text())).toEqual(["“本文采用问卷调查”", "依据 “样本量为 240”"]);
  await quotes[0].trigger("click");
  expect(wrapper.emitted("locate")[0][0].anchor_id).toBe("u1");

  const missing = wrapper.get('[data-test="rule-context"]');
  expect(missing.attributes("disabled")).toBeDefined();
  expect(missing.attributes("title")).toBe("冻结正文读不到");
  expect(missing.text()).toBe("冻结正文中找不到");
});
