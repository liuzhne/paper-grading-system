<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";

import SiteFooter from "./SiteFooter.vue";
import SiteHeader from "./SiteHeader.vue";
import SiteMark from "./SiteMark.vue";
import { measureSection, overlayState, productState } from "./landing-motion.js";
import { APP_URL, aboutUrl } from "./links.js";

/**
 * 宣传页（`/`），还原 Claude Design 原型「有据智评 宣传页.dc.html」。
 *
 * 与原型的差异（用户决定，2026-10-17）：
 * - 首屏「输入院系邮箱，获取试用名额」表单不上线。原型只改前端状态、提交后显示
 *   「已登记」而什么也没保存；落库另做。
 * - 原型里点了没反应的「申请试用」大按钮指向子页的预约区，「使用条款」去掉。
 */
defineProps({
  /** 首屏右侧的产品卡片（原型的 showProductCard 属性）。 */
  showProductCard: { type: Boolean, default: true },
});

const section = ref(/** @type {HTMLElement|null} */ (null));
const view = reactive({ p: 0, y: 0, vw: 1280, vh: 800, B: 0 });

let frame = 0;

function sample() {
  frame = 0;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const y = window.scrollY;
  let p = view.p;
  let B = view.B || vh;
  if (section.value) ({ p, B } = measureSection(section.value.getBoundingClientRect(), vh, y));
  Object.assign(view, { p, y, vw, vh, B });
}

function schedule() {
  if (!frame) frame = requestAnimationFrame(sample);
}

onMounted(() => {
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  sample();
});

onBeforeUnmount(() => {
  window.removeEventListener("scroll", schedule);
  window.removeEventListener("resize", schedule);
  if (frame) cancelAnimationFrame(frame);
});

const product = computed(() => productState(view));
const overlay = computed(() => overlayState({ y: view.y, vh: view.vh, B: view.B || view.vh }));

function explore() {
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({ top: overlay.value.target, behavior: reduce ? "auto" : "smooth" });
}

const percent = (/** @type {number} */ x) => `${(x * 100).toFixed(1)}%`;
</script>

