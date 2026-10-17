/**
 * 宣传页的滚动动画：纯函数，输入滚动位置与视口尺寸，输出各层的样式值。
 *
 * 逻辑与数值照搬 Claude Design 原型「有据智评 宣传页.dc.html」的 `scrollVals()` 与
 * `overlayVals()`。拆成纯函数是为了能单测：动画错位不会让任何东西报错，只会让
 * 某一屏停在半透明状态。
 *
 * 页面分三段：
 * 1. 首屏（深色）。三层波浪停在首屏底部，「探索更多」箭头压在波浪上。
 * 2. 转场。波浪随滚动上涌盖满屏幕（a: 0→1），中间出现标志；再继续滚动，波浪从
 *    底部退出（b: 0→1），露出下面的产品演示。
 * 3. 产品演示：580vh 高的区块里一屏 sticky，四步（识别评分项 → 给分与证据 →
 *    人工复核 → 导出）随滚动进度 p 依次切换。
 */

const clamp01 = (x) => Math.max(0, Math.min(1, x));
const smooth = (t) => t * t * (3 - 2 * t);

export const STEPS = [
  {
    rail: "识别评分项",
    title: "上传评分表，评分项自动成形",
    body: "评分项、满分和评分说明从表格中逐行识别。取自文字的分值会标出来，等你核对。",
    crumb: "评分标准 05 › 评分项",
  },
  {
    rail: "给分与证据",
    title: "每一项给分，都能回到原文",
    body: "给分时同时记下页码和章节。点一下证据，就跳到论文里的那一段。",
    crumb: "评分工作区 › SE-2026-014",
  },
  {
    rail: "人工复核",
    title: "拿不准的，交给老师",
    body: "置信度低于阈值的给分自动进入复核，每一次改分都写入记录。",
    crumb: "结果复核 › SE-2026-014",
  },
  {
    rail: "导出",
    title: "复核完，成绩单一次导出",
    body: "HTML 评审报告、Excel 成绩单和结构化数据同时生成。",
    crumb: "输出中心 › 批次 1",
  },
];

const CRITERIA = [
  ["T01", "选题与开题", 20],
  ["T02", "分析与解决问题1", 20],
  ["T03", "分析与解决问题2", 20],
  ["T04", "分析与解决问题3", 10],
  ["T05", "分析与解决问题4", 10],
  ["T06", "论文撰写与规范", 20],
];

const CONFIDENCE = [
  ["研究方法与技术方案", 61],
  ["选题与意义", 94],
  ["文献综述", 88],
  ["实现与实验", 83],
  ["结论与创新性", 57],
  ["写作规范与引用", 96],
];

const DISTRIBUTION = [8, 22, 52, 76, 24];

/** 产品窗口内容区的自然高度（px）；矮屏按比例缩小。 */
const WINDOW_BODY = 440;
const WINDOW_BAR = 46;

/** 两栏布局的最小视口宽度；更窄时说明文字在上、窗口在下。 */
export const WIDE_MIN = 960;

/**
 * 由产品演示区块的位置算滚动进度。
 *
 * 区块的第一屏被转场波浪盖着，所以进度从「区块顶部再往上滚过一屏」开始计。
 *
 * @param {{ top: number, height: number }} rect 区块的 getBoundingClientRect()
 * @param {number} vh 视口高度
 * @param {number} scrollY
 * @returns {{ p: number, B: number }} p 为 0–1 的进度，B 为区块在文档中的绝对顶部
 */
export function measureSection(rect, vh, scrollY) {
  const total = rect.height - vh - vh;
  return {
    p: clamp01((-rect.top - vh) / (total || 1)),
    B: rect.top + scrollY,
  };
}

/**
 * 产品演示一屏内各元素的状态。
 *
 * @param {{ p: number, vw: number, vh: number }} view
 */
