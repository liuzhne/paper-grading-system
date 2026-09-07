import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore } from "./session.js";

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
