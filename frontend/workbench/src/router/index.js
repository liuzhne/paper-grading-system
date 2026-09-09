import { createRouter, createWebHistory } from "vue-router";

import { useSessionStore } from "@/stores/session.js";
import { requiresModelSetup } from "@/router/llm-gate.js";

/**
 * 新页面统一挂在 /workbench/ 下（计划 §8.3）。
 * 未交付页面暂为占位视图；导航可见性由服务端能力表决定，
 * 前端路由守卫只做未登录跳转，真正的权限由服务端执行。
 */
const routes = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/views/LoginView.vue"),
    meta: { public: true, title: "登录" },
  },
  // 邮件里已经发出去的链接指向这两条路由。旧壳下线后由工作台承接，不接就等于
  // 把所有在途邀请与重置作废，而收件人只会看到 404。
  {
    path: "/register",
    name: "register",
    component: () => import("@/views/RegisterView.vue"),
    meta: { public: true, title: "接受邀请" },
  },
  {
    path: "/reset-password",
    name: "reset-password",
    component: () => import("@/views/ResetPasswordView.vue"),
    meta: { public: true, title: "重置密码" },
  },
  {
    path: "/",
    component: () => import("@/layouts/AppShell.vue"),
    children: [
      {
        path: "",
        name: "dashboard",
        component: () => import("@/views/DashboardView.vue"),
        meta: { title: "工作台", nav: "home" },
      },
      {
        path: "tasks",
        name: "tasks",
        component: () => import("@/views/TasksView.vue"),
        meta: { title: "评分任务", nav: "tasks" },
      },
      {
        path: "tasks/new",
        name: "task-new",
        component: () => import("@/views/NewTaskView.vue"),
        meta: { title: "新建评分任务", nav: "tasks" },
      },
      {
        path: "batches/:batchId/grade",
        name: "grade",
        component: () => import("@/views/GradeView.vue"),
        meta: { title: "评分工作区", nav: "tasks", chrome: "full" },
      },
      {
        path: "review",
        name: "review",
        component: () => import("@/views/ReviewView.vue"),
        meta: { title: "结果复核", nav: "review" },
      },
      {
        path: "rubrics",
        name: "rubrics",
        component: () => import("@/views/RubricsView.vue"),
        meta: { title: "评分标准", nav: "rubric" },
      },
      {
        path: "exports",
        name: "exports",
        component: () => import("@/views/ExportsView.vue"),
        meta: { title: "输出中心", nav: "export" },
      },
      {
        path: "account",
        name: "account",
        component: () => import("@/views/AccountView.vue"),
        meta: { title: "账户与连接", nav: "account" },
      },
      {
        path: "ops",
        name: "ops",
        component: () => import("@/views/OpsView.vue"),
        meta: { title: "运维", nav: "ops" },
      },
    ],
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: () => import("@/views/NotFoundView.vue"),
    meta: { public: true, title: "页面不存在" },
  },
];

export const router = createRouter({
  // base 与 vite.config.js 的 base 一致；两个宿主（Vercel / FastAPI）
  // 都需要把 /workbench/* 的深链接回落到本 index.html（计划 §8.2）。
  history: createWebHistory("/workbench/"),
  routes,
});

router.beforeEach(async (to) => {
  const session = useSessionStore();
  if (session.status === "unknown") {
    await session.bootstrap();
  }
  if (to.meta.public) return true;
  if (session.status !== "authenticated") {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  // 没有任何可用模型时先去配置，别让人上传完材料再撞上失败（D-027、D-028）。
  // 账户与连接页、运维页不拦——那正是解开这件事的地方。
  if (requiresModelSetup(session, to)) {
    return { name: "account", query: { setup: "model" } };
  }
  return true;
});

router.afterEach((to) => {
  const title = to.meta.title;
  document.title = title ? `${title} · 有据智评` : "有据智评";
});
