<script setup>
/**
 * 鉴权页的共用外壳（登录 / 邀请注册 / 密码重置）。
 *
 * 抽出来是因为旧壳下线后这三条路由都归工作台承接，而它们的左栏、栅格与表单
 * 样式完全一致——复制三份意味着以后改品牌文案要改三处，漏一处就出现两套外观。
 */
defineProps({
  heading: { type: String, required: true },
  sub: { type: String, default: "" },
});
</script>

<template>
  <div class="auth">
    <section class="pitch">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">
          <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#fff"
               stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 8.6V4h4.6" />
            <path d="M20 15.4V20h-4.6" />
            <path d="M8.2 12.4l3 3 5.2-5.8" />
          </svg>
        </span>
        <span class="brand-name">有据智评</span>
      </div>

      <div class="pitch-body">
        <p class="eyebrow mono">GRADING &amp; REVIEW WORKBENCH</p>
        <h1>每一个分数<br />都能回到原文那一段</h1>
        <p class="lede">
          面向课程作业与学位论文的批量评审系统。评分标准以已发布版本固化，评分结果逐项标注证据出处，人工复核留痕后统一导出。
        </p>
        <ol class="points">
          <li><span class="mono">01</span><span>评分标准经审核发布后不可变，全批次口径一致</span></li>
          <li><span class="mono">02</span><span>每项给分附页码与章节引用，可点击回到原文</span></li>
          <li><span class="mono">03</span><span>改分写入复核记录，报告与成绩单一次导出</span></li>
        </ol>
      </div>

      <p class="foot mono">评审工作台</p>
    </section>

    <section class="form-side">
      <div class="form-wrap">
        <h2>{{ heading }}</h2>
        <p v-if="sub" class="sub">{{ sub }}</p>
        <slot />
      </div>
    </section>
  </div>
</template>

<style>
@import "@/styles/auth.css";
</style>
