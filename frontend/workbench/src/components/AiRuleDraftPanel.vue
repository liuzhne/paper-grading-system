<script setup>
import { computed, ref, watch } from "vue";

import { draftRows, rowKey } from "@/lib/ai-draft.js";

/**
 * AI 起草建议的确认面板（设计稿「扣分规则」区块，V3-2）。
 *
 * 起草端点是 **non-persistent** 的：它返回建议，一条也不落库。所以这个面板做的
 * 是「看清、逐条排除、把最终集合交出去」，落库由调用方走 `recompile` 完成。
 *
 * 逐条排除而不是逐条勾选确认：起草结果默认全部待应用，用户要做的是**挑出不要的
 * 那几条**。反过来要求逐条打勾，会让一次生成十几条规则的常见情形变成十几次点击，
 * 而多数时候用户其实全都要。
 */
const props = defineProps({
  /** `POST /draft-deduction-rules` 返回的 items。 */
  items: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
});

const emit = defineEmits(["apply", "discard"]);

/** 被排除的行键。默认空集合——生成出来的规则默认都要。 */
const excluded = ref(new Set());

const rows = computed(() =>
  (props.items || []).flatMap((item) => draftRows(item.draft)),
);

const kept = computed(() => rows.value.filter((row) => !excluded.value.has(rowKey(row))));

/** 用来源与模型说明这批规则是谁产出的。 */
const metadata = computed(() => {
  for (const item of props.items || []) {
    const meta = item?.draft?.generation_metadata;
    if (meta) return meta;
  }
  return null;
});

// 换了一批起草结果，之前的排除不该跟着走：那些行键指的是上一次生成的规则。
watch(
  () => props.items,
  () => {
    excluded.value = new Set();
  },
);

function toggle(row) {
  const key = rowKey(row);
  const next = new Set(excluded.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  excluded.value = next;
}

function isExcluded(row) {
  return excluded.value.has(rowKey(row));
}
</script>

<template>
  <section v-if="rows.length" class="card draft-panel" data-test="draft-panel">
    <div class="card-head">
      <div>
        <h2 class="card-title">AI 起草的扣分规则</h2>
        <p class="card-note">
          同一问题的严重程度互斥，评分时最多命中一档。
          <span v-if="metadata" class="faint mono model">
            {{ metadata.provider }} · {{ metadata.model_name }}
          </span>
        </p>
      </div>
      <span class="chip chip-warn">待确认</span>
    </div>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>问题类型</th>
            <th>严重程度</th>
            <th>扣分</th>
            <th>触发条件</th>
            <th>来源</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="rowKey(row)" :class="{ dropped: isExcluded(row) }">
            <td>
              <div>{{ row.issue }}</div>
              <div class="faint mono code">{{ row.criterionCode }} · {{ row.groupCode }}</div>
            </td>
            <td><span class="chip">{{ row.severityLabel }}</span></td>
            <td class="num">{{ row.points }}</td>
            <td class="trigger">
              <div>{{ row.trigger }}</div>
              <div class="faint reason">{{ row.reason }}</div>
            </td>
            <td class="muted">{{ row.sourceLabel }}</td>
            <td class="actions">
              <button
                class="btn btn-sm"
                type="button"
                data-test="exclude"
                @click="toggle(row)"
              >
                {{ isExcluded(row) ? "已排除 · 撤销" : "排除" }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card-foot foot">
      <p class="foot-note">
        未确认的规则不会进入可执行评分版本。确认会记录确认人、时间与生成指纹。
      </p>
      <div class="btn-row">
        <button class="btn" type="button" data-test="discard" @click="emit('discard')">
          丢弃这批建议
        </button>
        <button
          class="btn btn-primary"
          type="button"
          data-test="apply"
          :disabled="busy || !kept.length"
          @click="emit('apply', excluded)"
        >
          {{ busy ? "应用中…" : `确认并应用（${kept.length}）` }}
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.draft-panel {
  margin-bottom: 18px;
}

.model {
  margin-left: 6px;
}

.code {
  font-size: 11.5px;
  margin-top: 3px;
}

.trigger {
  max-width: 300px;
}

.reason {
  font-size: 12px;
  margin-top: 3px;
}

.actions {
  white-space: nowrap;
}

/* 被排除的行留在原地、压暗——直接抽走会让用户失去「我刚才排除了哪一条」的位置感。 */
.dropped {
  opacity: 0.45;
}

.foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.foot-note {
  margin: 0;
  flex: 1 1 260px;
}
</style>
