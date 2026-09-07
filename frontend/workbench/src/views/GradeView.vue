<script setup>
import StageStub from "@/components/StageStub.vue";
</script>

<template>
  <div class="pad">
    <StageStub
      title="评分工作区"
      stage="3"
      summary="材料列表 / 结构化正文 / 评分表三栏。"
      :scope="[
        '正文来自运行绑定的冻结投影：Core 用 DocumentSnapshot，legacy 用 PaperChunk 并标明可变解析来源',
        '证据定位失败时显示原因，不伪造引文、页码或空高亮',
        '普通改分与阻塞任务操作分开；阻塞未解决前不出现完整总分与等级',
        '分页与锚点加载、相邻材料、键盘导航、窄屏折叠',
      ]"
    />
  </div>
</template>

<style scoped>
.pad {
  padding: 30px 36px 56px;
  max-width: var(--content-max);
}
</style>
