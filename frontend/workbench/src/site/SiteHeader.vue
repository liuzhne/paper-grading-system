<script setup>
import SiteMark from "./SiteMark.vue";
import { APP_URL, aboutUrl } from "./links.js";

/**
 * 官网页头。宣传页里它叠在深色首屏上（透明底），子页里自带深色底。
 *
 * 窄屏下导航换到第二行、去掉「秋季试用」角标：原型只按桌面宽度排版，四个导航、
 * 两个按钮和角标在手机宽度上会挤出横向滚动。
 */
const props = defineProps({
  /** @type {import("vue").PropType<"product"|"deploy"|"security"|"docs">} */
  active: { type: String, default: "product" },
  /** 子页自带深色底与下内边距。 */
  solid: { type: Boolean, default: false },
  /** 页头「申请试用」的去处：宣传页去院系部署，子页直接到预约表单区。 */
  trialHref: { type: String, default: () => aboutUrl("deploy") },
  /**
   * 页面自己接管「申请试用」：宣传页首屏有申请表单时，点了把光标送到邮箱框，
   * 不跳走（触发 `trial` 事件）。没有表单时照常跳转。
   */
  interceptTrial: { type: Boolean, default: false },
});

const emit = defineEmits(["trial"]);

function onTrialClick(/** @type {MouseEvent} */ event) {
  if (!props.interceptTrial) return;
  // 只接管不带修饰键的左键点击：⌘ / Ctrl / Shift / Alt 点击与中键要保留浏览器本来的
  // 行为（新标签页、新窗口、下载），否则用户没法把子页另开。
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  emit("trial");
}

const nav = [
  { key: "product", label: "产品", href: "/" },
  { key: "deploy", label: "院系部署", href: aboutUrl("deploy") },
  { key: "security", label: "数据安全", href: aboutUrl("security") },
  { key: "docs", label: "帮助文档", href: aboutUrl("docs") },
];
</script>

<template>
  <header class="site-header" :class="{ solid: props.solid }">
    <a class="brand" href="/" aria-label="有据智评首页">
      <span class="brand-mark"><SiteMark /></span>
      <span class="brand-name">有据智评</span>
    </a>
    <nav class="nav" aria-label="官网导航">
      <a
        v-for="item in nav"
        :key="item.key"
        :href="item.href"
        class="nav-link"
        :class="{ active: item.key === props.active }"
        :aria-current="item.key === props.active ? 'page' : undefined"
      >{{ item.label }}</a>
    </nav>
    <div class="actions">
      <a class="btn-login" :href="APP_URL">登录</a>
      <a class="btn-trial" :href="props.trialHref" data-test="header-trial" @click="onTrialClick">申请试用</a>
    </div>
    <div class="ribbon" aria-hidden="true">
      <span class="ribbon-tag">PUBLIC BETA</span>
      <span class="ribbon-year">2026</span>
      <span class="ribbon-label">秋季试用</span>
    </div>
  </header>
</template>

<style scoped>
.site-header {
  position: relative;
  z-index: 3;
  display: flex;
  align-items: flex-start;
  gap: 20px;
  padding: 28px clamp(20px, 3vw, 44px) 0;
  color: #fff;
}

.site-header.solid {
  z-index: 30;
  background: #1c211e;
  padding-bottom: 18px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: none;
  color: #fff;
}

.brand:hover {
  color: #fff;
}

.brand-mark {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: #175c50;
  display: grid;
  place-items: center;
}

.brand-name {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.16em;
}

.nav {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 6px 0 0 clamp(12px, 3vw, 48px);
  flex-wrap: wrap;
}

.nav-link {
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 14px;
  color: #cdd3d0;
}

.solid .nav-link {
  color: #a8b0ac;
}

.nav-link:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.06);
}

.nav-link.active,
.solid .nav-link.active {
  color: #fff;
  background: rgba(255, 255, 255, 0.1);
}

.actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 2px;
  flex: none;
}

.btn-login {
  display: inline-flex;
  align-items: center;
  height: 40px;
  padding: 0 16px;
  border-radius: 20px;
  font-size: 14px;
  color: #e7eae8;
  border: 1px solid rgba(255, 255, 255, 0.28);
}

.btn-login:hover {
  border-color: #fff;
  color: #fff;
}

.btn-trial {
  display: inline-flex;
  align-items: center;
  height: 40px;
  padding: 0 18px;
  border-radius: 20px;
  font-size: 14px;
  font-weight: 600;
  background: #f6f5f2;
  color: #1c211e;
}

.btn-trial:hover {
  background: #fff;
  color: #1c211e;
}

.ribbon {
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 12px 16px 16px;
  background: #f6f5f2;
  color: #1c211e;
  border-radius: 0 0 10px 10px;
  margin-top: -28px;
  clip-path: polygon(0 0, 100% 0, 100% 86%, 50% 100%, 0 86%);
}

.ribbon-tag {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.18em;
  color: #175c50;
  margin-top: 14px;
}

.ribbon-year {
  font-family: var(--font-mono);
  font-size: 24px;
  font-weight: 600;
  line-height: 1.1;
  margin-top: 4px;
}

.ribbon-label {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.2em;
  margin-top: 2px;
}

@media (max-width: 900px) {
  .ribbon {
    display: none;
  }
}

@media (max-width: 720px) {
  .site-header {
    flex-wrap: wrap;
    gap: 14px 12px;
    padding-top: 20px;
  }

  .nav {
    order: 3;
    width: 100%;
    margin: 0 0 0 -12px;
  }

  .brand-name {
    display: none;
  }
}
</style>
