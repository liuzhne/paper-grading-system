// @ts-check
import { defineStore } from "pinia";
import { ref } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { mergeConfirmedDrafts } from "@/lib/ai-draft.js";

/**
 * 评分标准：列表、导入、克隆（v3 §3.1、§5.1）。
 *
 * **只能由导入产生**（D-026）：不提供空白新建。副作用是好的——每份标准都带模板
 * 溯源，`publish` 里「legacy draft 必须先显式升级溯源」那条分支在新界面上不再
 * 可能触发。想要一份相似的标准就克隆已有的。
 */
export const useRubricsStore = defineStore("rubrics", () => {
  /** @type {import('vue').Ref<any[]>} */
  const rubrics = ref([]);
  /** @type {import('vue').Ref<string|null>} */
  const error = ref(null);
  const loading = ref(false);
  /** 解析完成、尚未创建正式 Rubric 的数据库临时会话。 */
  /** @type {import('vue').Ref<any|null>} */
  const activeImportSession = ref(null);

  /**
   * 最近一次导入的结果。
   *
   * `warnings` 与 `templateSummary` 是「你的 Excel 里哪几条没被识别」的唯一出口。
   * 吞掉它们，用户会以为全都导进去了，直到评分时才发现某个评分项没有判据。
   */
  const lastImport = ref({ warnings: [], templateSummary: null, rubricId: null, coverage: null, triggers: [], conflicts: [] });

  function reset() {
    rubrics.value = [];
    error.value = null;
    lastImport.value = { warnings: [], templateSummary: null, rubricId: null, coverage: null, triggers: [], conflicts: [] };
    activeImportSession.value = null;
    lastDraft.value = { items: [] };
    draftTasks.value = {};
    classificationTask.value = null;
    ruleReviewTask.value = null;
    structureTask.value = null;
    importStructureTask.value = null;
  }

  async function load() {
    loading.value = true;
    error.value = null;
    try {
      rubrics.value = (await api.get("/rubrics")) || [];
    } catch (err) {
      if (!(err instanceof StaleContextError)) {
        rubrics.value = [];
        error.value = err instanceof Error ? err.message : "加载评分标准失败";
      }
    } finally {
      loading.value = false;
    }
  }

  /**
   * 导入评分标准：Word 评分标准文档与 Excel 评分表至少一份（解析重构方案 §4.2）。
   * 组合处理由后端决定：仅 Word 时 Word 为规则文档；两者都有时 Excel 为结构主干。
   *
   * @param {{name: string, version: string, description?: string, visibility?: string,
   *          rulesFile?: File|null, templateFile?: File|null, structureOverride?: object|null}} input
   */
  async function importFiles(input) {
    if (!input.rulesFile && !input.templateFile) {
      throw new Error("请至少上传一份评分标准文件（Word 或 Excel）。");
    }
    const form = new FormData();
    form.append("name", input.name);
    form.append("version", input.version);
    if (input.description) form.append("description", input.description);
    // D-026/V3-c：默认仅自己可见。扩大范围是发布时的显式动作，不在导入时顺手做掉。
    form.append("visibility", input.visibility || "private");
    if (input.rulesFile) form.append("rules_file", input.rulesFile);
    // 模板是可选的。不传时**不要**append 一个空值——后端按「有没有这个字段」
    // 判断要不要解析批注。
    if (input.templateFile) form.append("template_file", input.templateFile);
    // 用户确认过的表格结构（E1 等识别失败时由 AI 建议）：由后端确定性解析。
    if (input.structureOverride) form.append("structure_override", JSON.stringify(input.structureOverride));

    const result = await api.post("/rubrics/import-files", undefined, {
      formData: form,
    });
    lastImport.value = {
      warnings: result.warnings || [],
      templateSummary: result.template_summary || null,
      rubricId: result.rubric?.id || null,
      coverage: result.coverage || null,
      triggers: result.triggers || [],
      conflicts: result.conflicts || [],
    };
    return result;
  }

  /**
   * 第一步确定性解析。只创建临时会话，确认前不得产生 Rubric。
   * @param {{name: string, version: string, description?: string, visibility?: string,
   *          rulesFile?: File|null, templateFile?: File|null, structureOverride?: object|null}} input
   */
  async function createImportSession(input) {
    if (!input.rulesFile && !input.templateFile) {
      throw new Error("请至少上传一份评分标准文件（Word 或 Excel）。");
    }
    const form = new FormData();
    form.append("name", input.name);
    form.append("version", input.version);
    if (input.description) form.append("description", input.description);
    form.append("visibility", input.visibility || "private");
    if (input.rulesFile) form.append("rules_file", input.rulesFile);
    if (input.templateFile) form.append("template_file", input.templateFile);
    if (input.structureOverride) form.append("structure_override", JSON.stringify(input.structureOverride));
    const result = await api.post("/rubrics/import-sessions", undefined, { formData: form });
    activeImportSession.value = result;
    lastImport.value = {
      warnings: result.warnings || [],
      templateSummary: result.template_summary || null,
      rubricId: null,
      coverage: result.coverage || null,
      triggers: [],
      conflicts: result.conflicts || [],
    };
    return result;
  }

  /**
   * 按 ID 打开一个仍在草稿状态的导入会话。评分助手在对话里上传规则与模板后，
   * 工作区里的评分标准页用它接着核对（会话存在数据库里，不依赖内存）。
   * @param {string} sessionId
   */
  async function loadImportSession(sessionId) {
    const result = await api.get(`/rubrics/import-sessions/${sessionId}`);
    if (result?.status !== "draft") return null;
    activeImportSession.value = result;
    lastImport.value = {
      warnings: result.warnings || [],
      templateSummary: result.template_summary || null,
      rubricId: null,
      coverage: result.coverage || null,
      triggers: [],
      conflicts: result.conflicts || [],
    };
    return result;
  }

  /** @param {Record<string, unknown>} changes */
  async function updateImportSession(changes) {
    if (!activeImportSession.value?.id) throw new Error("当前没有待确认的导入会话。");
    const result = await api.patch(`/rubrics/import-sessions/${activeImportSession.value.id}`, {
      expected_state_version: activeImportSession.value.state_version,
      ...changes,
    });
    activeImportSession.value = result;
    return result;
  }

  /** @param {string} idempotencyKey */
  async function confirmImportSession(idempotencyKey) {
    const current = activeImportSession.value;
    if (!current?.id) throw new Error("当前没有待确认的导入会话。");
    const result = await api.post(`/rubrics/import-sessions/${current.id}/confirm`, {
      expected_state_version: current.state_version,
      idempotency_key: idempotencyKey,
    });
    lastImport.value = { ...lastImport.value, rubricId: result.rubric?.id || null };
    activeImportSession.value = null;
    return result;
  }

  /** @param {"word"|"excel"} document */
  async function previewImportSource(document) {
    const current = activeImportSession.value;
    if (!current?.id) throw new Error("当前没有待确认的导入会话。");
    return api.get(`/rubrics/import-sessions/${current.id}/source-preview?document=${document}`);
  }

  /** @param {{rulesFile?: File|null, templateFile?: File|null}} input */
  async function previewImportReupload(input) {
    const current = activeImportSession.value;
    if (!current?.id) throw new Error("当前没有待确认的导入会话。");
    if (!input.rulesFile && !input.templateFile) throw new Error("请选择要重新上传的文件。");
    const form = new FormData();
    form.append("expected_state_version", String(current.state_version));
    if (input.rulesFile) form.append("rules_file", input.rulesFile);
    if (input.templateFile) form.append("template_file", input.templateFile);
    return api.post(`/rubrics/import-sessions/${current.id}/reupload-preview`, undefined, { formData: form });
  }

  /** @param {{fingerprint: string, rulesFile?: File|null, templateFile?: File|null}} input */
  async function confirmImportReupload(input) {
    const current = activeImportSession.value;
    if (!current?.id) throw new Error("当前没有待确认的导入会话。");
    const form = new FormData();
    form.append("expected_state_version", String(current.state_version));
    form.append("fingerprint", input.fingerprint);
    if (input.rulesFile) form.append("rules_file", input.rulesFile);
    if (input.templateFile) form.append("template_file", input.templateFile);
    const result = await api.post(`/rubrics/import-sessions/${current.id}/reupload-confirm`, undefined, { formData: form });
    activeImportSession.value = result;
    return result;
  }

  /** @param {string} conflictId @param {"use_excel"|"use_word"} decision @param {string} reason */
  async function resolveImportConflict(conflictId, decision, reason) {
    const current = activeImportSession.value;
    if (!current?.id) throw new Error("当前没有待确认的导入会话。");
    const result = await api.post(
      `/rubrics/import-sessions/${current.id}/conflicts/${encodeURIComponent(conflictId)}/resolve`,
      { expected_state_version: current.state_version, decision, reason },
    );
    activeImportSession.value = result;
    return result;
  }

  async function cancelImportSession() {
    const current = activeImportSession.value;
    if (!current?.id) throw new Error("当前没有待确认的导入会话。");
    const result = await api.post(`/rubrics/import-sessions/${current.id}/cancel`, {
      expected_state_version: current.state_version,
    });
    activeImportSession.value = null;
    return result;
  }

  /** @param {string} rubricId @param {{rulesFile?: File|null, templateFile?: File|null}} input */
  async function previewRubricReupload(rubricId, input) {
    if (!input.rulesFile && !input.templateFile) throw new Error("请选择要重新上传的文件。");
    const form = new FormData();
    if (input.rulesFile) form.append("rules_file", input.rulesFile);
    if (input.templateFile) form.append("template_file", input.templateFile);
    return api.post(`/rubrics/${rubricId}/reupload-preview`, undefined, { formData: form });
  }

  /** @param {string} rubricId @param {{fingerprint: string, rulesFile?: File|null, templateFile?: File|null}} input */
  async function confirmRubricReupload(rubricId, input) {
    const form = new FormData();
    form.append("fingerprint", input.fingerprint);
    if (input.rulesFile) form.append("rules_file", input.rulesFile);
    if (input.templateFile) form.append("template_file", input.templateFile);
    return api.post(`/rubrics/${rubricId}/reupload-confirm`, undefined, { formData: form });
  }

  /** 调用模型的操作都要先选定自己的 AI 连接（D-027）；估算不调用模型，不需要。 @param {string|null|undefined} connectionId */
  function requireConnection(connectionId) {
    if (!connectionId) throw new Error("请先选择用于识别的 AI 连接。");
  }

  /** @param {{rulesFile?: File|null, templateFile?: File|null}} input */
  function uploadForm(input) {
    const form = new FormData();
    if (input.rulesFile) form.append("rules_file", input.rulesFile);
    if (input.templateFile) form.append("template_file", input.templateFile);
    return form;
  }

  /**
   * 导入前结构预检（E1/E7）的规模估算：不调用模型。
   * @param {{rulesFile?: File|null, templateFile?: File|null}} input
   */
  async function estimateImportStructure(input) {
    return api.post("/rubrics/import-files/structure-suggestions/estimate", undefined, { formData: uploadForm(input) });
  }

  /**
   * 最近一次导入前结构识别任务（还没有评分标准，只对自己可见）。
   * @type {import('vue').Ref<any>}
   */
  const importStructureTask = ref(null);

  /**
   * 确认后提交导入前结构识别任务：提交即返回，结果（结构与将导入的评分项）在任务里。
   * @param {{rulesFile?: File|null, templateFile?: File|null, connectionId?: string|null}} input
   */
  async function startImportStructure(input) {
    requireConnection(input.connectionId);
    const form = uploadForm(input);
    if (input.connectionId) form.append("ai_connection_id", input.connectionId);
    importStructureTask.value = await api.post("/ai-tasks/import-structure", undefined, { formData: form });
    return importStructureTask.value;
  }

  /** @param {string} rubricId */
  async function loadParseCoverage(rubricId) {
    return api.get(`/rubrics/${rubricId}/parse-coverage`);
  }

  /**
   * 人工处理未认领单元：指派到已有评分项，或确认不是规则（单条也走批量接口）。
   * @param {string} rubricId
   * @param {{unitIds: string[], action: "assign"|"not_rule", reason: string, criterionCode?: string|null}} input
   */
  async function resolveUnits(rubricId, input) {
    if (input.action === "assign" && !input.criterionCode) throw new Error("请选择评分项。");
    /** @type {Record<string, unknown>} */
    const body = { unit_ids: input.unitIds, action: input.action, reason: input.reason };
    if (input.action === "assign") body.criterion_code = input.criterionCode;
    return api.post(`/rubrics/${rubricId}/units/resolve-batch`, body);
  }

  /**
   * 最近一次 AI 归类任务（B 阶段）：后台每 3 个单元一批执行，批次成功即合进建议。
   * @type {import('vue').Ref<any>}
   */
  const classificationTask = ref(null);

  /**
   * 兜底分类器：结果只是建议，采纳仍走 resolveUnits。提交即返回任务，并发由后端按
   * 连接的同时请求数控制（前端不再自己分批调度）。
   * @param {string} rubricId
   * @param {{unitIds?: string[]|null, connectionId: string|null, rejudge?: boolean}} input
   */
  async function classifyUnits(rubricId, input) {
    requireConnection(input.connectionId);
    const task = await api.post(`/rubrics/${rubricId}/ai-tasks`, {
      kind: "unit_classification",
      params: { ...(input.unitIds ? { unit_ids: input.unitIds } : {}), rejudge: Boolean(input.rejudge) },
      ai_connection_id: input.connectionId,
      // 显式勾选重新判断：作废同内容的旧结果，否则同指纹直接复用。
      regenerate: Boolean(input.rejudge),
    });
    classificationTask.value = task;
    return task;
  }

  /** 刷新当前归类任务。 */
  async function refreshClassificationTask() {
    const task = classificationTask.value;
    if (!task) return null;
    const fresh = await api.get(`/ai-tasks/${task.id}`);
    if (classificationTask.value?.id === task.id) classificationTask.value = fresh;
    return fresh;
  }

  /** 进入评分标准页时找回进行中的归类任务。 @param {string} rubricId */
  async function resumeClassificationTask(rubricId) {
    const tasks = (await api.get(`/rubrics/${rubricId}/ai-tasks?kind=unit_classification&active=1`)) || [];
    classificationTask.value = tasks[0] || null;
    return classificationTask.value;
  }

  /** @param {"cancel"|"retry"} action */
  async function classificationTaskAction(action) {
    const task = classificationTask.value;
    if (!task) return null;
    classificationTask.value = action === "cancel"
      ? await api.post(`/ai-tasks/${task.id}/cancel`, {})
      : await api.post(`/ai-tasks/${task.id}/retry`, {});
    return classificationTask.value;
  }

  /**
   * 最近一次草稿结构识别任务（C 阶段）：成功时建议已写进草稿，重新读取解析台账即可看到差异。
   * @type {import('vue').Ref<any>}
   */
  const structureTask = ref(null);

  /** 草稿结构建议的规模估算：不调用模型。 @param {string} rubricId */
  async function estimateStructure(rubricId) {
    return api.post(`/rubrics/${rubricId}/structure-suggestions/estimate`, {});
  }

  /**
   * 草稿结构建议（抽取器兜底），确认估算后提交任务。
   * @param {string} rubricId
   * @param {{connectionId: string|null, regenerate?: boolean}} input
   */
  async function startStructureSuggestion(rubricId, input) {
    requireConnection(input.connectionId);
    structureTask.value = await api.post(`/rubrics/${rubricId}/ai-tasks`, {
      kind: "structure_suggestion",
      params: { target: "draft" },
      ai_connection_id: input.connectionId,
      regenerate: Boolean(input.regenerate),
    });
    return structureTask.value;
  }

  /**
   * @param {string} rubricId
   * @param {{fingerprint: string, confirm: string[], exclude: string[], reason: string}} input
   */
  async function mergeStructure(rubricId, input) {
    return api.post(`/rubrics/${rubricId}/suggestions/merge`, {
      fingerprint: input.fingerprint, confirm: input.confirm, exclude: input.exclude, reason: input.reason,
    });
  }

  /** @param {string} rubricId @param {string} reason */
  async function undoStructure(rubricId, reason) {
    return api.post(`/rubrics/${rubricId}/suggestions/undo`, { reason });
  }

  /**
   * 最近一次规则审查任务：每个评分项一个条目，全部结束后写进草稿。
   * @type {import('vue').Ref<any>}
   */
  const ruleReviewTask = ref(null);

  /**
   * 规则审查的代码前置检查与规模估算：不调用模型。
   * @param {string} rubricId
   * @param {{scope: "priority"|"all"}} input
   */
  async function estimateRuleReview(rubricId, input) {
    return api.post(`/rubrics/${rubricId}/rule-review/estimate`, { scope: input.scope });
  }

  /**
   * 规则审查（第二部分结束后）：只报告问题，不修改规则；可跳过，发布时留痕。
   * 规则没变时同内容的任务直接复用；`regenerate` 表示用户明确要重新审查一遍。
   * @param {string} rubricId
   * @param {{connectionId: string|null, scope: "priority"|"all", regenerate?: boolean}} input
   */
  async function startRuleReview(rubricId, input) {
    requireConnection(input.connectionId);
    ruleReviewTask.value = await api.post(`/rubrics/${rubricId}/ai-tasks`, {
      kind: "rule_review",
      params: { scope: input.scope },
      ai_connection_id: input.connectionId,
      regenerate: Boolean(input.regenerate),
    });
    return ruleReviewTask.value;
  }

  /** @param {"review"|"structure"|"import"} key */
  function trackedTask(key) {
    return key === "review" ? ruleReviewTask : key === "structure" ? structureTask : importStructureTask;
  }

  /** 刷新正在跟踪的审查 / 结构识别任务。 @param {"review"|"structure"|"import"} key */
  async function refreshAiTask(key) {
    const holder = trackedTask(key);
    const task = holder.value;
    if (!task) return null;
    const fresh = await api.get(`/ai-tasks/${task.id}`);
    if (holder.value?.id === task.id) holder.value = fresh;
    return fresh;
  }

  /** @param {"review"|"structure"|"import"} key @param {"cancel"|"retry"} action */
  async function aiTaskAction(key, action) {
    const holder = trackedTask(key);
    const task = holder.value;
    if (!task) return null;
    holder.value = action === "cancel"
      ? await api.post(`/ai-tasks/${task.id}/cancel`, {})
      : await api.post(`/ai-tasks/${task.id}/retry`, {});
    return holder.value;
  }

  /**
   * 进入评分标准页时找回进行中的审查与结构识别任务（刷新或关页面不影响执行）。
   * @param {string} rubricId
   */
  async function resumeReviewTasks(rubricId) {
    const tasks = (await api.get(`/rubrics/${rubricId}/ai-tasks?active=1`)) || [];
    ruleReviewTask.value = tasks.find((/** @type {any} */ task) => task.kind === "rule_review") || null;
    structureTask.value = tasks.find((/** @type {any} */ task) => task.kind === "structure_suggestion") || null;
    return { review: ruleReviewTask.value, structure: structureTask.value };
  }

  /** @param {string} rubricId */
  async function loadRuleReview(rubricId) {
    return api.get(`/rubrics/${rubricId}/rule-review`);
  }

  /** @param {string} rubricId @param {string} findingId @param {string} reason */
  async function dismissFinding(rubricId, findingId, reason) {
    if (!reason?.trim()) throw new Error("请填写豁免原因。");
    return api.post(`/rubrics/${rubricId}/rule-review/findings/${encodeURIComponent(findingId)}/dismiss`, { reason });
  }

  /**
   * 克隆一份已有标准作为新草稿——替代「空白新建」的那条路径。
   *
   * @param {string} rubricId
   * @param {{name: string, version: string}} input
   */
  async function clone(rubricId, input) {
    const { name, version } = input;
    return api.post(`/rubrics/${rubricId}/clone`, { name, new_version: version });
  }

  /**
   * 发布：一次选定编译产物与分享范围（D-029）。
   *
   * **不提供「用最新的」快捷方式**：用户必须看到自己发布的是哪一份编译产物。
   * 范围不选时不发送该字段，由服务端沿用当前值——扩大范围是显式动作。
   *
   * @param {string} rubricId
   * @param {{compilationId: string|null, visibility: string|null, reason?: string}} input
   */
  async function publish(rubricId, input) {
    if (!input.compilationId) {
      throw new Error("请先选择要发布的编译产物。");
    }
    /** @type {Record<string, unknown>} */
    const body = { compilation_id: input.compilationId };
    if (input.reason) body.reason = input.reason;
    if (input.visibility) body.visibility = input.visibility;
    return api.post(`/rubrics/${rubricId}/publish`, body);
  }

  /** 最近一次 AI 起草的结果。起草只是建议，确认之前不改变任何已发布内容。 */
  /** @type {import('vue').Ref<{items: any[]}>} */
  const lastDraft = ref({ items: [] });

  /**
   * 起草任务（方案 A2）：按评分项编号记录进行中或刚结束的任务。刷新页面后由
   * `resumeDraftTasks` 找回进行中的；关页面不影响后台继续执行。
   * @type {import('vue').Ref<Record<string, any>>}
   */
  const draftTasks = ref({});

  /**
   * 记下任务的最新状态；成功时把结果放进待确认的起草建议（每个任务只放一次）。
   * @param {any} task
   */
  function rememberDraftTask(task) {
    const code = task?.scope?.criterion_code;
    if (!code) return;
    const previous = draftTasks.value[code];
    draftTasks.value = { ...draftTasks.value, [code]: task };
    const justFinished = task.status === "succeeded" && task.result &&
      !(previous?.id === task.id && previous?.status === "succeeded");
    if (justFinished) {
      const collection = lastDraft.value;
      collection.items = [...collection.items.filter((item) => item.criterion_code !== code), task.result];
    }
  }

  /** @param {any} task */
  function isActiveTask(task) {
    return task?.status === "queued" || task?.status === "running";
  }

  /**
   * AI 起草缺失的扣分细则（D-027）：为每个评分项建一个后台任务，立即返回。
   *
   * **必须带上用户自己的连接**：留空会走平台默认，而平台是 mock 时得到的是编出来
   * 的规则，却以「AI 起草 · 待确认」呈现——确认之后它们进入正式发布的评分标准。
   *
   * 同样的内容重复提交（双击、多个标签页）拿到同一个任务；`regenerate` 作废旧结果重新生成。
   *
   * @param {string} rubricId
   * @param {{criteria: any[], connectionId: string|null, regenerate?: boolean}} input
   */
  async function draftRules(rubricId, input) {
    if (!input.connectionId) {
      throw new Error("请先选择用于起草的 AI 连接。");
    }
    const collection = lastDraft.value;
    const started = [];
    for (const criterion of input.criteria) {
      try {
        const task = await api.post(`/rubrics/${rubricId}/ai-tasks`, {
          kind: "rule_draft",
          params: { criterion },
          ai_connection_id: input.connectionId,
          regenerate: Boolean(input.regenerate),
        });
        if (lastDraft.value !== collection) throw new StaleContextError();
        rememberDraftTask(task);
        started.push(task);
      } catch (err) {
        if (err instanceof StaleContextError) throw err;
        const message = err instanceof Error ? err.message : "起草失败";
        throw new Error(`评分项 ${criterion.code} 起草任务提交失败；已提交 ${started.length} 项。${message}`);
      }
    }
    return started;
  }

  /** 刷新所有进行中的起草任务（由页面轮询调用，页面隐藏时暂停）。 */
  async function refreshDraftTasks() {
    const active = Object.values(draftTasks.value).filter(isActiveTask);
    for (const task of active) {
      rememberDraftTask(await api.get(`/ai-tasks/${task.id}`));
    }
    return Object.values(draftTasks.value).some(isActiveTask);
  }

  /** 进入评分标准页时找回进行中的起草任务。 @param {string} rubricId */
  async function resumeDraftTasks(rubricId) {
    const tasks = (await api.get(`/rubrics/${rubricId}/ai-tasks?kind=rule_draft&active=1`)) || [];
    for (const task of tasks) rememberDraftTask(task);
    return tasks;
  }

  /** @param {string} taskId */
  async function cancelDraftTask(taskId) {
    rememberDraftTask(await api.post(`/ai-tasks/${taskId}/cancel`, {}));
  }

  /** 只重试失败的批次，已成功的保留。 @param {string} taskId */
  async function retryDraftTask(taskId) {
    rememberDraftTask(await api.post(`/ai-tasks/${taskId}/retry`, {}));
  }

  function forgetDraftTasks() {
    draftTasks.value = {};
  }

  /**
   * 执行草稿：编译产物、阻断项与歧义。三步详情的第 2、3 步都读它。
   *
   * @param {string} rubricId
   */
  async function loadExecutionDraft(rubricId) {
    return api.get(`/rubrics/${rubricId}/execution-draft`);
  }

  /**
   * 单个评分标准的完整内容，含每个评分项的扣分规则。
   *
   * 执行草稿是一份**安全读模型**，按设计不带规则正文；要把确认后的规则提交回去
   * 就必须从这里拿到完整的 criteria。
   *
   * @param {string} rubricId
   */
  async function loadRubric(rubricId) {
    return api.get(`/rubrics/${rubricId}`);
  }

  /**
   * 把确认后的 AI 起草规则写进可执行版本（V3-2 闭环）。
   *
   * 起草端点 non-persistent，落库唯一的路径是 `recompile`——它接收完整 criteria
   * 并生成新的执行草稿。`PATCH /rubrics/{id}` 在已有编译产物时会直接 409
   * （`RUBRIC_RECOMPILE_REQUIRED`），走不通。
   *
   * @param {string} rubricId
   * @param {{criteria: any[], items: any[], excluded: Set<string>,
   *          supersedesCompilationId: string|null, version: string,
   *          name?: string|null, totalScore?: number|null}} input
   */
  async function applyDraftRules(rubricId, input) {
    if (!input.supersedesCompilationId) {
      // 缺它 recompile 必然 409（RUBRIC_RECOMPILE_STALE）。在这里失败，错误信息
      // 才说得清是「页面上的执行草稿没选中」，而不是后端抛来的并发冲突。
      throw new Error("当前没有可继承的执行草稿，请刷新后重试。");
    }
    const criteria = mergeConfirmedDrafts(input.criteria, input.items, input.excluded);
    const changed = criteria.some(
      (criterion, index) => criterion !== input.criteria[index],
    );
    if (!changed) {
      throw new Error("没有可应用的规则：这批建议已被全部排除。");
    }

    const result = await api.post(`/rubrics/${rubricId}/recompile`, {
      supersedes_compilation_id: input.supersedesCompilationId,
      version: input.version,
      ...(input.name ? { name: input.name } : {}),
      ...(input.totalScore ? { total_score: input.totalScore } : {}),
      criteria,
      reason: "确认并应用 AI 起草的扣分细则",
    });
    // 同一批建议不该被应用两次：第二次会在新草稿上再叠一遍同样的规则。
    lastDraft.value = { items: [] };
    return result;
  }

  return {
    rubrics,
    error,
    loading,
    activeImportSession,
    lastImport,
    reset,
    load,
    importFiles,
    createImportSession,
    loadImportSession,
    updateImportSession,
    confirmImportSession,
    previewImportSource,
    previewImportReupload,
    confirmImportReupload,
    resolveImportConflict,
    cancelImportSession,
    previewRubricReupload,
    confirmRubricReupload,
    estimateImportStructure,
    importStructureTask,
    startImportStructure,
    loadParseCoverage,
    resolveUnits,
    classifyUnits,
    classificationTask,
    refreshClassificationTask,
    resumeClassificationTask,
    classificationTaskAction,
    structureTask,
    estimateStructure,
    startStructureSuggestion,
    mergeStructure,
    undoStructure,
    ruleReviewTask,
    estimateRuleReview,
    startRuleReview,
    refreshAiTask,
    aiTaskAction,
    resumeReviewTasks,
    loadRuleReview,
    dismissFinding,
    clone,
    publish,
    loadExecutionDraft,
    loadRubric,
    lastDraft,
    draftTasks,
    draftRules,
    refreshDraftTasks,
    resumeDraftTasks,
    cancelDraftTask,
    retryDraftTask,
    forgetDraftTasks,
    applyDraftRules,
  };
});
