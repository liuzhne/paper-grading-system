<script setup>
import { APP_URL, CONTACT_EMAIL, aboutUrl, mailto } from "./links.js";

/**
 * 官网页脚。原型里的「使用条款」是空链接——目前没有条款页，不放一个点了没反应
 * 的入口。
 */
defineProps({
  /** 子页的页脚内容限宽居中，宣传页的页脚通栏。 */
  contained: { type: Boolean, default: false },
});
</script>

<template>
  <footer class="site-footer">
    <div class="inner" :class="{ contained }">
      <span class="name">有据智评</span>
      <a :href="aboutUrl('deploy')">院系部署</a>
      <a :href="aboutUrl('security')">数据安全</a>
      <a :href="aboutUrl('docs')">帮助文档</a>
      <a :href="mailto(CONTACT_EMAIL, '有据智评 · 联系我们')">联系我们</a>
      <a class="app" :href="APP_URL">登录工作台</a>
      <span class="copy">© 2026 有据智评</span>
    </div>
  </footer>
</template>

<style scoped>
.site-footer {
  background: #1c211e;
  color: #a8b0ac;
}

.inner {
  padding: 36px clamp(20px, 3vw, 44px);
  display: flex;
  gap: 16px 32px;
  flex-wrap: wrap;
  align-items: center;
  font-size: 13.5px;
}

.inner.contained {
  max-width: 1280px;
  margin: 0 auto;
  padding: 36px clamp(20px, 3vw, 40px);
}

.name {
  color: #fff;
  font-weight: 600;
  letter-spacing: 0.14em;
}

a {
  color: #a8b0ac;
}

a:hover {
  color: #fff;
}

a.app {
  color: #e7eae8;
  font-weight: 600;
}

.copy {
  margin-left: auto;
}
</style>
