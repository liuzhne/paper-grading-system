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
