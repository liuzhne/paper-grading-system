/**
 * 内容级失败：这一批的模型输出不合格（不是 JSON、截断、字段不合法），换一批多半正常，
 * 不代表连接或厂商出了问题。后端仍以 200 返回，失败单元列在 failed / rejected 里。
 */
export const CONTENT_FAILURES = new Set([
  'invalid_json', 'invalid_output', 'invalid_envelope', 'incomplete_output', 'output_truncated', 'error_envelope',
  'empty_content', 'refused',
  'invalid_item', 'unknown_unit', 'duplicate_unit', 'invalid_label', 'invalid_confidence', 'unknown_criterion', 'missing_reason',
]);
/** 连续这么多批都是内容级失败，就当模型整体不可用，停止派发以免空耗额度。 */
export const CONTENT_FAILURE_STREAK_LIMIT = 2;

/**
 * At most three concurrent requests of three units each; `concurrency` lowers that
 * to the connection's declared limit (providers enforce concurrency per key).
 * - 单批内容级失败：记入 failed，其它批次继续；
 * - 系统级失败（请求抛错，或回包里出现限流、鉴权、超时、熔断等非内容错误码）或连续内容级失败：
 *   停止派发新批次，等在途请求结束后返回；请求抛错时最后抛出第一个错误。
 * 返回 { completed, failed, total, stopped }，stopped 为 null | 'repeated' | 'provider' | 'request'。
 */
export async function classifyInBatches(unitIds, { request, onResult, onProgress, isCurrent = () => true, concurrency = 3 }) {
  const ids = [...new Set(unitIds)];
  let cursor = 0, completed = 0, failed = 0, active = 0, streak = 0;
  /** @type {null | 'repeated' | 'provider' | 'request'} */
  let stopped = null;
  let firstError = null;
  let newest = '';
  const stop = (/** @type {'repeated' | 'provider' | 'request'} */ reason) => { stopped ||= reason; };
  const progress = () => {
    if (isCurrent()) onProgress({ completed, failed, total: ids.length, running: active > 0 || (!stopped && cursor < ids.length), stopped });
  };
  async function worker() {
    while (!stopped && isCurrent() && cursor < ids.length) {
      const batch = ids.slice(cursor, cursor + 3);
      cursor += 3;
      active++;
      progress();
      try {
        const result = await request(batch);
        if (!isCurrent()) return;
        // Responses may arrive in a different order from database commits.
        if (!result.created_at || result.created_at >= newest) {
          newest = result.created_at || newest;
          onResult(result);
        }
        const returned = new Set((result.results || []).map(item => item.unit_id));
        const missing = new Set(batch.filter(id => !returned.has(id)));
        completed += batch.length - missing.size;
        failed += missing.size;
        if (!missing.size) {
          streak = 0;
        } else {
          // 回包里的 rejected 是累计视图，只看本批缺失单元的错误码；没有错误码的缺失单元按内容级处理。
          const codes = (result.rejected || []).filter(item => missing.has(item.unit_id)).map(item => item.error);
          if (codes.some(code => !CONTENT_FAILURES.has(code))) stop('provider');
          else if (++streak >= CONTENT_FAILURE_STREAK_LIMIT) stop('repeated');
        }
      } catch (error) {
        stop('request');
        firstError ||= error;
      } finally {
        active--;
        progress();
      }
    }
  }
  const lanes = Math.max(1, Math.min(3, Math.floor(Number(concurrency)) || 3));
  await Promise.all(Array.from({ length: Math.min(lanes, Math.ceil(ids.length / 3)) }, worker));
  if (firstError) throw firstError;
  return { completed, failed, total: ids.length, stopped };
}
