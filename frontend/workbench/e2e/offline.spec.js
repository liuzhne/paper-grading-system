import { expect, test } from "@playwright/test";

/**
 * V13 · 离线与新旧并存（前端 v2 计划 §12.1、§8.2）。
 *
 * 「离线运行不访问 CDN」不是一句愿望：内网部署里一个 fonts.googleapis.com 的
 * 请求不会报错，只会让页面**卡在等待超时**，看起来像后端慢。字体已经自托管，
 * 这条守住的是「以后别人加回来一个外链」。
 */
const ALLOWED_HOST = "127.0.0.1";

async function collectExternalRequests(page, path) {
  const external = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol === "data:" || url.protocol === "blob:") return;
    if (url.hostname === ALLOWED_HOST || url.hostname === "localhost") return;
    external.push(request.url());
  });
  await page.goto(path);
  await page.waitForLoadState("networkidle");
  return external;
}

test.describe("V13 离线运行", () => {
  test("工作台不请求任何外部资源", async ({ page }) => {
    const external = await collectExternalRequests(page, "/workbench/");

    expect(external).toEqual([]);
  });

  test("评分工作区同样不外联", async ({ page }) => {
    const external = await collectExternalRequests(page, "/workbench/tasks");

    expect(external).toEqual([]);
  });

  test("字体来自同源产物而不是 CDN", async ({ page }) => {
    const fonts = [];
    page.on("request", (request) => {
      if (request.resourceType() === "font") fonts.push(request.url());
    });
    await page.goto("/workbench/");
    await page.waitForLoadState("networkidle");

    for (const url of fonts) {
      expect(url).toContain("/workbench/assets/");
    }
  });

  test("字体许可证随产物发布且可取", async ({ request }) => {
    // SIL OFL 1.1 要求许可证随字体分发。
    const response = await request.get("/workbench/LICENSE-IBM-Plex-Mono.txt");

    expect(response.status()).toBe(200);
    expect((await response.text()).toUpperCase()).toContain(
      "SIL OPEN FONT LICENSE",
    );
  });
});

test.describe("V13 新旧并存", () => {
  test("旧入口与新页同时可达", async ({ page }) => {
    const legacy = await page.goto("/legacy/");
    expect(legacy.status()).toBe(200);

    const workbench = await page.goto("/workbench/");
    expect(workbench.status()).toBe(200);
  });

  test("两套产物的资源不互相冲突", async ({ request }) => {
    // 旧资源在 /assets/*，新资源在 /workbench/assets/*；任何一边被另一边的
    // 回退吞掉，都会表现成难以定位的语法错误。
    const stray = await request.get("/assets/does-not-exist.js");
    expect(stray.status()).toBe(404);

    const strayWorkbench = await request.get("/workbench/assets/nope.js");
    expect(strayWorkbench.status()).toBe(404);
  });
});
