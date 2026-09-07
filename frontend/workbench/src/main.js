import { createApp } from "vue";
import { createPinia } from "pinia";

import App from "@/App.vue";
import { router } from "@/router/index.js";
import "@/styles/tokens.css";

createApp(App).use(createPinia()).use(router).mount("#app");
