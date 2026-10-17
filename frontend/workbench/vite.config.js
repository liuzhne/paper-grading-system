import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// 新页面统一挂在 /workbench/ 下（计划 §8.3 新旧并存）。
// 资源目录独立为 /workbench/assets/*，与旧 SPA 的 /assets/* 不冲突，
// 只有带 hash 的资源允许 immutable 缓存（§8.2）。
export default defineConfig({
  base: "/workbench/",
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    // 离线部署要求资源可完全本地化，禁止内联外链。
    assetsInlineLimit: 0,
    // 官网（宣传页与子页）与工作台同一次构建、同一个资源目录，但入口独立：
    // 宣传页不加载工作台主包。scripts/build_web_static.py 把 dist/site/ 下的两张
    // 入口页组装到 `/` 与 `/about`。
    rollupOptions: {
      input: {
        workbench: fileURLToPath(new URL("./index.html", import.meta.url)),
        site: fileURLToPath(new URL("./site/index.html", import.meta.url)),
        about: fileURLToPath(new URL("./site/about/index.html", import.meta.url)),
      },
      output: {
        // 三个入口共用的 Vue 运行时单独成块并按名字可辨认；不指定时 Rollup 会拿
        // 块里第一个模块命名，网络面板里看到的是一个叫 latin-600 的 JS。
        manualChunks: (id) => (/node_modules\/@?vue\//.test(id) ? "vue" : undefined),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 开发代理保持同源 Cookie 语义（§8.2 本地 FastAPI 行）。
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.js"],
  },
});
