// @ts-check
/**
 * 后端合同的类型别名（前端 v2 计划 §8）。
 *
 * 类型**从 `schema.d.ts` 的端点路径取**，不手抄一份形状出来：手抄的副本在后端
 * 改了字段之后不会动，而那正是这套门禁要防的事。`schema.d.ts` 由
 * `npm run api:dump && npm run api:generate` 从真实应用的 OpenAPI 生成，CI 比对
 * 它的 diff。
 *
 * `jsconfig.json` 的 `checkJs` 是 false——历史文件还没准备好逐一过类型。所以
 * 想被校验的文件要自己写 `// @ts-check`，按接口逐步接入，而不是先把全仓库改完
 * 再开始（§8：「只为本次使用的接口补强 schema，不要求先改完所有历史端点」）。
 */

/** @typedef {import("./schema").paths} paths */
/** @typedef {import("./schema").components} components */

/**
 * 批次进度。阶段计数与完成比例都来自服务端的结果选择集合。
 * @typedef {paths["/api/batches/{batch_id}/progress"]["get"]["responses"][200]["content"]["application/json"]} BatchProgress
 */

/**
 * 批次列表项。
 * @typedef {paths["/api/batches"]["get"]["responses"][200]["content"]["application/json"][number]} Batch
 */

/**
 * 工作台 KPI。
 * @typedef {paths["/api/batches/overview"]["get"]["responses"][200]["content"]["application/json"]} BatchOverview
 */

/**
 * 分数分布。`bucketing` 恒为 null——分档口径是 §11 未决项。
 * @typedef {paths["/api/batches/{batch_id}/score-distribution"]["get"]["responses"][200]["content"]["application/json"]} ScoreDistribution
 */

export {};
