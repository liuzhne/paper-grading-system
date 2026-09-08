<script setup>
import { computed, ref } from "vue";
import { RouterView, useRoute, useRouter } from "vue-router";

import { useSessionStore } from "@/stores/session.js";

const session = useSessionStore();
const route = useRoute();
const router = useRouter();

const menuOpen = ref(false);

const activeNav = computed(() => route.meta.nav || "");
// 评分工作区是整屏三栏布局，不套用常规内容边距。
const fullBleed = computed(() => route.meta.chrome === "full");

// /auth/me 是 {auth_required, user, organization} 的嵌套结构；
// 关闭鉴权的开发模式下 user 为 null，此时不能显示成「未登录」。
const user = computed(() => session.identity?.user ?? null);
const displayName = computed(() => {
  if (user.value) return user.value.display_name || user.value.username;
  return session.authEnforced ? "未登录" : "开发模式";
});
const avatarText = computed(() => (displayName.value || "?").slice(0, 1));
const contextLine = computed(() => {
  if (!session.authEnforced) return "AUTH_ENABLED=false，非生产权限";
  const org = session.organization?.name;
  const role = session.organizationRole;
  return [org, role].filter(Boolean).join(" · ") || "无组织上下文";
});


async function onLogout() {
  await session.logout();
  await router.push({ name: "login" });
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="#fff"
               stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 8.6V4h4.6" />
            <path d="M20 15.4V20h-4.6" />
            <path d="M8.2 12.4l3 3 5.2-5.8" />
          </svg>
        </span>
        <span>
          <span class="brand-name">有据智评</span>
          <span class="brand-sub">评审工作台</span>
        </span>
      </div>

      <nav class="nav" aria-label="主导航">
        <div class="nav-label">评审</div>
        <RouterLink class="nav-item" :class="{ active: activeNav === 'home' }" :to="{ name: 'dashboard' }">
          工作台
        </RouterLink>
        <RouterLink class="nav-item" :class="{ active: activeNav === 'tasks' }" :to="{ name: 'tasks' }">
          评分任务
        </RouterLink>
        <RouterLink class="nav-item" :class="{ active: activeNav === 'review' }" :to="{ name: 'review' }">
          结果复核
        </RouterLink>

        <div class="nav-label">标准与输出</div>
        <RouterLink class="nav-item" :class="{ active: activeNav === 'rubric' }" :to="{ name: 'rubrics' }">
          评分标准
        </RouterLink>
        <RouterLink class="nav-item" :class="{ active: activeNav === 'export' }" :to="{ name: 'exports' }">
          输出中心
        </RouterLink>
        <RouterLink class="nav-item" :class="{ active: activeNav === 'account' }" :to="{ name: 'account' }">
          账户与连接
        </RouterLink>

        <!-- 运维页不进主分组。可见性只由服务端下发的能力表决定（计划 §2.1）；
             前端不自行推断角色，真正的权限由各端点服务端执行。 -->
        <template v-if="session.can('view_organization_ops') || session.can('view_platform_ops')">
          <div class="nav-label">运维</div>
          <RouterLink class="nav-item" :class="{ active: activeNav === 'ops' }" :to="{ name: 'ops' }">
            运维与质量
          </RouterLink>
        </template>
      </nav>

      <div class="sidebar-footer">

        <button class="account" type="button" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen">
          <span class="avatar" aria-hidden="true">{{ avatarText }}</span>
          <span class="account-copy">
            <span class="account-name">{{ displayName }}</span>
            <span class="account-context">{{ contextLine }}</span>
          </span>
          <span class="account-caret" aria-hidden="true">⌄</span>
        </button>

        <div v-if="menuOpen" class="account-menu" role="menu">
          <button
            v-if="session.authEnforced"
            type="button"
            role="menuitem"
            @click="onLogout"
          >
            退出登录
          </button>
          <p v-else class="dev-note">开发模式未启用鉴权，无会话可退出。</p>
        </div>
      </div>
    </aside>

    <main class="workspace" :class="{ 'full-bleed': fullBleed }">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  min-height: 100vh;
}

.sidebar {
  width: var(--sidebar-width);
  flex: none;
  background: var(--sidebar-bg);
  color: #fff;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
}

.brand {
  padding: 20px 18px;
  display: flex;
  align-items: center;
  gap: 11px;
}

.brand-mark {
  width: 32px;
  height: 32px;
  border-radius: 9px;
  background: var(--accent);
  display: grid;
  place-items: center;
  flex: none;
}

.brand-name {
  display: block;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.12em;
}

.brand-sub {
  display: block;
  font-size: 11px;
  color: var(--sidebar-text-faint);
  margin-top: 2px;
}

.nav {
  padding: 0 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}

.nav-label {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.16em;
  color: var(--sidebar-label);
  padding: 14px 8px 8px;
}

.nav-item {
  display: block;
  padding: 9px 10px;
  border-radius: 7px;
  font-size: 13.5px;
  color: var(--sidebar-text-muted);
  background: transparent;
}

.nav-item:hover {
  background: var(--sidebar-item-hover-bg);
  color: var(--sidebar-text);
}

.nav-item.active {
  background: var(--sidebar-item-active-bg);
  color: #fff;
}

.sidebar-footer {
  margin-top: auto;
  padding: 14px 14px 16px;
  border-top: 1px solid var(--sidebar-divider);
}





.account {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  border: 0;
  background: transparent;
  cursor: pointer;
  padding: 0;
  text-align: left;
}

.avatar {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--sidebar-avatar-bg);
  color: #cdd3d0;
  display: grid;
  place-items: center;
  font-size: 13px;
  font-weight: 600;
  flex: none;
}

.account-copy {
  flex: 1;
  min-width: 0;
}

.account-name {
  display: block;
  font-size: 13px;
  color: var(--sidebar-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-context {
  display: block;
  font-size: 11px;
  color: var(--sidebar-text-faint);
  margin-top: 1px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-caret {
  color: var(--sidebar-text-faint);
}

.account-menu {
  margin-top: 10px;
}

.account-menu button {
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #f0b7ae;
  font-size: 13px;
  cursor: pointer;
}

.account-menu button:hover {
  background: var(--sidebar-item-hover-bg);
}

.dev-note {
  margin: 0;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--sidebar-text-faint);
}

.workspace {
  flex: 1;
  min-width: 0;
}

.workspace:not(.full-bleed) {
  padding: 30px 36px 56px;
  max-width: var(--content-max);
}

/* 窄屏折叠：设计三栏宽度不作为唯一布局（计划 §8）。 */
@media (max-width: 900px) {
  .shell {
    flex-direction: column;
  }

  .sidebar {
    width: 100%;
    height: auto;
    position: static;
  }

  .workspace:not(.full-bleed) {
    padding: 20px 16px 40px;
  }
}
</style>
