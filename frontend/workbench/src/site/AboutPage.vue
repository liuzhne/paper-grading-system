<script setup>
import { nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";

import SiteFooter from "./SiteFooter.vue";
import SiteHeader from "./SiteHeader.vue";
import {
  AI_LIMITS,
  DEPLOY_STEPS,
  DOC_NAV,
  DOC_STEPS,
  INTEGRATIONS,
  KEYS,
  LIFECYCLE,
  PAGE_TITLES,
  PLANS,
  PRINCIPLES,
  STATUSES,
  TOC,
  resolveHash,
} from "./about-content.js";
import { CONTACT_EMAIL, SECURITY_EMAIL, aboutUrl, mailto } from "./links.js";
import { siteLeadsEnabled, siteLeadsRetentionDays } from "./site-config.js";
import { failureMessage, submitLead } from "./site-leads.js";

/**
 * 官网子页（`/about`），还原 Claude Design 原型「有据智评 官网子页.dc.html」。
 *
 * 与原型的差异（用户决定，2026-10-17）：
 * - 「预约部署评估」表单只在部署打开申请开关时出现（2026-10-18 落库上线，加了可选的
 *   备注 / 课程范围）；没开时（Vercel）保留「发邮件预约」。
 * - 帮助文档去掉不工作的搜索框、「下一篇」与「有帮助吗」；空链接改为锚点、工作台
 *   页面或「即将上线」。
 */
const page = ref(/** @type {"deploy"|"security"|"docs"} */ ("deploy"));
const anchor = ref(/** @type {string|null} */ (null));

async function sync() {
  const target = resolveHash(window.location.hash);
  page.value = target.page;
  anchor.value = target.anchor;
  document.title = `${PAGE_TITLES[target.page]} · 有据智评`;
  await nextTick();
  // 锚点在切换分页之后才渲染出来，浏览器自己的锚点跳转那时已经错过了。
  if (target.anchor) document.getElementById(target.anchor)?.scrollIntoView();
  else if (target.known) window.scrollTo(0, 0);
}

onMounted(() => {
  window.addEventListener("hashchange", sync);
  sync();
});

onBeforeUnmount(() => window.removeEventListener("hashchange", sync));

// ---------- 预约部署评估 ----------

const leadsEnabled = siteLeadsEnabled();
const retentionDays = siteLeadsRetentionDays();
const NOTE_MAX = 500;
const deploy = reactive({
  organization: "",
  email: "",
  note: "",
  website: "",
  sending: false,
  sent: false,
  error: "",
  fields: /** @type {Record<string, string>} */ ({}),
});

async function submitDeploy() {
  if (deploy.sending) return;
  deploy.error = "";
  const fields = /** @type {Record<string, string>} */ ({});
  if (!deploy.organization.trim()) fields.organization_name = "请填写学校与院系";
  if (!deploy.email.trim()) fields.email = "请填写联系邮箱";
  deploy.fields = fields;
  if (Object.keys(fields).length) return;
  deploy.sending = true;
  const result = await submitLead({
    kind: "deploy_assessment",
    email: deploy.email.trim(),
    organization_name: deploy.organization.trim(),
    note: deploy.note.trim(),
    website: deploy.website,
  });
  deploy.sending = false;
  if (result.ok) {
    deploy.sent = true;
    return;
  }
  if (result.reason === "invalid") {
    deploy.fields = result.fields;
    return;
  }
  deploy.error = failureMessage(result.reason);
}
</script>

<template>
  <div class="about">
    <SiteHeader solid :active="page" :trial-href="aboutUrl('contact')" />

    <main v-if="page === 'deploy'" data-test="page-deploy">
      <section class="intro">
        <div class="intro-ring gold" aria-hidden="true" />
        <div class="intro-inner">
          <div class="eyebrow">院系部署</div>
          <h1>部署在学校自己的服务器上，材料不出校</h1>
          <p class="lead">三种部署方式，按数据存放要求和网络条件选择。院系正式使用推荐校内私有部署：单租户、对接统一身份认证、自选模型服务。</p>
          <div class="intro-actions">
            <a class="btn-gold" href="#contact">预约部署评估</a>
            <a class="btn-ghost" href="#docs">查看部署文档</a>
          </div>
        </div>
      </section>

      <section class="block">
        <h2>选择部署方式</h2>
        <p class="block-sub">三种方式功能一致，区别在数据存放位置、AI 模型来源和账户体系。</p>
        <div class="plans">
          <div v-for="plan in PLANS" :key="plan.name" class="plan" :class="{ featured: plan.recommended }">
            <div class="plan-head">
              <span class="plan-name">{{ plan.name }}</span>
              <span v-if="plan.recommended" class="plan-badge">推荐</span>
            </div>
            <div class="plan-fit">{{ plan.fit }}</div>
            <div class="plan-rows">
              <div v-for="row in plan.rows" :key="row.k" class="plan-row">
                <div class="plan-key">{{ row.k }}</div>
                <div class="plan-value">{{ row.v }}</div>
              </div>
            </div>
            <a class="plan-cta" href="#contact">{{ plan.cta }}</a>
          </div>
        </div>
        <div class="footnote">离线部署时 Google Sheets 写入不可用，其余导出方式（HTML 报告、Excel 成绩单、结构化 JSON）不受影响。</div>
      </section>

      <section class="block">
        <h2 class="spaced">私有部署流程</h2>
        <div class="steps">
          <div v-for="step in DEPLOY_STEPS" :key="step.n" class="step">
            <div class="step-n">{{ step.n }}</div>
            <div class="step-t">{{ step.t }}</div>
            <div class="step-d">{{ step.d }}</div>
            <div class="step-who">{{ step.who }}</div>
          </div>
        </div>
      </section>

      <section class="block">
        <h2>需要对接的系统</h2>
        <p class="block-sub">部署评估时逐项确认，没有的可以先跳过。</p>
        <div class="table-card">
          <div v-for="item in INTEGRATIONS" :key="item.t" class="integration">
            <div class="integration-t">{{ item.t }}</div>
            <div class="integration-d">{{ item.d }}</div>
            <div class="integration-tag"><span class="tag" :class="{ required: item.required }">{{ item.tag }}</span></div>
          </div>
        </div>
      </section>

      <section id="contact" class="block last">
        <div class="contact">
          <div class="contact-copy">
            <h2>预约部署评估</h2>
            <p v-if="leadsEnabled">留下院系和联系方式，我们会确认课程范围、数据存放要求和现有系统，再给出部署方案。</p>
            <p v-else>发邮件告诉我们学校、院系和联系方式，我们会确认课程范围、数据存放要求和现有系统，再给出部署方案。</p>
          </div>
          <template v-if="leadsEnabled">
            <form v-if="!deploy.sent" class="contact-form" novalidate data-test="deploy-form" @submit.prevent="submitDeploy">
              <label class="sr-only" for="deploy-org">学校与院系</label>
              <input
                id="deploy-org"
                v-model="deploy.organization"
                type="text"
                maxlength="120"
                autocomplete="organization"
                placeholder="学校与院系"
                :aria-invalid="deploy.fields.organization_name ? 'true' : undefined"
                :aria-describedby="deploy.fields.organization_name ? 'deploy-org-error' : undefined"
              />
              <p v-if="deploy.fields.organization_name" id="deploy-org-error" class="field-error" role="alert">{{ deploy.fields.organization_name }}</p>
              <label class="sr-only" for="deploy-email">联系邮箱</label>
              <input
                id="deploy-email"
                v-model="deploy.email"
                type="email"
                maxlength="254"
                autocomplete="email"
                placeholder="联系邮箱"
                :aria-invalid="deploy.fields.email ? 'true' : undefined"
                :aria-describedby="deploy.fields.email ? 'deploy-email-error' : undefined"
              />
              <p v-if="deploy.fields.email" id="deploy-email-error" class="field-error" role="alert">{{ deploy.fields.email }}</p>
              <label class="sr-only" for="deploy-note">备注 / 课程范围（选填）</label>
              <textarea
                id="deploy-note"
                v-model="deploy.note"
                rows="3"
                :maxlength="NOTE_MAX"
                placeholder="备注 / 课程范围（选填）"
                :aria-invalid="deploy.fields.note ? 'true' : undefined"
              />
              <p v-if="deploy.fields.note" class="field-error" role="alert">{{ deploy.fields.note }}</p>
              <input v-model="deploy.website" class="honeypot" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" />
              <button type="submit" :disabled="deploy.sending">{{ deploy.sending ? "提交中…" : "提交" }}</button>
              <p v-if="deploy.error" class="field-error" role="alert">{{ deploy.error }}<a :href="mailto(CONTACT_EMAIL, '有据智评 · 预约部署评估')">{{ CONTACT_EMAIL }}</a></p>
              <p class="contact-consent">提交即表示同意我们用这个邮箱联系你。申请记录保存 {{ retentionDays }} 天，到期自动删除。<a :href="aboutUrl('leads')">怎么处理</a></p>
            </form>
            <div v-else class="contact-done" role="status" data-test="deploy-done">已收到，我们会通过邮件联系你。</div>
          </template>
          <div v-else class="contact-action">
            <a class="contact-mail" :href="mailto(CONTACT_EMAIL, '有据智评 · 预约部署评估')">发邮件预约</a>
            <div class="contact-addr">{{ CONTACT_EMAIL }}</div>
          </div>
        </div>
      </section>
    </main>

    <main v-else-if="page === 'security'" data-test="page-security">
      <section class="intro">
        <div class="intro-ring teal" aria-hidden="true" />
        <div class="intro-inner">
          <div class="eyebrow">数据安全</div>
          <h1>学生材料只用于评分</h1>
          <p class="lead flush">这一页说明材料存在哪里、谁能看到、AI 能做什么不能做什么，以及每一次改分如何留痕。</p>
        </div>
      </section>

      <section class="block">
        <div class="steps cards">
          <div v-for="item in PRINCIPLES" :key="item.n" class="card">
            <div class="step-n">{{ item.n }}</div>
            <div class="card-t">{{ item.t }}</div>
            <div class="card-d">{{ item.d }}</div>
          </div>
        </div>
      </section>

      <section class="block">
        <h2>一份材料在系统里经过哪里</h2>
        <p class="block-sub">以校内私有部署为例。原始材料的保留期限由院系管理员设定。</p>
        <div class="table-card scroll">
          <table class="lifecycle">
            <thead>
              <tr>
                <th class="first">环节</th>
                <th>产生的数据</th>
                <th>存放位置</th>
                <th class="last-col">可以访问的人</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in LIFECYCLE" :key="row.n">
                <td class="first"><span class="stage"><span class="stage-n">{{ row.n }}</span>{{ row.t }}</span></td>
                <td>{{ row.data }}</td>
                <td>{{ row.where }}</td>
                <td class="last-col">{{ row.who }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="block split">
        <div class="panel-card">
          <h3>AI 的边界</h3>
          <div class="limits">
            <div v-for="item in AI_LIMITS" :key="item.t" class="limit">
              <span class="limit-mark" :class="{ no: !item.allowed }">{{ item.allowed ? "✓" : "×" }}</span>
              <span>{{ item.t }}</span>
            </div>
          </div>
        </div>
        <div class="panel-card">
          <h3 class="tight">匿名评审</h3>
          <p class="panel-p">批次开启匿名评审后，评审教师只看到送审编号，看不到学生姓名。</p>
          <div class="anon">
            <span class="anon-name">林越 · 2022xxxx14</span>
            <span class="anon-arrow">→</span>
            <span class="anon-id">SE-2026-014</span>
          </div>
          <p class="panel-note">编号与姓名的对应关系只有教务管理员可见，导出成绩单时再还原。</p>
        </div>
      </section>

      <section id="leads" class="block">
        <div class="panel-card">
          <h3 class="tight">官网申请信息</h3>
          <p class="panel-p flush">在官网提交的试用与部署评估申请，只保存邮箱、学校与院系和你写的备注，用于联系你；不保存 IP 地址。记录保存 {{ retentionDays }} 天后自动删除，也可以发邮件到 <a :href="mailto(CONTACT_EMAIL, '有据智评 · 删除申请记录')">{{ CONTACT_EMAIL }}</a> 要求提前删除。</p>
        </div>
      </section>

      <section class="block last">
        <div class="report">
          <div class="report-copy">
            <div class="report-t">发现安全问题</div>
            <div class="report-d">请发邮件到安全团队，我们会在确认后回复处理进展。请勿在公开渠道披露细节。</div>
          </div>
          <a class="report-mail" :href="mailto(SECURITY_EMAIL, '有据智评 · 安全问题报告')">{{ SECURITY_EMAIL }}</a>
        </div>
      </section>
    </main>

    <main v-else class="docs" data-test="page-docs">
      <aside class="docs-nav" aria-label="帮助文档目录">
        <div v-for="group in DOC_NAV" :key="group.t" class="docs-group">
          <div class="docs-group-t">{{ group.t }}</div>
          <template v-for="item in group.items" :key="item.t">
            <a
              v-if="item.href"
              :href="item.href"
              class="docs-item"
              :class="{ current: item.current }"
              :aria-current="item.current ? 'page' : undefined"
            >{{ item.t }}</a>
            <span v-else class="docs-item soon" title="即将上线">{{ item.t }}</span>
          </template>
        </div>
      </aside>

      <article class="docs-article">
        <div class="crumb">帮助文档 / 入门</div>
        <h1 class="docs-h1">快速开始</h1>
        <p class="docs-lead">从建立评分标准到导出成绩单，一个批次要走五步。前两步每套评分标准只需要做一次，之后的批次可以直接复用。</p>

        <div id="docs-before" class="notice">
          <span class="notice-mark" aria-hidden="true">!</span>
          <div>开始前请准备好两份文件：评分表（.xlsx，列出评分项和分值）和评分标准文档（.docx，教研组审定的原文）。只有文档也可以，但评分项需要手动核对。</div>
        </div>

        <div id="docs-steps">
          <section v-for="step in DOC_STEPS" :key="step.n" class="doc-step">
            <span class="doc-step-n">{{ step.n }}</span>
            <div class="doc-step-body">
              <h2>{{ step.t }}</h2>
              <p>{{ step.d }}</p>
              <div class="doc-step-meta">
                位置：<span class="doc-step-path">{{ step.path }}</span> · <a :href="step.link.href">{{ step.link.label }} →</a>
              </div>
            </div>
          </section>
        </div>

        <h2 id="keys" class="docs-h2">快捷键</h2>
        <p class="docs-p">在评分规则的「待归类原文」中可用：</p>
        <div class="table-card keys">
          <div v-for="key in KEYS" :key="key.k" class="key-row">
            <span class="key-cap-wrap"><kbd class="key-cap">{{ key.k }}</kbd></span>
            <span>{{ key.d }}</span>
          </div>
        </div>

        <h2 id="status" class="docs-h2 flush">批次状态</h2>
        <div class="table-card scroll statuses">
          <table>
            <thead>
              <tr>
                <th class="status-col">界面显示</th>
                <th>含义</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="status in STATUSES" :key="status.label">
                <td><span class="status" :class="status.tone">{{ status.label }}</span></td>
                <td class="status-d">{{ status.d }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <nav class="toc" aria-label="本页内容">
        <div class="docs-group-t">本页内容</div>
        <div class="toc-list">
          <a
            v-for="(item, i) in TOC"
            :key="item.anchor"
            :href="`#${item.anchor}`"
            class="toc-item"
            :class="{ current: anchor ? anchor === item.anchor : i === 0 }"
          >{{ item.t }}</a>
        </div>
      </nav>
    </main>

    <SiteFooter contained />
  </div>
</template>

<style scoped>
.about {
  min-height: 100vh;
  background: #f6f5f2;
  color: #1a1b19;
}

a {
  color: #175c50;
}

a:hover {
  color: #0e433a;
}

/* ---------- 分页首屏 ---------- */

.intro {
  background: #1c211e;
  color: #fff;
  position: relative;
  overflow: hidden;
}

.intro-ring {
  position: absolute;
  width: 760px;
  height: 760px;
  right: -220px;
  top: -360px;
  border-radius: 50%;
}

.intro-ring.gold {
  border: 1.5px solid rgba(201, 149, 63, 0.3);
}

.intro-ring.teal {
  border: 1.5px solid rgba(111, 161, 150, 0.28);
}

.intro-inner {
  position: relative;
  max-width: 1280px;
  margin: 0 auto;
  padding: clamp(48px, 8vh, 88px) clamp(20px, 3vw, 40px) clamp(48px, 7vh, 76px);
}

.eyebrow {
  font-family: var(--font-mono-cjk);
  font-size: 12px;
  letter-spacing: 0.2em;
  color: #6fa196;
  margin-bottom: 18px;
}

h1 {
  margin: 0 0 18px;
  font-size: clamp(34px, 4.4vw, 60px);
  line-height: 1.18;
  font-weight: 800;
  max-width: 820px;
  text-wrap: balance;
}

.lead {
  margin: 0 0 30px;
  font-size: 16px;
  line-height: 1.85;
  color: #cdd3d0;
  max-width: 640px;
  text-wrap: pretty;
}

.lead.flush {
  margin-bottom: 0;
}

.intro-actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.btn-gold {
  display: inline-flex;
  align-items: center;
  height: 48px;
  padding: 0 26px;
  border-radius: 26px;
  background: #c9953f;
  color: #1c211e;
  font-size: 15px;
  font-weight: 700;
}

.btn-gold:hover {
  background: #d8a650;
  color: #1c211e;
}

.btn-ghost {
  display: inline-flex;
  align-items: center;
  height: 48px;
  padding: 0 22px;
  border-radius: 26px;
  border: 1px solid rgba(255, 255, 255, 0.3);
  color: #fff;
  font-size: 15px;
}

.btn-ghost:hover {
  border-color: #fff;
  color: #fff;
}

/* ---------- 内容块 ---------- */

.block {
  max-width: 1280px;
  margin: 0 auto;
  padding: 64px clamp(20px, 3vw, 40px) 0;
}

.block.last {
  padding-bottom: 80px;
}

.block h2 {
  margin: 0 0 8px;
  font-size: 26px;
  font-weight: 700;
}

.block h2.spaced {
  margin-bottom: 26px;
}

.block-sub {
  margin: 0 0 26px;
  font-size: 14.5px;
  color: #83867f;
}

.plans {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  margin-bottom: 18px;
}

.plan {
  background: #fff;
  color: #1a1b19;
  border: 1px solid #e4e3de;
  border-radius: 14px;
  padding: 24px;
  display: flex;
  flex-direction: column;
}

.plan.featured {
  background: #1c211e;
  color: #fff;
  border-color: #1c211e;
}

.plan-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.plan-name {
  font-size: 18px;
  font-weight: 700;
}

.plan-badge {
  font-size: 11.5px;
  padding: 2px 9px;
  border-radius: 20px;
  background: #c9953f;
  color: #1c211e;
  font-weight: 600;
}

.plan-fit {
  font-size: 13.5px;
  color: #83867f;
  margin-bottom: 18px;
}

.featured .plan-fit,
.featured .plan-key {
  color: #a8b0ac;
}

.plan-rows {
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: 1;
}

.plan-row {
  border-top: 1px solid #eeede8;
  padding-top: 11px;
}

.featured .plan-row {
  border-top-color: rgba(255, 255, 255, 0.1);
}

.plan-key {
  font-size: 12px;
  color: #83867f;
  margin-bottom: 3px;
}

.plan-value {
  font-size: 14px;
  line-height: 1.6;
}

.plan-cta {
  margin-top: 22px;
  display: inline-flex;
  justify-content: center;
  align-items: center;
  height: 42px;
  border-radius: 22px;
  font-size: 14px;
  font-weight: 600;
  background: #fff;
  color: #175c50;
  border: 1px solid #d9d8d2;
}

.plan-cta:hover {
  border-color: #175c50;
}

.featured .plan-cta {
  background: #c9953f;
  color: #1c211e;
  border-color: #c9953f;
}

.featured .plan-cta:hover {
  background: #d8a650;
  color: #1c211e;
}

.footnote {
  font-size: 12.5px;
  color: #9b9d95;
}

.steps {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1px;
  background: #eeede8;
  border: 1px solid #e4e3de;
  border-radius: 14px;
  overflow: hidden;
}

.step {
  padding: 24px 22px;
  background: #fff;
}

.step-n {
  font-family: var(--font-mono);
  font-size: 13px;
  color: #c9953f;
  margin-bottom: 14px;
}

.step-t {
  font-size: 16px;
  font-weight: 650;
  margin-bottom: 8px;
}

.step-d {
  font-size: 13.5px;
  line-height: 1.8;
  color: #4a4d48;
  text-wrap: pretty;
}

.step-who {
  font-size: 12.5px;
  color: #83867f;
  margin-top: 12px;
}

.steps.cards {
  gap: 16px;
  background: transparent;
  border: 0;
  border-radius: 0;
  overflow: visible;
}

.card {
  background: #fff;
  border: 1px solid #e4e3de;
  border-radius: 14px;
  padding: 24px;
}

.card .step-n {
  font-size: 12.5px;
}

.card-t {
  font-size: 17px;
  font-weight: 700;
  margin-bottom: 8px;
}

.card-d {
  font-size: 14px;
  line-height: 1.8;
  color: #4a4d48;
  text-wrap: pretty;
}

.table-card {
  background: #fff;
  border: 1px solid #e4e3de;
  border-radius: 14px;
  overflow: hidden;
}

.table-card.scroll {
  overflow: auto;
}

.integration {
  display: flex;
  gap: 12px 28px;
  flex-wrap: wrap;
  padding: 18px 24px;
  border-bottom: 1px solid #eeede8;
}

.integration:last-child {
  border-bottom: 0;
}

.integration-t {
  flex: 0 1 220px;
  font-size: 15px;
  font-weight: 650;
}

.integration-d {
  flex: 1 1 360px;
  font-size: 14px;
  line-height: 1.75;
  color: #4a4d48;
}

.integration-tag {
  flex: 0 0 auto;
}

.tag {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 20px;
  background: #eeede8;
  color: #6f716a;
}

.tag.required {
  background: #e9efec;
  color: #175c50;
}

.contact {
  background: #175c50;
  color: #fff;
  border-radius: 18px;
  padding: clamp(28px, 4vw, 48px);
  display: flex;
  gap: 28px 48px;
  flex-wrap: wrap;
  align-items: center;
}

.contact-copy {
  flex: 1 1 380px;
}

.contact h2 {
  margin: 0 0 10px;
  font-size: 28px;
}

.contact p {
  margin: 0;
  font-size: 15px;
  line-height: 1.8;
  color: #d6e5e0;
}

.contact-action {
  flex: 1 1 380px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.contact-mail {
  height: 46px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  background: #c9953f;
  color: #1c211e;
  font-size: 15px;
  font-weight: 700;
}

.contact-mail:hover {
  background: #d8a650;
  color: #1c211e;
}

.contact-form {
  flex: 1 1 380px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.contact-form input,
.contact-form textarea {
  padding: 0 16px;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 0.25);
  background: rgba(0, 0, 0, 0.15);
  color: #fff;
  font-size: 15px;
}

.contact-form input {
  height: 46px;
}

.contact-form textarea {
  padding: 12px 16px;
  resize: vertical;
  line-height: 1.6;
}

.contact-form input::placeholder,
.contact-form textarea::placeholder {
  color: #a9c4bc;
}

.contact-form input:focus,
.contact-form textarea:focus {
  outline: none;
  border-color: #c9953f;
}

.contact-form input[aria-invalid="true"] {
  border-color: #f0c27a;
}

.contact-form button {
  height: 46px;
  border: 0;
  border-radius: 10px;
  background: #c9953f;
  color: #1c211e;
  font-size: 15px;
  font-weight: 700;
  cursor: pointer;
}

.contact-form button:hover {
  background: #d8a650;
}

.contact-form button:disabled {
  cursor: progress;
  opacity: 0.75;
}

.field-error {
  margin: -2px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: #f6dfb4;
}

.field-error a,
.contact-consent a {
  margin-left: 4px;
  color: #fff;
  border-bottom: 1px solid rgba(255, 255, 255, 0.5);
}

.contact-consent {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: #b9d2ca;
}

.contact-done {
  flex: 1 1 380px;
  font-size: 15px;
  line-height: 1.8;
  background: rgba(0, 0, 0, 0.15);
  border-radius: 12px;
  padding: 20px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}

.honeypot {
  position: absolute;
  left: -10000px;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.panel-p.flush {
  margin-bottom: 0;
}

#leads {
  scroll-margin-top: 16px;
}

.contact-addr {
  font-family: var(--font-mono);
  font-size: 14px;
  color: #d6e5e0;
  text-align: center;
  word-break: break-all;
}

/* ---------- 数据安全 ---------- */

.lifecycle {
  width: 100%;
  min-width: 820px;
  border-collapse: collapse;
  font-size: 14px;
}

.lifecycle thead tr,
.statuses thead tr {
  background: #faf9f6;
  color: #83867f;
  font-size: 12.5px;
  text-align: left;
}

.lifecycle th {
  padding: 12px 16px;
  font-weight: 500;
  border-bottom: 1px solid #eeede8;
}

.lifecycle th.first {
  width: 120px;
}

.lifecycle .first,
.lifecycle .last-col {
  padding-left: 24px;
  padding-right: 24px;
}

.lifecycle tbody tr {
  border-bottom: 1px solid #f2f1ec;
}

.lifecycle td {
  padding: 15px 16px;
  vertical-align: top;
  color: #4a4d48;
  line-height: 1.7;
}

.stage {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  font-weight: 650;
  color: #1a1b19;
}

.stage-n {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: #c9953f;
}

.split {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr));
  gap: 16px;
}

.panel-card {
  background: #fff;
  border: 1px solid #e4e3de;
  border-radius: 14px;
  padding: 26px;
}

.panel-card h3 {
  margin: 0 0 16px;
  font-size: 19px;
  font-weight: 700;
}

.panel-card h3.tight {
  margin-bottom: 6px;
}

.limits {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.limit {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  font-size: 14px;
  line-height: 1.75;
}

.limit-mark {
  flex: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  margin-top: 2px;
  display: grid;
  place-items: center;
  background: #e9efec;
  color: #175c50;
  font-size: 12px;
  font-weight: 700;
}

.limit-mark.no {
  background: #f8eae7;
  color: #a3372b;
}

.panel-p {
  margin: 0 0 16px;
  font-size: 14px;
  line-height: 1.8;
  color: #4a4d48;
}

.anon {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
  background: #faf9f6;
  border: 1px solid #eeede8;
  border-radius: 10px;
  padding: 14px 16px;
  font-size: 13.5px;
}

.anon-name {
  text-decoration: line-through;
  color: #9b9d95;
}

.anon-arrow {
  color: #9b9d95;
}

.anon-id {
  font-family: var(--font-mono);
  color: #175c50;
  font-weight: 600;
}

.panel-note {
  margin: 14px 0 0;
  font-size: 13px;
  line-height: 1.75;
  color: #83867f;
}

.report {
  border: 1px solid #e4e3de;
  background: #fff;
  border-radius: 14px;
  padding: 24px 28px;
  display: flex;
  gap: 14px 32px;
  flex-wrap: wrap;
  align-items: center;
}

.report-copy {
  flex: 1 1 380px;
}

.report-t {
  font-size: 17px;
  font-weight: 700;
  margin-bottom: 6px;
}

.report-d {
  font-size: 14px;
  line-height: 1.75;
  color: #4a4d48;
}

.report-mail {
  font-family: var(--font-mono);
  font-size: 15px;
  font-weight: 600;
  word-break: break-all;
}

/* ---------- 帮助文档 ---------- */

.docs {
  max-width: 1280px;
  margin: 0 auto;
  padding: 0 clamp(20px, 3vw, 40px);
  display: flex;
  gap: 0 40px;
  flex-wrap: wrap;
  align-items: flex-start;
}

.docs-nav {
  flex: 0 1 240px;
  min-width: 210px;
  padding: 28px 0 40px;
  top: 16px;
}

.docs-group {
  margin-bottom: 18px;
}

.docs-group-t {
  font-size: 12px;
  font-weight: 650;
  color: #83867f;
  letter-spacing: 0.08em;
  margin-bottom: 6px;
}

.docs-item {
  display: block;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 13.5px;
  color: #4a4d48;
}

a.docs-item:hover {
  color: #4a4d48;
  background: #eeede8;
}

.docs-item.current,
a.docs-item.current:hover {
  color: #175c50;
  background: #e9efec;
  font-weight: 600;
}

.docs-item.soon {
  color: #b3b5ac;
  cursor: default;
}

.docs-article {
  flex: 999 1 520px;
  min-width: 0;
  max-width: 760px;
  padding: 36px 0 80px;
}

.crumb {
  font-size: 13px;
  color: #9b9d95;
  margin-bottom: 10px;
}

.docs-h1 {
  margin: 0 0 14px;
  font-size: 34px;
  font-weight: 800;
}

.docs-lead {
  margin: 0 0 30px;
  font-size: 16px;
  line-height: 1.9;
  color: #4a4d48;
  text-wrap: pretty;
}

.notice {
  display: flex;
  gap: 12px;
  background: #fdfaf3;
  border: 1px solid #f0e6d2;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 36px;
  font-size: 14px;
  line-height: 1.75;
  color: #6f6a5c;
}

.notice-mark {
  flex: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #c9953f;
  color: #1c211e;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 700;
  margin-top: 2px;
}

.doc-step {
  display: flex;
  gap: 18px;
  margin-bottom: 34px;
}

.doc-step-n {
  flex: none;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: #175c50;
  color: #fff;
  font-family: var(--font-mono);
  font-size: 14px;
  display: grid;
  place-items: center;
}

.doc-step-body {
  flex: 1;
  min-width: 0;
}

.doc-step-body h2 {
  margin: 4px 0 8px;
  font-size: 20px;
  font-weight: 700;
}

.doc-step-body p {
  margin: 0 0 10px;
  font-size: 15px;
  line-height: 1.9;
  color: #4a4d48;
  text-wrap: pretty;
}

.doc-step-meta {
  font-size: 13px;
  color: #83867f;
}

.doc-step-path {
  font-family: var(--font-mono-cjk);
  color: #4a4d48;
}

.docs-h2 {
  margin: 48px 0 14px;
  font-size: 22px;
  font-weight: 700;
  scroll-margin-top: 16px;
}

.docs-h2.flush {
  margin-top: 0;
}

.docs-p {
  margin: 0 0 14px;
  font-size: 15px;
  line-height: 1.85;
  color: #4a4d48;
}

.keys {
  border-radius: 12px;
  margin-bottom: 40px;
}

.key-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 18px;
  border-bottom: 1px solid #f2f1ec;
  font-size: 14px;
}

.key-row:last-child {
  border-bottom: 0;
}

.key-cap-wrap {
  flex: 0 0 110px;
}

.key-cap {
  font-family: var(--font-mono);
  font-size: 12.5px;
  padding: 3px 9px;
  border-radius: 5px;
  border: 1px solid #d9d8d2;
  border-bottom-width: 2px;
  background: #faf9f6;
}

.statuses {
  border-radius: 12px;
  margin-bottom: 44px;
}

.statuses table {
  width: 100%;
  min-width: 520px;
  border-collapse: collapse;
  font-size: 14px;
}

.statuses th {
  padding: 10px 18px;
  font-weight: 500;
  border-bottom: 1px solid #eeede8;
}

.statuses th.status-col {
  width: 170px;
}

.statuses tbody tr {
  border-bottom: 1px solid #f2f1ec;
}

.statuses td {
  padding: 11px 18px;
}

.status-d {
  color: #4a4d48;
  line-height: 1.7;
}

.status {
  display: inline-block;
  padding: 3px 9px;
  border-radius: 5px;
  font-size: 12.5px;
}

.status.ok {
  background: #e9efec;
  color: #175c50;
}

.status.warn {
  background: #f6eedd;
  color: #8a5a12;
}

.status.muted {
  background: #eeede8;
  color: #6f716a;
}

.toc {
  display: none;
  flex: 0 1 180px;
  min-width: 160px;
  padding: 40px 0;
  position: sticky;
  top: 16px;
}

.toc-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-left: 1px solid #e0dfd9;
}

.toc-item {
  padding: 5px 12px;
  font-size: 13px;
  color: #83867f;
  margin-left: -1px;
  border-left: 2px solid transparent;
}

.toc-item.current {
  color: #175c50;
  border-left-color: #175c50;
}

#docs-before,
#contact {
  scroll-margin-top: 16px;
}

/* 原型按视口宽度切换的列数与吸顶，这里用媒体查询表达，不必在脚本里量宽度。 */
@media (min-width: 560px) {
  .steps {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (min-width: 860px) {
  .plans {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (min-width: 1000px) {
  .steps {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

@media (min-width: 1100px) {
  .docs-nav {
    position: sticky;
  }
}

@media (min-width: 1180px) {
  .toc {
    display: block;
  }
}
</style>