<template>
  <div class="landing">
    <div class="hero">
      <div class="ring ring-gold" aria-hidden="true" />
      <div class="ring ring-teal" aria-hidden="true" />
      <div class="ring ring-faint" aria-hidden="true" />

      <SiteHeader active="product" />

      <section class="hero-main">
        <div class="hero-copy">
          <h1>每一个分数，<br />都有出处</h1>
          <p class="tagline">作业与学位论文评审系统 | 评分标准入库 · 逐项给出证据 · 人工复核留痕</p>
          <a class="deploy-link" :href="aboutUrl('deploy')">院系批量部署？联系我们</a>
          <div class="cta-row">
            <a class="cta-primary" :href="aboutUrl('contact')" data-test="hero-trial">申请试用</a>
            <span class="signin">已有账户？<a :href="APP_URL" data-test="enter-app">直接进入工作台 →</a></span>
          </div>
        </div>

        <div v-if="showProductCard" class="hero-card" aria-hidden="true">
          <div class="paper">
            <div class="paper-meta">论文正文 · 第 12 页</div>
            <div class="paper-heading">3.2 研究方法</div>
            <div class="paper-quote">本文采用对比实验，在三组公开数据集上评估模型的召回率与精确率……</div>
            <div class="lines">
              <div class="line" />
              <div class="line" style="width: 72%" />
            </div>
          </div>
          <div class="score">
            <div class="score-head">
              <span class="score-name">研究方法与技术方案</span>
              <span class="score-value">21<span class="score-max"> / 25</span></span>
            </div>
            <div class="score-confidence">
              <span class="score-label">置信度</span>
              <div class="meter"><div class="meter-fill" style="width: 86%" /></div>
              <span class="score-pct">86%</span>
            </div>
            <div class="score-foot">
              <span class="chip">证据 · 第 12 页 · 3.2</span>
              <span class="reviewed">已复核</span>
            </div>
          </div>
          <div class="connector" />
        </div>
      </section>

      <div class="wave-spacer" />
    </div>

    <section ref="section" class="product" aria-label="产品演示">
      <div class="product-stage">
        <div
          class="product-grid"
          :style="{ gridTemplateColumns: product.frame.wide ? 'minmax(0, 0.85fr) minmax(0, 1.35fr)' : 'minmax(0, 1fr)' }"
        >
          <div class="captions" :style="{ minHeight: `${product.frame.captionHeight}px` }">
            <div class="rail">
              <div v-for="step in product.rail" :key="step.label" class="rail-step">
                <div class="rail-track"><div class="rail-fill" :style="{ width: percent(step.fill) }" /></div>
                <div class="rail-label" :class="{ current: step.current }">{{ step.label }}</div>
              </div>
            </div>
            <div
              v-for="caption in product.captions"
              :key="caption.index"
              class="caption"
              :style="{ opacity: caption.opacity, transform: `translateY(${caption.shift.toFixed(1)}px)` }"
            >
              <div class="caption-index">{{ caption.index }}</div>
              <h2>{{ caption.title }}</h2>
              <p>{{ caption.body }}</p>
            </div>
          </div>

          <div
            class="window"
            aria-hidden="true"
            :style="{
              transform: `scale(${product.frame.scale.toFixed(4)})`,
              opacity: product.frame.opacity,
              marginBottom: `${product.frame.marginBottom.toFixed(1)}px`,
            }"
          >
            <div class="window-bar">
              <span class="window-mark"><SiteMark :size="13" :stroke-width="2.6" /></span>
              <span class="window-crumb">{{ product.frame.crumb }}</span>
            </div>
            <div class="window-body" :style="{ height: `${product.frame.bodyHeight}px` }">
              <div class="panel" :style="{ opacity: product.criteria.opacity }">
                <div class="panel-head"><span class="panel-title">评分项</span><span class="panel-sub">打分规则.xlsx › 指导教师评分项</span></div>
                <div
                  v-for="row in product.criteria.rows"
                  :key="row.code"
                  class="crit-row"
                  :style="{ opacity: row.opacity, transform: `translateX(${row.shift.toFixed(1)}px)` }"
                >
                  <span class="crit-code">{{ row.code }}</span>
                  <span class="crit-name">{{ row.name }}</span>
                  <span class="crit-max">{{ row.max }}</span>
                  <span class="crit-state" :class="{ ok: row.checked }"><span class="dot" />{{ row.checked ? "已核对" : "待核对" }}</span>
                </div>
              </div>

              <div class="panel panel-evidence" :style="{ opacity: product.evidence.opacity }">
                <div class="doc">
                  <div class="doc-page">
                    <div class="doc-meta">论文正文 · 第 12 页</div>
                    <div class="lines lines-gap">
                      <div class="line light" />
                      <div class="line light" style="width: 80%" />
                    </div>
                    <div class="paper-heading">3.2 研究方法</div>
                    <div class="doc-quote">
                      <div class="doc-mark" :style="{ width: percent(product.evidence.highlight) }" />
                      <span>本文采用对比实验，在三组公开数据集上评估模型的召回率与精确率……</span>
                    </div>
                    <div class="lines lines-top">
                      <div class="line light" />
                      <div class="line light" style="width: 64%" />
                      <div class="line light" style="width: 88%" />
                    </div>
                  </div>
                </div>
                <div class="evidence-side">
                  <svg class="evidence-link" width="60" height="70" viewBox="0 0 60 70">
                    <path
                      d="M0 40 C 30 40, 30 4, 58 4"
                      pathLength="1"
                      fill="none"
                      stroke="#c9953f"
                      stroke-width="1.8"
                      stroke-dasharray="1"
                      :style="{ strokeDashoffset: product.evidence.dash }"
                    />
                  </svg>
                  <div
                    class="evidence-card"
                    :style="{ opacity: product.evidence.cardOpacity, transform: `translateY(${product.evidence.cardShift.toFixed(1)}px)` }"
                  >
                    <div class="score-head">
                      <span class="score-name">研究方法与技术方案</span>
                      <span class="score-value big">21<span class="score-max"> / 25</span></span>
                    </div>
                    <span class="chip dim">证据 · 第 12 页 · 3.2</span>
                  </div>
                </div>
              </div>

              <div class="panel" :style="{ opacity: product.review.opacity }">
                <div class="panel-head"><span class="panel-title">评分表</span><span class="panel-sub">SE-2026-014 · 置信度</span></div>
                <div
                  v-for="row in product.review.rows"
                  :key="row.name"
                  class="conf-row"
                  :style="{ background: `rgba(201,149,63,${(row.tag * 0.1).toFixed(3)})` }"
                >
                  <span class="conf-name">{{ row.name }}</span>
                  <div class="conf-track">
                    <div class="conf-fill" :class="{ warn: row.flagged }" :style="{ width: `${row.value}%` }" />
                  </div>
                  <span class="conf-value" :class="{ warn: row.flagged }">{{ row.value }}%</span>
                  <span class="conf-tag" :style="{ opacity: row.tag }">待复核</span>
                </div>
              </div>

              <div class="panel panel-transcript" :style="{ opacity: product.transcript.opacity }">
                <div class="panel-head tight"><span class="panel-title">成绩单</span><span class="panel-sub">2026 届毕业论文 · 批次 1</span></div>
                <div class="stats">
                  <div>
                    <div class="stat-label">已复核</div>
                    <div class="stat-value">{{ product.transcript.count }}<span class="stat-of"> / 24</span></div>
                  </div>
                  <div>
                    <div class="stat-label">均分</div>
                    <div class="stat-value">{{ product.transcript.average }}</div>
                  </div>
                </div>
                <div class="bars">
                  <div
                    v-for="(bar, i) in product.transcript.bars"
                    :key="i"
                    class="bar"
                    :class="{ highlight: bar.highlight }"
                    :style="{ height: `${bar.height.toFixed(1)}%` }"
                  />
                </div>
                <div class="bar-labels"><span>&lt;60</span><span>60–69</span><span>70–79</span><span>80–89</span><span>90+</span></div>
                <div class="exports" :style="{ opacity: product.transcript.exportOpacity }">
                  <span class="export primary">HTML 评审报告</span>
                  <span class="export">Excel 成绩单</span>
                  <span class="export">结构化 JSON</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="closing">
      <h2>每一个分数，都有出处</h2>
      <p>用你自己的一套评分标准试一个批次。</p>
      <div class="closing-actions">
        <a class="closing-primary" :href="aboutUrl('deploy')">申请试用</a>
        <a class="closing-secondary" :href="APP_URL">登录工作台</a>
      </div>
    </section>

    <SiteFooter />

    <div
      class="overlay"
      :style="{ transform: `translateY(${overlay.shift.toFixed(1)}px)`, display: overlay.hidden ? 'none' : 'block' }"
    >
      <svg class="waves" viewBox="0 0 1440 1000" preserveAspectRatio="none" aria-hidden="true">
        <path :d="overlay.paths[0]" fill="#c9953f" />
        <path :d="overlay.paths[1]" fill="#2f7d5c" />
        <path :d="overlay.paths[2]" fill="#175c50" />
      </svg>
      <button
        type="button"
        class="explore"
        :style="{ opacity: overlay.arrowOpacity, pointerEvents: overlay.arrowInteractive ? 'auto' : 'none' }"
        :tabindex="overlay.arrowInteractive ? 0 : -1"
        @click="explore"
      >
        <span class="explore-label">探索更多</span>
        <svg class="explore-arrow" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M12 4v15" />
          <path d="M5 12l7 7 7-7" />
        </svg>
      </button>
      <div class="overlay-logo" aria-hidden="true" :style="{ opacity: overlay.logoOpacity, transform: `scale(${overlay.logoScale.toFixed(3)})` }">
        <span class="overlay-mark"><SiteMark :size="42" :stroke-width="2" /></span>
        <span class="overlay-name">有据智评</span>
        <span class="overlay-sub">从评分标准到成绩单</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.landing {
  background: #f6f5f2;
}

