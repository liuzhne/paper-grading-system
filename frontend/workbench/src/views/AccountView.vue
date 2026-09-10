<script setup>
import { computed, onMounted, reactive, ref } from "vue";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { useAnchorHighlight } from "@/lib/anchor-highlight.js";
import { useSessionStore } from "@/stores/session.js";

const session = useSessionStore();

const ROLES = [
  { value: "member", label: "成员" },
  { value: "teacher", label: "教师" },
  { value: "org_admin", label: "组织管理员" },
];
const ROLE_LABELS = Object.fromEntries(ROLES.map((r) => [r.value, r.label]));

const canManageMembers = computed(() => session.can("manage_members"));
const orgId = computed(() => session.organizationId);

/* ---------------- 成员 ---------------- */

const members = ref([]);
const membersError = ref(null);
const membersLoading = ref(false);

const invite = reactive({ email: "", role: "member", busy: false, error: null });
/** 一次性机密：邀请链接与重置令牌只在创建响应里出现一次，之后无法取回。 */
const issued = ref(null);
const rowBusy = reactive({});

async function loadMembers() {
  if (!canManageMembers.value || !orgId.value) {
    members.value = [];
    return;
  }
  membersLoading.value = true;
  membersError.value = null;
  try {
    members.value = await api.get(`/organizations/${orgId.value}/members`);
  } catch (error) {
    if (error instanceof StaleContextError) return;
    members.value = [];
    membersError.value =
      error instanceof ApiError && error.status === 404
        ? "当前账号无权查看该组织的成员。"
        : error?.message || "加载成员失败";
  } finally {
    membersLoading.value = false;
  }
}

async function onInvite() {
  const organizationId = orgId.value;
  if (!organizationId) return;
  invite.busy = true;
  invite.error = null;
  issued.value = null;
  try {
    const result = await api.post(
      `/organizations/${organizationId}/members`,
      { email: invite.email.trim(), role: invite.role },
      { organizationId },
    );
    issued.value = {
      kind: "invitation",
      subject: result.email,
      value: result.invitation_token,
    };
    invite.email = "";
    await loadMembers();
  } catch (error) {
    if (error instanceof StaleContextError) return;
    invite.error = error?.message || "创建邀请失败";
  } finally {
    invite.busy = false;
  }
}

async function onChangeRole(member, event) {
  const organizationId = orgId.value;
  const nextRole = event.target.value;
  if (!organizationId || nextRole === member.role) return;
  rowBusy[member.user_id] = true;
  try {
    await api.patch(
      `/organizations/${organizationId}/members/${member.user_id}`,
      { role: nextRole },
      { organizationId },
    );
    member.role = nextRole;
  } catch (error) {
    if (!(error instanceof StaleContextError)) {
      membersError.value = error?.message || "更新角色失败";
      event.target.value = member.role;
    }
  } finally {
    rowBusy[member.user_id] = false;
  }
}

async function onIssueReset(member) {
  const organizationId = orgId.value;
  if (!organizationId) return;
  rowBusy[member.user_id] = true;
  issued.value = null;
  try {
    const result = await api.post(
      `/organizations/${organizationId}/members/${member.user_id}/password-reset-token`,
      {},
      { organizationId },
    );
    issued.value = {
      kind: "reset",
      subject: member.username,
      value: result.reset_token,
      expiresAt: result.expires_at,
    };
  } catch (error) {
    if (!(error instanceof StaleContextError)) {
      membersError.value = error?.message || "签发重置令牌失败";
    }
  } finally {
    rowBusy[member.user_id] = false;
  }
}

/* ---------------- 私有 AI 连接（BYOK） ---------------- */
const { highlighted: aiHighlighted } = useAnchorHighlight("ai-connections");


const connections = ref([]);
const connectionsError = ref(null);
const draft = reactive({
  name: "我的 AI 连接",
  provider_type: "openai_responses",
  base_url: "https://api.openai.com/v1",
  model_name: "gpt-4.1-mini",
  api_key: "",
  busy: false,
  error: null,
  result: null,
});
const connBusy = reactive({});
const rotating = ref(null);
const rotateKey = ref("");

async function loadConnections() {
  connectionsError.value = null;
  try {
    connections.value = await api.get("/ai-connections");
  } catch (error) {
    if (error instanceof StaleContextError) return;
    connections.value = [];
    connectionsError.value = error?.message || "加载 AI 连接失败";
  }
}

function draftPayload() {
  return {
    name: draft.name.trim(),
    provider_type: draft.provider_type,
    base_url: draft.base_url.trim(),
    model_name: draft.model_name.trim(),
    api_key: draft.api_key,
    provider_options: {},
  };
}

