import { defineStore } from "pinia";
import { ref, computed } from "vue";

import { api, resetContext, StaleContextError } from "@/api/client.js";

/**
 * 会话与组织上下文。
 *
 * 组织切换合同（计划 §2.1）：切换前由调用方处理未保存改分并停止上传调度；
 * 切换时 resetContext() 中止在途请求，随后清空本 store 缓存的组织级数据。
 */
export const useSessionStore = defineStore("session", () => {
  const status = ref("unknown"); // unknown | anonymous | authenticated
  const identity = ref(null);
  const organizations = ref([]);
  const organizationId = ref(null);
  const capabilities = ref(null);
  const loadError = ref(null);
  /** 显式开发模式（AUTH_ENABLED=false）。绝不能当作生产权限证明。 */
  const authEnforced = ref(true);

  const organization = computed(
    () => organizations.value.find((o) => o.id === organizationId.value) || null,
  );
  // /auth/me 是嵌套结构：{auth_required, user, organization}；
  // 关闭鉴权的开发模式下 user/organization 均为 null。
  const organizationRole = computed(() => identity.value?.organization?.role ?? null);
  const platformRole = computed(
    () => identity.value?.user?.platform_role ?? capabilities.value?.platform_role ?? null,
  );
  const isPlatformAdmin = computed(() => platformRole.value === "platform_admin");
  const isOrgAdmin = computed(
    () => isPlatformAdmin.value || organizationRole.value === "org_admin",
  );

  /** 能力表由服务端下发；前端不自行推断权限，只用它决定导航可见性。 */
  function can(key) {
    return Boolean(capabilities.value?.abilities?.[key]);
  }

  async function bootstrap() {
    loadError.value = null;
    try {
      identity.value = await api.get("/auth/me");
      authEnforced.value = identity.value?.auth_required !== false;
      status.value = "authenticated";
      organizationId.value = identity.value?.organization?.id ?? null;
      await Promise.all([loadOrganizations(), loadCapabilities()]);
    } catch (error) {
      if (error instanceof StaleContextError) return;
      if (error?.status === 401) {
        status.value = "anonymous";
        identity.value = null;
        return;
      }
      loadError.value = error?.message || "加载会话失败";
      status.value = "anonymous";
    }
  }

  async function loadOrganizations() {
    try {
      organizations.value = (await api.get("/organizations")) || [];
    } catch (error) {
      if (!(error instanceof StaleContextError)) organizations.value = [];
    }
  }

  async function loadCapabilities() {
    try {
      capabilities.value = await api.get("/system/capabilities");
    } catch (error) {
      if (!(error instanceof StaleContextError)) capabilities.value = null;
    }
  }

  /** 切换组织：作废在途请求 → 服务端切上下文 → 重载身份与能力。 */
  /**
   * 清空所有装着组织级数据的 store（计划 §2.1）。
   *
   * `resetContext()` 只中止在途请求；已经渲染出来的批次、复核队列、材料列表
   * 还留在内存里。切换组织后它们会继续显示——上一个组织的学生姓名留在界面上
   * 就是一次跨组织泄露，而且下一次写请求可能落到错误的批次上。
   *
   * 动态 import 避开循环依赖：那几个 store 反过来会用到 session。
   */
  async function clearOrganizationScopedStores() {
    const [batches, grading, review, upload] = await Promise.all([
      import("@/stores/batches.js"),
      import("@/stores/grading.js"),
      import("@/stores/review.js"),
      import("@/stores/upload.js"),
    ]);
    // 先停上传调度再清：继续上传会把文件写进旧组织的批次。
    upload.useUploadStore().cancel();
    batches.useBatchesStore().reset();
    grading.useGradingStore().reset();
    review.useReviewStore().reset();
    upload.useUploadStore().reset();
  }

  async function switchOrganization(nextId) {
    if (nextId === organizationId.value) return;
    resetContext();
    await clearOrganizationScopedStores();
    capabilities.value = null;
    organizationId.value = nextId;
    await api.post("/auth/organization-context", { organization_id: nextId });
    identity.value = await api.get("/auth/me");
    organizationId.value = identity.value?.organization?.id ?? nextId;
    await loadCapabilities();
  }

  async function logout() {
    await api.post("/auth/logout");
    resetContext();
    await clearOrganizationScopedStores();
    identity.value = null;
    organizations.value = [];
    organizationId.value = null;
    capabilities.value = null;
    status.value = "anonymous";
  }

  return {
    status,
    identity,
    organizations,
    organizationId,
    organization,
    organizationRole,
    platformRole,
    isPlatformAdmin,
    isOrgAdmin,
    capabilities,
    loadError,
    authEnforced,
    can,
    bootstrap,
    loadOrganizations,
    loadCapabilities,
    switchOrganization,
    logout,
  };
});
