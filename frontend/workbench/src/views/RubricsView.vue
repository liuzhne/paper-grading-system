<script setup>
import StageStub from "@/components/StageStub.vue";
</script>

<template>
  <StageStub
    title="评分标准"
    stage="5"
    summary="模板库与基本信息 / 评分规则 / 校验发布三步详情。"
    :scope="[
      '规则来源区分用户原文与 AI 建议，保留 SourceRule 溯源',
      '逐条审核与阻断项定位，冻结版本与可见范围齐全',
      'UI 重组不修改授权规则、分值、冻结版本与评分政策',
    ]"
  />
</template>
