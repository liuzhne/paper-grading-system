<script setup>
import StageStub from "@/components/StageStub.vue";
</script>

<template>
  <StageStub
    title="账户与连接"
    stage="5"
    summary="身份、组织成员与私有 AI 连接。"
    :scope="[
      '当前身份与组织角色、成员管理、邀请与密码重置令牌',
      '私有 BYOK 连接的保存、测试、换 Key、停用与删除',
      '组织管理员身份不得因此可查看其他用户的 Key',
    ]"
  />
</template>
