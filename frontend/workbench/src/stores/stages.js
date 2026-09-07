/**
 * 批次业务阶段的展示口径（前端 v2 计划 §5-C）。
 *
 * 顺序即评审流程顺序，图例按此排列。`code` 与后端
 * `services/batches/state.BATCH_STAGES` 一一对应——界面同时显示中文标签与
 * 内部编码，便于对照后端状态与工单沟通。
 *
 * **阶段不表达任务故障。** 一次 job 失败不代表批次阶段就是失败，进度条与
 * 状态标签必须并列显示 job 状态，见 progress 端点。
 */
export const STAGES = {
  draft: { label: "草稿", tone: "neutral", hint: "已建批次，尚未上传或未开始" },
  parsing: { label: "解析中", tone: "active", hint: "正在提取正文与篇章结构" },
  scoring: { label: "评分中", tone: "active", hint: "按已发布标准逐份评分" },
  scored: { label: "已评分", tone: "ok", hint: "全部材料评分成功" },
  scored_with_errors: {
    label: "已评分 · 含异常项",
    tone: "warn",
    hint: "部分材料失败或置信度过低",
  },
  reviewed: { label: "已复核", tone: "neutral", hint: "人工确认完成，可导出" },
  archived: { label: "已归档", tone: "neutral", hint: "成绩已发布，只读" },
};

/** 未知阶段原样回显：后端新增取值时界面降级而不是崩掉或伪装成已知阶段。 */
export function stageLabel(code) {
  return STAGES[code]?.label ?? code;
}

export function stageTone(code) {
  return STAGES[code]?.tone ?? "neutral";
}

export function stageHint(code) {
  return STAGES[code]?.hint ?? "";
}

export const STAGE_ORDER = Object.keys(STAGES);