export function productState({ p, vw, vh }) {
  const wide = vw >= WIDE_MIN;
  const q = clamp01((p - 0.06) / 0.9);
  // s[i]：第 i 步自身的进度（0–1）。
  const s = [0, 1, 2, 3].map((i) => clamp01(q * 4 - i));
  const op = (i) => (i === 0 ? 1 : clamp01(s[i] / 0.12)) * (i === 3 ? 1 : clamp01((1 - s[i]) / 0.12));
  const current = Math.min(3, Math.floor(q * 4));

  const captions = STEPS.map((step, i) => ({
    index: `0${i + 1} / 04`,
    title: step.title,
    body: step.body,
    opacity: op(i),
    shift: (1 - op(i)) * (s[i] >= 1 ? -18 : 18),
  }));
  const rail = STEPS.map((step, i) => ({
    label: step.rail,
    fill: s[i],
    current: i === current,
  }));

  const checked = s[0] > 0.85;
  const criteria = {
    opacity: op(0),
    rows: CRITERIA.map(([code, name, max], k) => {
      const shown = clamp01((s[0] - k * 0.09) / 0.15);
      return { code, name, max, opacity: shown, shift: (1 - shown) * 28, checked };
    }),
  };

  const card = clamp01((s[1] - 0.5) / 0.25);
  const evidence = {
    opacity: op(1),
    highlight: clamp01(s[1] / 0.35),
    dash: 1 - clamp01((s[1] - 0.3) / 0.3),
    cardOpacity: card,
    cardShift: (1 - card) * 16,
  };

  const flag = clamp01((s[2] - 0.55) / 0.2);
  const review = {
    opacity: op(2),
    rows: CONFIDENCE.map(([name, value]) => {
      const f = value < 70 ? flag : 0;
      const shown = Math.round(value * clamp01(s[2] / 0.5));
      return { name, value: shown, flagged: f > 0.5, tag: f };
    }),
  };

  const grow = clamp01(s[3] / 0.5);
  const bars = clamp01((s[3] - 0.1) / 0.5);
  const transcript = {
    opacity: op(3),
    count: Math.round(24 * grow),
    average: (81.4 * grow).toFixed(1),
    exportOpacity: clamp01((s[3] - 0.55) / 0.25),
    bars: DISTRIBUTION.map((h, i) => ({ height: h * bars, highlight: i === 3 })),
  };

  const captionHeight = wide ? 320 : vh < 640 ? 170 : 230;
  const available = wide ? vh - WINDOW_BAR - 60 : vh - captionHeight - 28 - WINDOW_BAR - 40;
  const fit = Math.max(0.55, Math.min(1, available / WINDOW_BODY));
  const frame = {
    wide,
    captionHeight,
    bodyHeight: WINDOW_BODY,
    scale: (0.86 + 0.14 * clamp01(p / 0.08)) * fit,
    // 缩放不改变占位高度；用负的下外边距把缩掉的那部分还回去，窗口下方不留白。
    marginBottom: -(WINDOW_BODY + WINDOW_BAR) * (1 - fit),
    opacity: 0.35 + 0.65 * clamp01(p / 0.05),
    crumb: STEPS[current].crumb,
  };

  return { captions, rail, criteria, evidence, review, transcript, frame };
}

/**
 * 一条起伏的波浪边：两组正弦叠加，避免看起来像机械的正弦曲线。
 *
 * @returns {Array<[number, number]>}
 */
function waveEdge(base, amplitude, phase, frequency) {
  const N = 36;
  const points = [];
  for (let k = 0; k <= N; k++) {
    const x = (1440 * k) / N;
    const u = (x / 1440) * Math.PI * 2 * frequency;
    points.push([x, base + amplitude * Math.sin(u + phase) + amplitude * 0.45 * Math.sin(u * 2.3 + phase * 1.7)]);
  }
  return points;
}

/**
 * 转场波浪层（固定定位、盖在所有内容之上，坐标系是 1440×1000 的 viewBox）。
 *
 * @param {{ y: number, vh: number, B: number }} view B 为产品演示区块的绝对顶部
 */
export function overlayState({ y, vh, B }) {
  const S0 = Math.max(0, B - vh);
  const S1 = Math.max(S0 + vh * 0.5, B);
  const S2 = S1 + vh * 0.3;
  const S3 = S1 + vh;
  const a = clamp01((y - S0) / (S1 - S0));
  const b = clamp01((y - S2) / (S3 - S2));

  // 三层波浪静止时的高度（自底部起，px → viewBox 单位）。
  const restPx = Math.max(220, Math.min(360, vh * 0.32));
  const rest = [1, 0.72, 0.46].map((k) => ((restPx * k) / vh) * 1000);
  const lagIn = [0, 0.1, 0.2];
  const lagOut = [0.2, 0.1, 0];
  const t = a + b;

  const paths = [0, 1, 2].map((i) => {
    const rise = smooth(clamp01((a - lagIn[i]) / (1 - lagIn[i])));
    const leave = smooth(clamp01((b - lagOut[i]) / (1 - lagOut[i])));
    const amplitude = 34 + 10 * i;
    const phase = i * 1.4 + t * 2.2;
    const top0 = 1000 - rest[i];
    const top = waveEdge(top0 + (-160 - top0) * rise, amplitude, phase, 1.1 + i * 0.25);
    const bottom = waveEdge(1160 + (-160 - 1160) * leave, amplitude, phase + 2, 1 + i * 0.3).reverse();
    return "M" + top.concat(bottom).map(([px, py]) => `${px.toFixed(1)} ${py.toFixed(1)}`).join(" L") + " Z";
  });

  const logo = clamp01((a - 0.85) / 0.15) * (1 - clamp01(b / 0.3));
  const shift = Math.max(0, S0 - y);
  const arrow = 1 - clamp01(a / 0.25);

  return {
    paths,
    shift,
    hidden: shift > vh || b >= 1,
    logoOpacity: logo,
    logoScale: 0.94 + 0.06 * logo,
    arrowOpacity: arrow,
    arrowInteractive: arrow > 0.2,
    // 「探索更多」滚到转场结束、产品演示露出的位置。
    target: S1 + vh,
  };
}
