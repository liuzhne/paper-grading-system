<script setup>
/**
 * 一条扣分细则（评分助手 v2 设计稿）：问题 + 严重度 + 扣分 + 触发条件 + 来源 + 确认 / 排除。
 * 评分助手的“核对评分规则”与评分标准页的核对步共用（v2 界面改造方案 §5）。
 * 排除即时落库（状态 rejected），不参与发布与评分；“恢复”回到待确认（排除落库改造方案 §2）。
 */
import { computed } from "vue";

const props = defineProps({
  rule: { type: Object, required: true },
  editable: Boolean,
  busy: Boolean,
  /** 每个评分项至少保留 1 条细则：本项只剩这一条未排除时不能再排除。 */
  canExclude: { type: Boolean, default: true },
});
defineEmits(["confirm", "exclude", "restore"]);

const SEVERITIES = { minor: ["轻微", "tag-muted"], moderate: ["中等", "tag-warn"], severe: ["严重", "tag-danger"] };
const DIRECTIONS = { deduct: "扣分", band: "分档", bonus: "加分", none: "人工复核" };

const pending = computed(() => ["draft", "review"].includes(props.rule.status));
const excluded = computed(() => props.rule.status === "rejected");
const severity = computed(() => SEVERITIES[props.rule.origin?.severity] || [DIRECTIONS[props.rule.direction] || "", "tag-muted"]);
const points = computed(() => {
  const rule = props.rule;
  if (rule.max_points == null) return "—";
  const sign = rule.direction === "deduct" ? "−" : rule.direction === "bonus" ? "+" : "";
  return `${sign}${Number(rule.max_points)}`;
});
const condition = computed(() => props.rule.origin?.trigger || props.rule.checker_params?.match || props.rule.rule_text || "");
// 条款原文与触发条件不同时两行都显示（评分标准页核对要看到完整条款）。
const ruleText = computed(() => (props.rule.rule_text && props.rule.rule_text !== condition.value ? props.rule.rule_text : ""));
const aiInferred = computed(() => {
  const source = props.rule.origin?.source;
  return ["ai_inferred", "llm"].includes(source) || (props.rule.ai_origin && !source && !props.rule.sources?.length);
});
/** 来源标签：原文定位（表格第几行）、AI 推断（没有原文依据，要老师确认是否保留）、其余按创建方式。 */
const source = computed(() => {
  const rule = props.rule;
  const first = rule.sources?.[0];
  if (first) return { text: `原文 ${[first.sheet, first.row ? `第 ${first.row} 行` : ""].filter(Boolean).join(" · ")}`, tone: "" };
  const ref = rule.origin?.source_refs?.[0];
  if (ref) return { text: `原文 ${ref}`, tone: "" };
  if (aiInferred.value) return { text: "AI 推断 · 无原文依据", tone: "tag-warn" };
  if (rule.origin?.source === "ai_interpreted_user_text") return { text: "AI 解读原文", tone: "tag-info" };
  return { text: { manual: "用户录入", compiler: "原文解析", legacy_upgrade: "历史原文" }[rule.creation_method] || "评分说明", tone: "tag-accent" };
});
</script>

<template>
  <article class="rule-card" :class="{ pending, rejected: excluded }" :data-rule-code="rule.rule_code">
    <div class="top">
      <div class="main">
        <div class="title-line">
          <strong class="issue">{{ rule.name }}</strong>
          <span v-if="severity[0]" class="tag" :class="severity[1]">{{ severity[0] }}</span>
          <span v-if="rule.ai_origin" class="tag tag-warn" data-test="ai-origin">AI · {{ rule.ai_model || "模型未记录" }}</span>
        </div>
        <p v-if="condition" class="cond">{{ condition }}</p>
        <p v-if="ruleText" class="cond faint-text">{{ ruleText }}</p>
        <p v-for="level in rule.levels || []" :key="level.code" class="cond">{{ level.code }} · {{ level.points }} 分：{{ level.descriptor }}</p>
      </div>
      <span class="mono points" :class="{ deduct: rule.direction === 'deduct' }">{{ points }}</span>
    </div>
    <div class="bottom">
      <span class="tag" :class="source.tone">{{ source.text }}</span>
      <details v-if="rule.sources?.length" class="sources">
        <summary>原文</summary>
        <p v-for="(item, index) in rule.sources" :key="index">{{ item.text }}</p>
      </details>
      <div class="actions">
        <template v-if="pending && editable">
          <button class="text-btn is-muted" type="button" data-test="exclude" :disabled="busy || !canExclude"
                  :title="canExclude ? '不参与评分，可随时恢复' : '每个评分项至少保留 1 条细则'" @click="$emit('exclude', rule)">排除</button>
          <button class="btn btn-sm btn-outline confirm" type="button" :disabled="busy" @click="$emit('confirm', rule)">确认</button>
        </template>
        <span v-if="rule.status === 'approved'" class="state ok">已确认</span>
        <span v-else-if="excluded" class="state" data-test="excluded">已排除<template v-if="editable"> ·
          <button class="text-btn" type="button" data-test="restore" :disabled="busy" @click="$emit('restore', rule)">恢复</button></template></span>
        <span v-else-if="pending && !editable" class="state warn">待确认</span>
      </div>
    </div>
    <slot name="details" />
  </article>
</template>

<style scoped>
.rule-card { padding: 12px 14px; border: 1px solid var(--border-light); border-radius: var(--radius-md); background: var(--surface); }
.rule-card.pending { border-color: #ecdcbc; background: #fffcf5; }
/* 已排除：内容变淡、标题划线，“恢复”保持清晰可点。 */
.rule-card.rejected .top, .rule-card.rejected .bottom > :not(.actions) { opacity: 0.55; }
.rule-card.rejected .issue { text-decoration: line-through; }
.top { display: flex; align-items: flex-start; gap: 10px; }
.main { flex: 1; min-width: 0; }
.title-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.issue { font-size: 13.5px; font-weight: 600; }
.cond { margin: 4px 0 0; font-size: 12.5px; line-height: 1.7; color: var(--neutral-ink); overflow-wrap: anywhere; }
.faint-text { color: var(--text-muted); }
.points { flex: none; font-size: 15px; font-weight: 600; color: var(--text-secondary); }
.points.deduct { color: var(--danger); }
.bottom { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
.bottom .tag { padding: 2px 8px; }
.sources { font-size: 12px; color: var(--text-muted); }
.sources summary { cursor: pointer; }
.sources p { margin: 6px 0 0; line-height: 1.7; color: var(--text-secondary); }
.actions { margin-left: auto; display: flex; align-items: center; gap: 14px; }
.btn-outline { height: 28px; font-weight: 600; }
.state { font-size: 12px; color: var(--text-muted); }
.state.ok { color: var(--accent); }
.state.warn { color: var(--warn-ink); }
</style>
