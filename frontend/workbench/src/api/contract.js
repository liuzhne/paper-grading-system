// @ts-check
/**
 * 前后端接口契约版本（版本守卫，方案第 5 节）。
 *
 * 后端每个响应带 `X-PGS-Contract`。两边不一致说明这个页面是旧版本：在下一次写操作或
 * 路由切换之前静默刷新，旧页面就不会去调已停用的接口。
 *
 * **必须与 backend/app/core/contract.py 的 API_CONTRACT_VERSION 一致**（后端测试校验）。
 */
export const API_CONTRACT_VERSION = "2026-10-10.ai-tasks-c";
export const API_CONTRACT_HEADER = "X-PGS-Contract";