async function onTestDraft() {
  const organizationId = orgId.value;
  draft.busy = true;
  draft.error = null;
  draft.result = null;
  try {
    // 测试在服务端完成，浏览器不直接请求模型厂商。
    const probe = await api.post("/ai-connections/test-draft", draftPayload(), {
      organizationId,
    });
    draft.result = `配置校验通过：${probe.provider_type} · ${probe.model_name}`;
  } catch (error) {
    if (!(error instanceof StaleContextError)) {
      draft.error = error?.message || "测试失败";
    }
  } finally {
    draft.busy = false;
  }
}

async function onCreateConnection() {
  const organizationId = orgId.value;
  draft.busy = true;
  draft.error = null;
  draft.result = null;
  try {
    await api.post("/ai-connections", draftPayload(), { organizationId });
    // 明文 Key 用完即弃，不留在组件状态里。
    draft.api_key = "";
    await loadConnections();
    // 绑好连接，`can_use_llm` 就该翻过来。不刷新的话用户配完了仍然进不去功能页，
    // 得自己按 F5——而页面上没有任何东西提示他要这么做。
    await session.loadCapabilities();
  } catch (error) {
    if (!(error instanceof StaleContextError)) {
      draft.error = error?.message || "保存连接失败";
    }
  } finally {
    draft.busy = false;
  }
}

async function connectionAction(connection, action) {
  const organizationId = orgId.value;
  connBusy[connection.id] = action;
  connectionsError.value = null;
  try {
    if (action === "test") {
      await api.post(`/ai-connections/${connection.id}/test`, {}, { organizationId });
    } else if (action === "disable") {
      await api.post(`/ai-connections/${connection.id}/disable`, {}, { organizationId });
    } else if (action === "delete") {
      await api.del(`/ai-connections/${connection.id}`, { organizationId });
    }
    await loadConnections();
    // 停用或删掉最后一个连接，`can_use_llm` 会翻回 false。不刷新的话前端仍以为
    // 可用，放人进功能页——然后每个动作在后端失败。**反向也要刷。**
    await session.loadCapabilities();
  } catch (error) {
    if (!(error instanceof StaleContextError)) {
      connectionsError.value = error?.message || "操作失败";
    }
  } finally {
    connBusy[connection.id] = null;
  }
}

async function onRotate(connection) {
  const organizationId = orgId.value;
  connBusy[connection.id] = "rotate";
  connectionsError.value = null;
  try {
    await api.post(
      `/ai-connections/${connection.id}/rotate-key`,
      { api_key: rotateKey.value },
      { organizationId },
    );
    rotateKey.value = "";
    rotating.value = null;
    await loadConnections();
  } catch (error) {
    if (!(error instanceof StaleContextError)) {
      connectionsError.value = error?.message || "换 Key 失败";
    }
  } finally {
    connBusy[connection.id] = null;
  }
}

/* ---------------- 通用 ---------------- */

const copyState = ref(null);