/* ---------- 首屏 ---------- */

.hero {
  position: relative;
  min-height: 100vh;
  overflow: hidden;
  background: #1c211e;
  color: #fff;
  display: flex;
  flex-direction: column;
}

.ring {
  position: absolute;
  border-radius: 50%;
  pointer-events: none;
}

.ring-gold {
  width: 1100px;
  height: 1100px;
  right: -260px;
  top: -420px;
  border: 1.5px solid rgba(201, 149, 63, 0.32);
}

.ring-teal {
  width: 760px;
  height: 760px;
  right: -40px;
  top: -180px;
  border: 1px solid rgba(111, 161, 150, 0.22);
}

.ring-faint {
  width: 520px;
  height: 520px;
  left: 30%;
  top: 22%;
  border: 1px solid rgba(111, 161, 150, 0.12);
}

.hero-main {
  position: relative;
  z-index: 3;
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 48px;
  padding: clamp(40px, 7vh, 96px) clamp(20px, 8vw, 150px) 0 clamp(20px, 18vw, 300px);
}

.hero-copy {
  flex: 1 1 520px;
  min-width: 0;
  max-width: 780px;
}

h1 {
  margin: 0 0 26px;
  font-size: clamp(40px, 5.6vw, 86px);
  line-height: 1.12;
  font-weight: 800;
  letter-spacing: 0.02em;
  text-wrap: balance;
}

