// @ts-check
import { defineStore } from "pinia";
import { ref } from "vue";

import { api, StaleContextError } from "@/api/client.js";

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

  /**
   * 最近一次导入的结果。
   *
   * `warnings` 与 `templateSummary` 是「你的 Excel 里哪几条没被识别」的唯一出口。
   * 吞掉它们，用户会以为全都导进去了，直到评分时才发现某个评分项没有判据。
   */
  const lastImport = ref({ warnings: [], templateSummary: null, rubricId: null });

  function reset() {
    rubrics.value = [];
    error.value = null;
    lastImport.value = { warnings: [], templateSummary: null, rubricId: null };
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
   * 导入规则 Excel（可带 Word 批注模板）。
   *
   * @param {{name: string, version: string, description?: string,
   *          visibility?: string, rulesFile: File, templateFile?: File|null}} input
   */
  async function importFiles(input) {
    const form = new FormData();
    form.append("name", input.name);
    form.append("version", input.version);
    if (input.description) form.append("description", input.description);
    // D-026/V3-c：默认仅自己可见。扩大范围是发布时的显式动作，不在导入时顺手做掉。
    form.append("visibility", input.visibility || "private");
    form.append("rules_file", input.rulesFile);
    // 模板是可选的。不传时**不要**append 一个空值——后端按「有没有这个字段」
    // 判断要不要解析批注。
    if (input.templateFile) form.append("template_file", input.templateFile);

    const result = await api.post("/rubrics/import-files", undefined, {
      formData: form,
    });
    lastImport.value = {
      warnings: result.warnings || [],
      templateSummary: result.template_summary || null,
      rubricId: result.rubric?.id || null,
    };
    return result;
  }

  /**
   * 克隆一份已有标准作为新草稿——替代「空白新建」的那条路径。
   *
   * @param {string} rubricId
   * @param {{name: string, version: string}} input
   */
  async function clone(rubricId, input) {
    const { name, version } = input;
    return api.post(`/rubrics/${rubricId}/clone`, { name, version });
  }

  return { rubrics, error, loading, lastImport, reset, load, importFiles, clone };
});
