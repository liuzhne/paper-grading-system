import { describe, expect, it } from "vitest";

import { STEPS, measureSection, overlayState, productState, stillProductStates } from "./landing-motion.js";

/**
 * 宣传页滚动动画。动画算错不会报错，只会让某一屏停在半透明、两步叠在一起或
 * 永远盖着波浪——这里按滚动的几个关键位置断言每一层「该不该看得见」。
 */
describe("measureSection", () => {
  it("区块第一屏被转场盖着，进度从再滚过一屏开始算", () => {
    const vh = 800;
    // 区块高 580vh；顶部刚到视口顶：-top = 0 < vh，进度仍为 0。
    expect(measureSection({ top: 0, height: 5.8 * vh }, vh, 2000).p).toBe(0);
    // 再滚过一屏：进度开始走。
    expect(measureSection({ top: -vh - 100, height: 5.8 * vh }, vh, 2900).p).toBeGreaterThan(0);
    // 滚到区块末尾：进度到 1，不越界。
    expect(measureSection({ top: -10 * vh, height: 5.8 * vh }, vh, 9000).p).toBe(1);
  });

  it("B 是区块在文档里的绝对顶部", () => {
    expect(measureSection({ top: 300, height: 4000 }, 800, 700).B).toBe(1000);
  });
});

describe("productState", () => {
  const view = (p, vw = 1280, vh = 800) => productState({ p, vw, vh });

  it("开头只显示第一步，评分项待核对", () => {
    const state = view(0);
    expect(state.captions.map((c) => c.opacity)).toEqual([1, 0, 0, 0]);
    expect(state.criteria.opacity).toBe(1);
    expect(state.evidence.opacity).toBe(0);
    expect(state.criteria.rows.every((r) => !r.checked)).toBe(true);
    expect(state.frame.crumb).toBe(STEPS[0].crumb);
    expect(state.rail.map((r) => r.current)).toEqual([true, false, false, false]);
  });

  it("结尾停在导出：成绩单数字到位、导出按钮可见", () => {
    const state = view(1);
    expect(state.captions.map((c) => c.opacity)).toEqual([0, 0, 0, 1]);
    expect(state.transcript.opacity).toBe(1);
    expect(state.transcript.count).toBe(24);
    expect(state.transcript.average).toBe("81.4");
    expect(state.transcript.exportOpacity).toBe(1);
    expect(state.frame.crumb).toBe(STEPS[3].crumb);
  });

  it("复核那一步把低置信度项标出来", () => {
    // 第三步走完一大半：q≈0.69 → s[2]≈0.75。
    const state = view(0.68);
    const flagged = state.review.rows.filter((r) => r.flagged).map((r) => r.name);
    expect(flagged).toEqual(["研究方法与技术方案", "结论与创新性"]);
  });

  it("任一时刻最多两步同时可见（交叉淡入淡出），不会全空", () => {
    for (let p = 0; p <= 1; p += 0.01) {
      const visible = view(p).captions.filter((c) => c.opacity > 0).length;
      expect(visible).toBeGreaterThanOrEqual(1);
      expect(visible).toBeLessThanOrEqual(2);
    }
  });

  it("窄屏单栏，矮屏缩小窗口并用负外边距还回占位", () => {
    const narrow = view(0.5, 390, 700);
    expect(narrow.frame.wide).toBe(false);
    expect(narrow.frame.scale).toBeLessThan(1);
    expect(narrow.frame.marginBottom).toBeLessThan(0);

    const roomy = view(0.5, 1440, 1000);
    expect(roomy.frame.wide).toBe(true);
    expect(roomy.frame.marginBottom).toBeCloseTo(0);
  });
});

describe("overlayState", () => {
  const vh = 800;
  const B = 1200; // 首屏比一屏略高

  it("首屏：波浪停在首屏底部，箭头可点，标志不显示", () => {
    const state = overlayState({ y: 0, vh, B });
    expect(state.shift).toBe(B - vh);
    expect(state.hidden).toBe(false);
    expect(state.arrowOpacity).toBe(1);
    expect(state.arrowInteractive).toBe(true);
    expect(state.logoOpacity).toBe(0);
    expect(state.paths).toHaveLength(3);
    expect(state.paths[0]).toMatch(/^M[\d.\s L-]+Z$/);
  });

  it("盖满屏幕时显示标志，箭头已退场", () => {
    const state = overlayState({ y: B, vh, B });
    expect(state.shift).toBe(0);
    expect(state.logoOpacity).toBe(1);
    expect(state.arrowInteractive).toBe(false);
  });

  it("转场结束后整层隐藏，「探索更多」正好滚到这里", () => {
    const { target } = overlayState({ y: 0, vh, B });
    expect(target).toBe(B + vh);
    expect(overlayState({ y: target, vh, B }).hidden).toBe(true);
  });
});

describe("stillProductStates（减少动态效果）", () => {
  const stills = stillProductStates({ vw: 1440, vh: 900 });

  it("四步各一张，每张只露出自己那一步", () => {
    expect(stills).toHaveLength(4);
    stills.forEach((state, i) => {
      const panels = [state.criteria, state.evidence, state.review, state.transcript].map((x) => x.opacity);
      panels.forEach((opacity, j) => {
        if (j === i) expect(opacity, `第 ${i + 1} 张的面板 ${j + 1}`).toBeCloseTo(1, 3);
        else expect(opacity, `第 ${i + 1} 张的面板 ${j + 1}`).toBe(0);
      });
      expect(state.frame.crumb).toBe(STEPS[i].crumb);
    });
  });

  it("每一步都停在演示完成的样子，窗口不缩放不淡入", () => {
    const [criteria, evidence, review, transcript] = stills;
    expect(criteria.criteria.rows.every((r) => r.checked && r.opacity === 1)).toBe(true);
    expect(evidence.evidence.highlight).toBe(1);
    expect(evidence.evidence.cardOpacity).toBe(1);
    expect(review.review.rows.filter((r) => r.flagged).map((r) => r.name)).toEqual(["研究方法与技术方案", "结论与创新性"]);
    expect(transcript.transcript.count).toBe(24);
    expect(transcript.transcript.exportOpacity).toBe(1);
    for (const state of stills) {
      expect(state.frame).toMatchObject({ scale: 1, opacity: 1, marginBottom: 0 });
    }
  });
});
