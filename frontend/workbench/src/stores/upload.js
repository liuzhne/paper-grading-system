import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, request, ApiError, StaleContextError } from "@/api/client.js";

/**
 * 上传编排（前端 v2 计划 §5-E）。
 *
 * 两条最要紧的约束：
 *
 * 1. **逐文件隔离。** 每个文件独立走完上传 → 归档确认 → 解析，一个失败既不
 *    撤销已成功的文件，也不阻断后续文件。批量整体回滚会让用户重传全部。
 * 2. **不偷偷回落。** 直传未配置时必须报错。改走 `/papers/upload` 的函数转发
 *    把大文件塞进 Serverless 请求体，正是 FUNCTION_PAYLOAD_TOO_LARGE 的成因，
 *    而且失败得莫名其妙。
 *
 * 限制值一律读服务端能力表，不写死在前端——同一份代码要能跑在不同部署上。
 */
export const useUploadStore = defineStore("upload", () => {
  const provider = ref(null);
  const maxSizeMb = ref(null);
  const tusThresholdMb = ref(null);
  const acceptedExtensions = ref([]);
  const queue = ref([]);
  const uploadedPaperIds = ref([]);
  const busy = ref(false);
  const canceled = ref(false);

  const doneCount = computed(
    () => queue.value.filter((entry) => entry.status === "done").length,
  );
  const failedCount = computed(
    () => queue.value.filter((entry) => entry.status === "failed").length,
  );
  const pendingCount = computed(
    () => queue.value.filter((entry) => entry.status === "pending").length,
  );

  async function loadCapabilities() {
    try {
      const caps = await api.get("/system/capabilities");
      provider.value = caps.upload.provider;
      maxSizeMb.value = caps.upload.max_size_mb;
      tusThresholdMb.value = caps.upload.tus_threshold_mb;
      acceptedExtensions.value = caps.upload.accepted_extensions;
      // 能力表可能晚于用户选文件到达。不补判的话，那一批文件等于**没过浏览器侧
      // 预检**——一个 .txt 或一个超限大文件会一路走到上传才被服务端拒绝。
      recheckPending();
    } catch (err) {
      if (!(err instanceof StaleContextError)) provider.value = null;
    }
  }

  function extensionOf(name) {
    const at = name.lastIndexOf(".");
    return at === -1 ? "" : name.slice(at).toLowerCase();
  }

  /** 按当前已知的上传能力判定一个条目。能力未知时不下结论。 */
  function applyPrecheck(entry) {
    const extension = extensionOf(entry.name);
    if (
      acceptedExtensions.value.length &&
      !acceptedExtensions.value.includes(extension)
    ) {
      entry.status = "rejected";
      entry.error = `不支持的文件类型 ${extension || "（无扩展名）"}；仅接受 ${acceptedExtensions.value.join("、")}。`;
      return;
    }
    if (maxSizeMb.value && entry.size > maxSizeMb.value * 1024 * 1024) {
      entry.status = "rejected";
      entry.error = `文件超过 ${maxSizeMb.value} MB 上限。`;
    }
  }

  /** 能力表到达后补判**仍在等待**的条目。

   * 只碰 `pending`：已上传或已失败的条目不能因为一次迟到的能力表被改写。
   */
  function recheckPending() {
    for (const entry of queue.value) {
      if (entry.status === "pending") applyPrecheck(entry);
    }
  }

  /** 浏览器侧预检；服务端仍会按配置复核，这里只是尽早给出反馈。 */
  function stage(files, { sizeOverride = null } = {}) {
    const staged = Array.from(files).map((file) => {
      const size = sizeOverride ?? file.size;
      const entry = {
        id: `${file.name}-${size}-${Math.random().toString(16).slice(2, 8)}`,
        file,
        name: file.name,
        size,
        status: "pending",
        error: null,
        paperId: null,
      };
      applyPrecheck(entry);
      return entry;
    });
    queue.value = [...queue.value, ...staged];
    return staged;
  }

  async function uploadLocal(entry, batchId) {
    // batch_id 是 multipart 表单字段而非查询参数（papers.upload_paper 用
    // Form(...)）。写成 query 会以 422 missing batch_id 失败。
    const form = new FormData();
    form.append("batch_id", batchId);
    form.append("file", entry.file, entry.name);
    return request("/papers/upload", { method: "POST", formData: form });
  }

  async function uploadDirect(entry, batchId) {
    // 直传三段：预留对象路径 → 写入存储 → 归档确认。任何一段失败都不改走
    // 函数转发；调用方看到的是明确的存储错误，而不是一个体积超限的怪异失败。
    const intent = await api.post("/papers/direct-upload-intents", {
      batch_id: batchId,
      file_name: entry.name,
      content_type: entry.file.type || "application/octet-stream",
      byte_size: entry.size,
    });

    const put = await fetch(intent.signed_url, {
      method: "PUT",
      headers: { "content-type": entry.file.type || "application/octet-stream" },
      body: entry.file,
    });
    if (!put.ok) {
      throw new Error(`存储写入失败（HTTP ${put.status}）`);
    }

    return api.post(`/papers/${intent.paper.id}/complete-upload`, {
      byte_size: entry.size,
    });
  }

  async function uploadOne(entry, batchId) {
    entry.status = "uploading";
    entry.error = null;
    try {
      const paper =
        provider.value === "supabase"
          ? await uploadDirect(entry, batchId)
          : await uploadLocal(entry, batchId);
      entry.paperId = paper.id;
      entry.status = "done";
      if (!uploadedPaperIds.value.includes(paper.id)) {
        uploadedPaperIds.value = [...uploadedPaperIds.value, paper.id];
      }
    } catch (err) {
      if (err instanceof StaleContextError) throw err;
      entry.status = "failed";
      entry.error =
        err instanceof ApiError ? err.detail || err.message : err?.message || "上传失败";
    }
  }

  /**
   * 取消**调度**（计划 §5-E）。
   *
   * 停的是「还没开始的那些」。已经发出去的那一个仍会走完并记下结果——文件在
   * 服务端已经归档，谎称它没传成功只会让用户再传一次，产生重复对象。
   */
  function cancel() {
    canceled.value = true;
  }

  async function uploadAll(batchId) {
    busy.value = true;
    canceled.value = false;
    try {
      for (const entry of queue.value) {
        // 逐个串行：一个失败不阻断后面的文件，也不撤销前面的成功。
        if (entry.status === "pending" || entry.status === "failed") {
          await uploadOne(entry, batchId);
        }
        // 取消不是失败：剩下的保持 pending，重试入口不该把它们当成错误，
        // 再次开始时从没传的那个继续。
        if (canceled.value) break;
      }
    } finally {
      busy.value = false;
    }
  }

  /**
   * 按服务端文件状态恢复队列（计划 §5-E）。
   *
   * 刷新会丢掉内存里的队列，但材料已经在服务端归档了。不恢复就会让用户重新
   * 选一遍并重传——「已归档的文件不重传」是这条的重点。
   *
   * 失败时抛出而不是静默留一个空队列：空队列看起来就是「这个批次还没有材料」，
   * 用户会照着这个结论再传一次。
   */
  async function restoreFromServer(batchId) {
    const papers = (await api.get(`/papers?batch_id=${batchId}`)) || [];
    queue.value = papers.map((paper) => ({
      name: paper.file_name,
      size: null,
      // 服务端已有即已归档，标 done。
      status: "done",
      error: null,
      paperId: paper.id,
      file: null,
    }));
    uploadedPaperIds.value = papers.map((paper) => paper.id);
    return papers;
  }

  async function retryFailed(batchId) {
    busy.value = true;
    try {
      for (const entry of queue.value) {
        if (entry.status === "failed") await uploadOne(entry, batchId);
      }
    } finally {
      busy.value = false;
    }
  }

  function clearQueue() {
    // 只清界面队列；已归档材料的 ID 保留，后续预检与评分要用。
    queue.value = [];
  }

  function reset() {
    queue.value = [];
    uploadedPaperIds.value = [];
    canceled.value = false;
  }

  return {
    provider,
    maxSizeMb,
    tusThresholdMb,
    acceptedExtensions,
    queue,
    uploadedPaperIds,
    busy,
    canceled,
    doneCount,
    failedCount,
    pendingCount,
    loadCapabilities,
    stage,
    uploadAll,
    cancel,
    restoreFromServer,
    retryFailed,
    clearQueue,
    reset,
  };
});
