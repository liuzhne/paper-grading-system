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
    } catch (err) {
      if (!(err instanceof StaleContextError)) provider.value = null;
    }
  }

  function extensionOf(name) {
    const at = name.lastIndexOf(".");
    return at === -1 ? "" : name.slice(at).toLowerCase();
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
      const extension = extensionOf(file.name);
      if (acceptedExtensions.value.length && !acceptedExtensions.value.includes(extension)) {
        entry.status = "rejected";
        entry.error = `不支持的文件类型 ${extension || "（无扩展名）"}；仅接受 ${acceptedExtensions.value.join("、")}。`;
      } else if (maxSizeMb.value && size > maxSizeMb.value * 1024 * 1024) {
        entry.status = "rejected";
        entry.error = `文件超过 ${maxSizeMb.value} MB 上限。`;
      }
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

  async function uploadAll(batchId) {
    busy.value = true;
    try {
      for (const entry of queue.value) {
        // 逐个串行：一个失败不阻断后面的文件，也不撤销前面的成功。
        if (entry.status === "pending" || entry.status === "failed") {
          await uploadOne(entry, batchId);
        }
      }
    } finally {
      busy.value = false;
    }
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
  }

  return {
    provider,
    maxSizeMb,
    tusThresholdMb,
    acceptedExtensions,
    queue,
    uploadedPaperIds,
    busy,
    doneCount,
    failedCount,
    pendingCount,
    loadCapabilities,
    stage,
    uploadAll,
    retryFailed,
    clearQueue,
    reset,
  };
});