.tagline {
  margin: 0 0 16px;
  font-family: var(--font-mono-cjk);
  font-size: clamp(14px, 1.25vw, 18px);
  color: #cdd3d0;
  letter-spacing: 0.02em;
  text-wrap: pretty;
}

.deploy-link {
  display: inline-block;
  margin: 18px 0 28px;
  font-family: var(--font-mono-cjk);
  font-size: 14px;
  color: #e7eae8;
  border-bottom: 1.5px solid #e7eae8;
  padding-bottom: 2px;
}

.deploy-link:hover {
  color: #fff;
  border-bottom-color: #fff;
}

.cta-row {
  display: flex;
  align-items: center;
  gap: 14px 22px;
  flex-wrap: wrap;
}

.cta-primary {
  display: inline-flex;
  align-items: center;
  height: 60px;
  padding: 0 40px;
  border-radius: 40px;
  background: linear-gradient(90deg, #175c50, #2f7d5c);
  color: #fff;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.12em;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
}

.cta-primary:hover {
  color: #fff;
  filter: brightness(1.12);
}

.signin {
  font-size: 14px;
  color: #a8b0ac;
}

.signin a {
  color: #e7eae8;
  font-weight: 600;
  border-bottom: 1px solid rgba(231, 234, 232, 0.5);
  padding-bottom: 1px;
}

.signin a:hover {
  color: #fff;
}

.hero-card {
  flex: 0 1 400px;
  min-width: 300px;
  position: relative;
  height: 470px;
}

.paper {
  position: absolute;
  left: 0;
  top: 30px;
  width: 300px;
  background: #f6f5f2;
  color: #1a1b19;
  border-radius: 10px;
  padding: 22px 24px;
  transform: rotate(-4deg);
  box-shadow: 0 24px 50px rgba(0, 0, 0, 0.35);
}

.paper-meta {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.1em;
  color: #9b9d95;
  margin-bottom: 12px;
}

.paper-heading {
  font-size: 12.5px;
  line-height: 1.85;
  color: #83867f;
  margin-bottom: 10px;
}

.paper-quote {
  border-left: 3px solid #c9953f;
  background: #fbf1dc;
  padding: 10px 12px;
  border-radius: 0 6px 6px 0;
  font-size: 13px;
  line-height: 1.75;
}

.lines {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 14px;
}

.line {
  height: 7px;
  background: #e6e5df;
  border-radius: 2px;
}

.line.light {
  background: #efeee9;
}

.score {
  position: absolute;
  right: 0;
  top: 262px;
  width: 270px;
  background: #fff;
  color: #1a1b19;
  border-radius: 12px;
  padding: 18px 20px;
  transform: rotate(3deg);
  box-shadow: 0 24px 50px rgba(0, 0, 0, 0.4);
}

.score-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}

