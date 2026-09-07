<script setup>
import StageStub from "@/components/StageStub.vue";
</script>

<template>
  <StageStub
    title="输出中心"
    stage="6"
    summary="HTML 报告 / 成绩单 / 结构化 JSON / Sheets 与导出审计。"
    :scope="[
      '预检区分告警与阻塞：未解决阻塞时禁止导出正式成绩',
      '历史显示通道、范围、操作人与生成状态；生成成功不等于下载完成',
      '旧日志四种 target_type 保留原值，历史粒度不强行合并',
      'Sheets 可用性从鉴权能力投影读取，离线时禁用',
    ]"
  />
</template>
