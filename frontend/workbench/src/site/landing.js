import { createApp } from "vue";

// 与工作台同一份自托管 IBM Plex Mono（SIL OFL 1.1，离线部署不能走 Google Fonts）。
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "@fontsource/ibm-plex-mono/latin-600.css";

import "./site.css";
import LandingPage from "./LandingPage.vue";
import { redirectIfSignedIn } from "./session-redirect.js";

// 先渲染再查会话：访客不必等一次接口往返；已登录的人会先看到一瞬宣传页再跳走
// （用户接受的代价，2026-10-17）。
createApp(LandingPage).mount("#app");
redirectIfSignedIn();
