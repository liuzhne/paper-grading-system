import { createApp } from "vue";
import { createPinia } from "pinia";

import App from "@/App.vue";
import { router } from "@/router/index.js";
// IBM Plex Mono 的 latin 子集（数字、拉丁字母）。经 npm 引入并由 Vite 打包成
// 自托管资源——离线部署要求资源可完全本地化，不能走 Google Fonts CDN。
// 许可为 SIL OFL 1.1，LICENSE 随包分发。中文正文继续走系统字体栈，不引入
// CJK 字体（那是几 MB 量级）。
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "@fontsource/ibm-plex-mono/latin-600.css";

import "@/styles/tokens.css";
import "@/styles/components.css";

createApp(App).use(createPinia()).use(router).mount("#app");