async function copySecret(value) {
  try {
    await navigator.clipboard.writeText(value);
    copyState.value = "copied";
  } catch {
    // 非安全上下文或权限被拒；提示用户手动复制，不降级到任何日志输出。
    copyState.value = "failed";
  }
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

function statusChip(status) {
  if (status === "active") return "chip chip-ok";
  if (status === "disabled") return "chip chip-warn";
  return "chip";
}

onMounted(async () => {
  await Promise.all([loadMembers(), loadConnections()]);
});
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">账户与组织</p>
      <h1 class="page-title">账户与连接</h1>
      <p class="page-sub">
        在当前组织内管理成员与私有 AI 连接。密钥仅通过 HTTPS 发往本系统后端，浏览器不会直接请求模型厂商。
      </p>
    </header>

    <!-- 当前身份 -->
    <section class="card card-pad">
      <h2 class="card-title">当前身份</h2>
      <dl class="identity">
        <div>
          <dt>账号</dt>
          <dd>{{ session.identity?.user?.display_name || session.identity?.user?.username || "开发模式" }}</dd>
        </div>
        <div>
          <dt>当前组织</dt>
          <dd>{{ session.organization?.name || "无组织上下文" }}</dd>
        </div>
        <div>
          <dt>组织角色</dt>
          <dd>{{ ROLE_LABELS[session.organizationRole] || "—" }}</dd>
        </div>
        <div>
          <dt>平台角色</dt>
          <dd class="mono">{{ session.platformRole || "—" }}</dd>
        </div>
      </dl>
      <p v-if="!session.authEnforced" class="notice notice-warn identity-note">
        当前部署未启用鉴权（<span class="mono">AUTH_ENABLED=false</span>），这是显式开发模式，不能作为生产权限证明。
      </p>
    </section>

    <!-- 一次性机密 -->
    <section v-if="issued" class="notice notice-warn" role="status">
      <p class="notice-title">
        {{ issued.kind === "invitation" ? "邀请令牌已创建" : "重置令牌已签发" }}
      </p>
      <p>
        面向 <strong>{{ issued.subject }}</strong>。
        <template v-if="issued.expiresAt">有效期至 {{ formatTime(issued.expiresAt) }}。</template>
        <strong>此令牌只显示这一次</strong>，请立即通过可信渠道转交；离开本页后无法再取回。
      </p>
      <div class="secret">
        <code>{{ issued.value }}</code>
        <button class="btn btn-sm" type="button" @click="copySecret(issued.value)">复制</button>
        <button class="btn btn-sm" type="button" @click="issued = null">隐藏</button>
      </div>
      <p v-if="copyState === 'copied'" class="field-hint">已复制到剪贴板。</p>
      <p v-else-if="copyState === 'failed'" class="field-hint">
        当前环境不允许自动复制，请手动选中上方文本复制。
      </p>
    </section>

    <!-- 组织成员 -->
    <section v-if="canManageMembers" class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">组织成员</h2>
          <p class="card-note">调整角色或为成员签发一次性密码重置令牌。本系统不代发邮件。</p>
        </div>
        <span class="chip">{{ members.length }} 人</span>
      </div>

      <p v-if="membersError" class="notice notice-danger" role="alert">{{ membersError }}</p>

      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>成员</th>
              <th>邮箱</th>
              <th>组织角色</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="member in members" :key="member.user_id">
              <td>
                <div>{{ member.display_name || member.username }}</div>
                <div class="faint mono member-username">{{ member.username }}</div>
              </td>
              <td class="muted">{{ member.email || "—" }}</td>
              <td>
                <select
                  class="select role-select"
                  :value="member.role"
                  :disabled="rowBusy[member.user_id]"
                  :aria-label="`修改 ${member.username} 的组织角色`"
                  @change="onChangeRole(member, $event)"
                >
                  <option v-for="role in ROLES" :key="role.value" :value="role.value">
                    {{ role.label }}
                  </option>
                </select>
              </td>
              <td>
                <button
                  class="btn btn-sm"
                  type="button"
                  :disabled="rowBusy[member.user_id]"
                  @click="onIssueReset(member)"
                >
                  签发重置令牌
                </button>
              </td>
            </tr>
            <tr v-if="!members.length && !membersLoading">
              <td class="table-empty" colspan="4">当前组织还没有成员记录。</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card-foot">
        <form class="invite-form" @submit.prevent="onInvite">
          <label class="field invite-email">
            <span class="field-label">邀请邮箱</span>
            <input v-model="invite.email" class="input" type="email" required autocomplete="email" />
          </label>
          <label class="field">
            <span class="field-label">组织角色</span>
            <select v-model="invite.role" class="select">
              <option v-for="role in ROLES" :key="role.value" :value="role.value">
                {{ role.label }}
              </option>
            </select>
          </label>
          <button class="btn btn-primary invite-submit" type="submit" :disabled="invite.busy">
            {{ invite.busy ? "创建中…" : "创建邀请链接" }}
          </button>
        </form>
        <p v-if="invite.error" class="notice notice-danger" role="alert">{{ invite.error }}</p>
      </div>
    </section>

    <!-- 私有 AI 连接。带 id 与高亮：未配置模型的守卫会把用户直接送到这里。 -->
    <section id="ai-connections" class="card" :class="{ highlight: aiHighlighted }">
      <div class="card-head">
        <div>
          <h2 class="card-title">我的 AI 连接</h2>
          <p class="card-note">
            保存、测试、换 Key、停用与删除都在服务端完成；页面只展示掩码与状态，任何时候都不会回显完整密钥。
          </p>
        </div>
        <span class="chip">{{ connections.length }} 个</span>
      </div>

      <p v-if="connectionsError" class="notice notice-danger" role="alert">{{ connectionsError }}</p>

      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>模型</th>
              <th>密钥</th>
              <th>状态</th>
              <th>最近验证</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="conn in connections" :key="conn.id">
              <tr>
                <td>
                  <div>{{ conn.name }}</div>
                  <div class="faint mono conn-base">{{ conn.base_url }}</div>
                </td>
                <td class="muted">{{ conn.model_name }}</td>
                <td class="num">{{ conn.key_masked }}<span class="faint"> · v{{ conn.key_version }}</span></td>
                <td>
                  <span :class="statusChip(conn.status)">{{ conn.status }}</span>
                  <div v-if="conn.last_error_code" class="faint mono conn-base">{{ conn.last_error_code }}</div>
                </td>
                <td class="num muted">{{ formatTime(conn.last_verified_at) }}</td>
                <td>
                  <div class="btn-row">
                    <button
                      class="btn btn-sm"
                      type="button"
                      :disabled="connBusy[conn.id]"
                      @click="connectionAction(conn, 'test')"
                    >
                      测试
                    </button>
                    <button
                      class="btn btn-sm"
                      type="button"
                      :disabled="connBusy[conn.id]"
                      @click="rotating = rotating === conn.id ? null : conn.id"
                    >
                      换 Key
                    </button>
                    <button
                      v-if="conn.status === 'active'"
                      class="btn btn-sm"
                      type="button"
                      :disabled="connBusy[conn.id]"
                      @click="connectionAction(conn, 'disable')"
                    >
                      停用
                    </button>
                    <button
                      class="btn btn-sm btn-danger"
                      type="button"
                      :disabled="connBusy[conn.id]"
                      @click="connectionAction(conn, 'delete')"
                    >
                      删除
                    </button>
                  </div>
                </td>
              </tr>
              <tr v-if="rotating === conn.id">
                <td colspan="6">
                  <form class="rotate-form" @submit.prevent="onRotate(conn)">
                    <label class="field rotate-field">
                      <span class="field-label">新的 API Key</span>
                      <input
                        v-model="rotateKey"
                        class="input"
                        type="password"
                        autocomplete="off"
                        required
                      />
                      <span class="field-hint">提交后立即加密存储，页面不保留明文。</span>
                    </label>
                    <button class="btn btn-primary" type="submit" :disabled="connBusy[conn.id]">
                      确认更换
                    </button>
                  </form>
                </td>
              </tr>
            </template>
            <tr v-if="!connections.length">
              <td class="table-empty" colspan="6">还没有保存任何私有连接。</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card-foot">
        <form @submit.prevent="onCreateConnection">
          <div class="form-grid">
            <label class="field">
              <span class="field-label">名称</span>
              <input v-model="draft.name" class="input" type="text" required />
            </label>
            <label class="field">
              <span class="field-label">厂商协议</span>
              <select v-model="draft.provider_type" class="select">
                <option value="openai_responses">OpenAI Responses</option>
                <option value="openai_compatible">OpenAI 兼容</option>
              </select>
            </label>
            <label class="field">
              <span class="field-label">模型</span>
              <input v-model="draft.model_name" class="input" type="text" required />
            </label>
            <label class="field">
              <span class="field-label">HTTPS 接口地址</span>
              <input v-model="draft.base_url" class="input" type="url" required />
            </label>
            <label class="field">
              <span class="field-label">API Key</span>
              <input v-model="draft.api_key" class="input" type="password" autocomplete="off" required />
            </label>
          </div>

          <div class="form-actions">
            <span class="muted">测试与保存都在服务端完成，浏览器不直接调用厂商。</span>
            <span class="btn-row">
              <button class="btn" type="button" :disabled="draft.busy" @click="onTestDraft">
                测试配置
              </button>
              <button class="btn btn-primary" type="submit" :disabled="draft.busy">
                加密保存连接
              </button>
            </span>
          </div>
        </form>

        <p v-if="draft.error" class="notice notice-danger" role="alert">{{ draft.error }}</p>
        <p v-else-if="draft.result" class="notice" role="status">{{ draft.result }}</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.identity {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 18px;
  margin: 16px 0 0;
}

.identity dt {
  font-size: 12.5px;
  color: var(--text-muted);
  margin-bottom: 6px;
}

.identity dd {
  margin: 0;
  font-size: 14px;
  font-weight: 550;
}

.identity-note {
  margin: 18px 0 0;
}

.member-username,
.conn-base {
  font-size: 11.5px;
  margin-top: 3px;
}

.role-select {
  width: auto;
  min-width: 130px;
  height: 32px;
}

.invite-form {
  display: flex;
  align-items: flex-end;
  gap: 14px;
  flex-wrap: wrap;
}

.invite-form .field {
  margin-bottom: 0;
}

.invite-email {
  flex: 1 1 240px;
}

.invite-submit {
  height: 40px;
}

.rotate-form {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  flex-wrap: wrap;
}

.rotate-field {
  flex: 1 1 260px;
  margin-bottom: 0;
}
</style>