.score-name {
  font-size: 13.5px;
  font-weight: 650;
}

.score-value {
  font-family: var(--font-mono);
  font-size: 18px;
}

.score-value.big {
  font-size: 20px;
}

.score-max {
  font-size: 12px;
  color: #9b9d95;
}

.score-confidence {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.score-label {
  font-size: 11px;
  color: #9b9d95;
}

.meter {
  flex: 1;
  height: 4px;
  border-radius: 2px;
  background: #ebeae5;
}

.meter-fill {
  height: 100%;
  border-radius: 2px;
  background: #175c50;
}

.score-pct {
  font-family: var(--font-mono);
  font-size: 11px;
  color: #175c50;
}

.score-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.chip {
  font-size: 11.5px;
  padding: 3px 9px;
  border-radius: 5px;
  border: 1px solid #e0dfd9;
}

.chip.dim {
  display: inline-block;
  font-size: 12px;
  color: #4a4d48;
}

.reviewed {
  font-size: 11.5px;
  color: #175c50;
}

.connector {
  position: absolute;
  left: 292px;
  top: 150px;
  width: 50px;
  height: 108px;
  border-top: 1.5px dashed #c9953f;
  border-right: 1.5px dashed #c9953f;
  border-radius: 0 18px 0 0;
}

/* 波浪静止时压在这里；它本身是空的，只占高度。 */
.wave-spacer {
  position: relative;
  z-index: 1;
  height: clamp(220px, 32vh, 360px);
  margin-top: -40px;
}

/* ---------- 产品演示 ---------- */

.product {
  position: relative;
  height: 580vh;
  background: #f6f5f2;
  color: #1a1b19;
}

.product-stage {
  position: sticky;
  top: 0;
  height: 100vh;
  display: flex;
  align-items: center;
  padding: 0 clamp(20px, 6vw, 110px);
  overflow: hidden;
}

.product-grid {
  width: 100%;
  max-width: 1320px;
  margin: 0 auto;
  display: grid;
  gap: 28px 72px;
  align-items: center;
}

.captions {
  position: relative;
}

.rail {
  display: flex;
  gap: 8px;
  margin-bottom: 30px;
}

.rail-step {
  flex: 1;
  max-width: 64px;
}

.rail-track {
  height: 3px;
  border-radius: 2px;
  background: #e0dfd9;
  overflow: hidden;
}

.rail-fill {
  height: 100%;
  background: #175c50;
}

.rail-label {
  font-size: 11.5px;
  margin-top: 8px;
  color: #9b9d95;
  white-space: nowrap;
}

.rail-label.current {
  color: #1a1b19;
}

.caption {
  position: absolute;
  left: 0;
  right: 0;
  top: 64px;
}

.caption-index {
  font-family: var(--font-mono);
  font-size: 12.5px;
  letter-spacing: 0.14em;
  color: #c9953f;
  margin-bottom: 16px;
}

.caption h2 {
  margin: 0 0 16px;
  font-size: clamp(28px, 3.2vw, 46px);
  line-height: 1.22;
  font-weight: 800;
  text-wrap: balance;
}

.caption p {
  margin: 0;
  font-size: clamp(15px, 1.2vw, 17px);
  line-height: 1.85;
  color: #4a4d48;
  max-width: 440px;
  text-wrap: pretty;
}

.window {
  transform-origin: 50% 0;
  background: #fff;
  border: 1px solid #e4e3de;
  border-radius: 16px;
  box-shadow: 0 40px 90px rgba(28, 33, 30, 0.16);
  overflow: hidden;
}

.window-bar {
  height: 46px;
  background: #1c211e;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
}

.window-mark {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: #175c50;
  color: #fff;
  display: grid;
  place-items: center;
}

.window-crumb {
  font-size: 12.5px;
  color: #cdd3d0;
}

.window-body {
  position: relative;
}

.panel {
  position: absolute;
  inset: 0;
  padding: 22px 24px;
}

.panel-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 14px;
}

