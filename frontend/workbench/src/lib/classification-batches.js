/** At most three concurrent requests; drain in-flight work before returning on failure. */
export async function classifyInBatches(unitIds, { request, onResult, onProgress, isCurrent = () => true }) {
  const ids = [...new Set(unitIds)];
  let cursor = 0, completed = 0, active = 0, stopped = false, firstError = null;
  let newest = '';
  const progress = () => {
    if (isCurrent()) onProgress({ completed, total: ids.length, running: active > 0 || (!stopped && cursor < ids.length) });
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
        completed += batch.filter(id => returned.has(id)).length;
        if (batch.some(id => !returned.has(id))) stopped = true;
      } catch (error) {
        stopped = true;
        firstError ||= error;
      } finally {
        active--;
        progress();
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, Math.ceil(ids.length / 3)) }, worker));
  if (firstError) throw firstError;
}
