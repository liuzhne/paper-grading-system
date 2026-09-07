<script setup>
import StageStub from "@/components/StageStub.vue";
</script>

<template>
  <StageStub
    title="工作台"
    stage="1"
    summary="KPI、阶段计数、继续处理与分数分布。"
    :scope="[
      '批次 / 待评 / 待确认 / 解析失败 KPI，分母取当前目标集合',
      '继续处理卡片：解析 → 评分 → 复核 → 导出 四步进度',
      '分数分布仅统计同一结果选择口径下的有效终分，另报未评与阻塞数',
      '分桶与「上一批次」定义未定前不展示跨标准均分差',
    ]"
  />
</template>