.panel-head.tight {
  margin-bottom: 4px;
}

.panel-title {
  font-size: 15px;
  font-weight: 700;
}

.panel-sub {
  font-size: 12.5px;
  color: #83867f;
}

.crit-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 11px 4px;
  border-bottom: 1px solid #f2f1ec;
}

.crit-code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: #9b9d95;
  width: 32px;
}

.crit-name {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 550;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.crit-max {
  font-family: var(--font-mono);
  font-size: 14px;
}

.crit-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: 72px;
  font-size: 12px;
  color: #b58432;
}

.crit-state.ok {
  color: #175c50;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.panel-evidence {
  display: flex;
  gap: 18px;
  align-items: flex-start;
}

.doc {
  flex: 1.2;
  min-width: 0;
  background: #edece7;
  border-radius: 10px;
  padding: 14px;
}

.doc-page {
  background: #fff;
  border: 1px solid #e1e0da;
  border-radius: 4px;
  padding: 18px 20px;
}

.doc .paper-heading {
  line-height: normal;
  margin-bottom: 6px;
}

.doc-meta {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: #b3b5ac;
  margin-bottom: 12px;
}

.lines-gap {
  margin: 0 0 14px;
}

.lines-top {
  margin-top: 14px;
}

.doc-quote {
  position: relative;
  border-radius: 0 6px 6px 0;
  padding: 9px 11px;
  font-size: 13px;
  line-height: 1.75;
}

.doc-quote span {
  position: relative;
}

.doc-mark {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  background: #fbf1dc;
  border-left: 3px solid #c9953f;
  border-radius: 0 6px 6px 0;
}

.evidence-side {
  flex: 1;
  min-width: 0;
  padding-top: 70px;
  position: relative;
}

.evidence-link {
  position: absolute;
  left: -26px;
  top: 26px;
  overflow: visible;
}

.evidence-card {
  border: 1px solid #e4e3de;
  border-radius: 12px;
  padding: 16px 18px;
  box-shadow: 0 12px 30px rgba(0, 0, 0, 0.08);
}

.conf-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 8px;
  border-bottom: 1px solid #f2f1ec;
  border-radius: 6px;
}

