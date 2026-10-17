import { createApp } from "vue";

import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "@fontsource/ibm-plex-mono/latin-600.css";

import "./site.css";
import AboutPage from "./AboutPage.vue";

createApp(AboutPage).mount("#app");
