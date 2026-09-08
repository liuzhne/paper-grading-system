import { defineConfig, devices } from "@playwright/test";

/**
 * 浏览器验收（前端 v2 计划 §12.1，V01–V13）。
 *
 * 被测对象是**统一组装产物**（`public/`）经真实 FastAPI 托管的结果，不是
 * Vite dev server。dev server 有自己的中间件与模块图，测不出深链接回退、
 * 缺失资源必须 404、`/api` 不被 SPA 吞掉这几条——而它们恰恰只在生产托管
 * 路径上才会出问题。
 *
 * 数据用合成文档与 Mock LLM，库与存储隔离在 tmp 下；真实论文、Secret 与
 * 学生 PII 不得进入截图或 trace。
 */
const PORT = Number(process.env.PGS_E2E_PORT || 8099);
// 第二个后端跑在 AUTH_ENABLED=true 下，带两个组织与三种角色。V01/V02/V10 测的
// 就是边界本身，开发模式「放行一切」时它们全都会假通过。
const AUTH_PORT = PORT + 1;
const PYTHON = process.env.PGS_PYTHON || ".venv/bin/python";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 30_000,
  expect: { timeout: 8_000 },
  use: {
    // 用完整 Chromium 的新版 headless，而不是默认的 headless shell：验收要证明
    // 的是真实浏览器里的渲染与交互，shell 砍掉了一部分浏览器行为，且它是需要
    // 单独下载的第二份产物。`playwright install chromium` 已经包含完整构建。
    channel: "chromium",
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "zh-CN",
  },
  projects: [
    // 窄屏用例只在 mobile project 跑：桌面宽度下三栏本就不该折叠，
    // 在这里跑它是在断言一个错误的期望。
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      // auth.spec 要跑在鉴权后端上：对着开发模式的「放行一切」跑权限断言，
      // 通过与否都说明不了任何事。
      testIgnore: /(responsive|auth)\.spec\.js/,
    },
    // 窄屏折叠：设计三栏宽度不是唯一布局（计划 §8）。
    { name: "mobile", use: { ...devices["Pixel 5"] }, testMatch: /responsive\.spec\.js/ },
    // 鉴权与多组织（V01/V02/V10）。指向另一个后端，baseURL 不同。
    {
      name: "auth",
      use: {
        ...devices["Desktop Chrome"],
        channel: "chromium",
        baseURL: `http://127.0.0.1:${AUTH_PORT}`,
      },
      testMatch: /auth\.spec\.js/,
    },
  ],
  webServer: [
    {
    // 显式指向仓库 venv：`python` 在 PATH 上可能是系统解释器，那里没有本项目
    // 的依赖，webServer 会静默起不来、Playwright 一直等到超时。
    // 路径相对 `cwd`（下面已切到仓库根），不是相对本配置文件。
    command: `${PYTHON} -m e2e_server ${PORT}`,
    cwd: "../..",
    url: `http://127.0.0.1:${PORT}/api/system/integrations`,
    // 永不复用：验收里有写操作（批量采纳），复用同一个进程意味着第二次
    // 起跑时队列已被上一次跑空，本地重跑会假失败。重建一次库只要几秒。
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
    },
    {
      command: `${PYTHON} -m e2e_server ${AUTH_PORT} --auth`,
      cwd: "../..",
      url: `http://127.0.0.1:${AUTH_PORT}/api/system/integrations`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