.conf-name {
  flex: 0 1 150px;
  min-width: 0;
  font-size: 13.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conf-track {
  flex: 1;
  height: 5px;
  border-radius: 3px;
  background: #ebeae5;
  overflow: hidden;
}

.conf-fill {
  height: 100%;
  border-radius: 3px;
  background: #175c50;
}

.conf-value {
  font-family: var(--font-mono);
  font-size: 12px;
  width: 34px;
  text-align: right;
  color: #175c50;
}

.conf-fill.warn {
  background: #b58432;
}

.conf-value.warn {
  color: #b58432;
}

.conf-tag {
  width: 56px;
  font-size: 11.5px;
  padding: 2px 0;
  text-align: center;
  border-radius: 4px;
  background: #f6eedd;
  color: #8a5a12;
}

.panel-transcript {
  display: flex;
  flex-direction: column;
}

.stats {
  display: flex;
  gap: 36px;
  margin: 12px 0 18px;
}

.stat-label {
  font-size: 12px;
  color: #83867f;
}

.stat-value {
  font-family: var(--font-mono);
  font-size: 34px;
  font-weight: 500;
}

.stat-of {
  font-size: 15px;
  color: #9b9d95;
}

.bars {
  flex: 1;
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
  align-items: end;
  border-bottom: 1px solid #eeede8;
  min-height: 90px;
}

.bar {
  background: #c8cdc7;
  border-radius: 3px 3px 0 0;
}

.bar.highlight {
  background: #175c50;
}

.bar-labels {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: #9b9d95;
  text-align: center;
  margin: 6px 0 16px;
}

.exports {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.export {
  font-size: 12.5px;
  padding: 6px 12px;
  border-radius: 20px;
  border: 1px solid #d9d8d2;
  color: #4a4d48;
}

.export.primary {
  border-color: #175c50;
  background: #175c50;
  color: #fff;
}

/* ---------- 收尾与页脚 ---------- */

.closing {
  background: #f6f5f2;
  padding: 40px 20px 96px;
  text-align: center;
}

.closing h2 {
  margin: 0 0 14px;
  font-size: clamp(28px, 3.2vw, 44px);
  font-weight: 800;
  color: #1a1b19;
}

.closing p {
  margin: 0 0 28px;
  font-size: 16px;
  color: #4a4d48;
}

.closing-actions {
  display: flex;
  gap: 12px;
  justify-content: center;
  flex-wrap: wrap;
}

.closing-primary {
  display: inline-flex;
  align-items: center;
  height: 50px;
  padding: 0 30px;
  border-radius: 26px;
  background: #175c50;
  color: #fff;
  font-size: 16px;
  font-weight: 700;
}

.closing-primary:hover {
  color: #fff;
  filter: brightness(1.12);
}

.closing-secondary {
  display: inline-flex;
  align-items: center;
  height: 50px;
  padding: 0 24px;
  border-radius: 26px;
  border: 1px solid #cfcec7;
  color: #1a1b19;
  font-size: 16px;
}

.closing-secondary:hover {
  border-color: #175c50;
  color: #175c50;
}

/* ---------- 转场波浪 ---------- */

.overlay {
  position: fixed;
  left: 0;
  right: 0;
  top: 0;
  height: 100vh;
  z-index: 40;
  pointer-events: none;
}

.waves {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}

.explore {
  position: absolute;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  border: 0;
  background: transparent;
  color: #e7eae8;
  cursor: pointer;
  padding: 8px 14px;
}

.explore:hover {
  color: #fff;
}

.explore-label {
  font-size: 13px;
  letter-spacing: 0.24em;
  margin-right: -0.24em;
  color: #fff;
}

.explore-arrow {
  animation: site-bounce 1.6s ease-in-out infinite;
}

@keyframes site-bounce {
  0%,
  100% {
    transform: translateY(0);
  }

  50% {
    transform: translateY(9px);
  }
}

.overlay-logo {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  pointer-events: none;
  color: #fff;
}

.overlay-mark {
  width: 76px;
  height: 76px;
  border-radius: 20px;
  border: 2px solid rgba(255, 255, 255, 0.85);
  display: grid;
  place-items: center;
}

.overlay-name {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: 0.3em;
  margin-right: -0.3em;
}

.overlay-sub {
  font-family: var(--font-mono-cjk);
  font-size: 13px;
  letter-spacing: 0.18em;
  color: #cfe0da;
}

/* 原型的首屏左侧留白按视口宽度的 18% 算，手机上会白白吃掉五分之一的宽度。 */
@media (max-width: 720px) {
  .hero-main {
    padding: 32px 20px 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .explore-arrow {
    animation: none;
  }
}
</style>
