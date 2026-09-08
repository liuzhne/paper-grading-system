import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore } from "./session.js";
import { useBatchesStore } from "./batches.js";
import { useReviewStore } from "./review.js";
import { useUploadStore } from "./upload.js";

/**
 * `/auth/me` 返回的是嵌套结构 `{auth_required, user, organization}`，
 * 不是扁平字段。曾按扁平字段读取导致侧边栏在开发模式下显示「未登录」。
 */
describe("session store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function stubRoutes(routes) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        const path = String(url).replace("/api", "");
        if (!(path in routes)) {
          return Promise.resolve(new Response("{}", { status: 404 }));
        }
        const [status, body] = routes[path];
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
  }

  it("读取启用鉴权时的嵌套身份结构", async () => {
    stubRoutes({
      "/auth/me": [
        200,
        {
          auth_required: true,
          user: { id: "u1", username: "liuzhen", display_name: "刘珍", platform_role: "member" },
          organization: { id: "org-1", role: "org_admin" },
        },
      ],
      "/organizations": [200, [{ id: "org-1", name: "计算机学院" }]],
      "/system/capabilities": [
        200,
        { platform_role: "member", abilities: { manage_members: true } },
      ],
    });

    const session = useSessionStore();
    await session.bootstrap();

    expect(session.status).toBe("authenticated");
    expect(session.authEnforced).toBe(true);
    expect(session.organizationId).toBe("org-1");
    expect(session.organizationRole).toBe("org_admin");
    expect(session.organization?.name).toBe("计算机学院");
    expect(session.can("manage_members")).toBe(true);
    expect(session.can("view_platform_ops")).toBe(false);
  });

  it("开发模式下 user 为 null，但不是未登录", async () => {
    stubRoutes({
      "/auth/me": [200, { auth_required: false, user: null, organization: null }],
      "/organizations": [200, []],
      "/system/capabilities": [200, { platform_role: "developer", abilities: {} }],
    });

    const session = useSessionStore();
    await session.bootstrap();

    expect(session.status).toBe("authenticated");
    expect(session.authEnforced).toBe(false);
    expect(session.organizationId).toBeNull();
    // 平台角色回落到能力表，避免身份为空时整片界面失去判据。
    expect(session.platformRole).toBe("developer");
  });

  it("401 判为未登录，不当作加载失败", async () => {
    stubRoutes({ "/auth/me": [401, { detail: "未登录或会话失效" }] });

    const session = useSessionStore();
    await session.bootstrap();

    expect(session.status).toBe("anonymous");
    expect(session.identity).toBeNull();
    expect(session.loadError).toBeNull();
  });

  it("组织列表加载失败不影响会话可用", async () => {
    stubRoutes({
      "/auth/me": [
        200,
        {
          auth_required: true,
          user: { id: "u1", username: "x", platform_role: "member" },
          organization: { id: "org-1", role: "member" },
        },
      ],
      "/organizations": [503, { detail: "数据库暂不可用" }],
      "/system/capabilities": [200, { platform_role: "member", abilities: {} }],
    });

    const session = useSessionStore();
    await session.bootstrap();

    expect(session.status).toBe("authenticated");
    expect(session.organizations).toEqual([]);
  });
});

describe("组织切换清场（计划 §2.1）", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function jsonResponse(body, status = 200) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  function stubSwitch() {
    vi.stubGlobal("fetch", (url) => {
      const path = String(url);
      if (path.includes("/auth/me")) {
        return jsonResponse({ organization: { id: "org-b" } });
      }
      if (path.includes("/system/capabilities")) return jsonResponse({});
      return jsonResponse({});
    });
  }

  it("切换后不留上一个组织的批次与队列", async () => {
    stubSwitch();
    const session = useSessionStore();
    const batches = useBatchesStore();
    const review = useReviewStore();
    session.organizationId = "org-a";
    batches.batches = [{ id: "b1", name: "A 组织的批次", status: "scored" }];
    review.entries = [{ score_item_id: "s1", student_name: "张三" }];

    await session.switchOrganization("org-b");

    // 旧组织的学生姓名留在界面上，就是一次跨组织泄露。
    expect(batches.batches).toEqual([]);
    expect(review.entries).toEqual([]);
  });

  it("切换前停掉旧组织的上传调度", async () => {
    stubSwitch();
    const session = useSessionStore();
    const upload = useUploadStore();
    session.organizationId = "org-a";
    upload.uploadedPaperIds = ["p1"];

    await session.switchOrganization("org-b");

    // 继续上传会把文件写进旧组织的批次。
    expect(upload.uploadedPaperIds).toEqual([]);
    expect(upload.queue).toEqual([]);
  });

  it("切到同一个组织时不做清场", async () => {
    stubSwitch();
    const session = useSessionStore();
    const batches = useBatchesStore();
    session.organizationId = "org-a";
    batches.batches = [{ id: "b1", name: "保留", status: "scored" }];

    await session.switchOrganization("org-a");

    expect(batches.batches).toHaveLength(1);
  });

  it("登出同样清场", async () => {
    stubSwitch();
    const session = useSessionStore();
    const batches = useBatchesStore();
    batches.batches = [{ id: "b1", name: "登出前", status: "scored" }];

    await session.logout();

    expect(batches.batches).toEqual([]);
  });
});

describe("组织切换失败时不留半切状态", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function jsonResponse(body, status = 200) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  it("服务端拒绝切换时组织上下文回到原值", async () => {
    vi.stubGlobal("fetch", (url) => {
      if (String(url).includes("/auth/organization-context")) {
        return jsonResponse({ detail: "不是该组织成员" }, 403);
      }
      return jsonResponse({});
    });
    const session = useSessionStore();
    session.organizationId = "org-a";

    await expect(session.switchOrganization("org-b")).rejects.toThrow();

    // 客户端认为在 B、服务端还在 A，写请求的 X-Organization-ID 就会指错组织。
    expect(session.organizationId).toBe("org-a");
  });

  it("失败后仍然清了缓存，不留上一个组织的数据", async () => {
    vi.stubGlobal("fetch", (url) => {
      if (String(url).includes("/auth/organization-context")) {
        return jsonResponse({ detail: "boom" }, 500);
      }
      return jsonResponse({});
    });
    const session = useSessionStore();
    const batches = useBatchesStore();
    session.organizationId = "org-a";
    batches.batches = [{ id: "b1", name: "A 的批次", status: "scored" }];

    await expect(session.switchOrganization("org-b")).rejects.toThrow();

    // 在途请求已被 abort、缓存已清：重新加载即可，但不能留着可能已过期的内容。
    expect(batches.batches).toEqual([]);
  });
});
