import { describe, expect, it, vi } from 'vitest';
import { classifyInBatches } from './classification-batches.js';
const success = ids => ({ results: ids.map(unit_id => ({ unit_id })) });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const ids = Array.from({length:13}, (_, i) => String(i));
describe('AI 归类有限并发', () => {
  it('最多同时三批，每批三条，完成一个立即补位，单元不重复', async () => {
    let active=0, peak=0;
    const sent=[], pending=[];
    const run=classifyInBatches(ids, {
      request: batch => { sent.push(batch); active++; peak=Math.max(peak,active); return new Promise(resolve => pending.push(() => {active--; resolve(success(batch));})); },
      onResult:vi.fn(), onProgress:vi.fn(),
    });
    expect(sent).toHaveLength(3);
    pending.shift()(); await tick();
    expect(sent).toHaveLength(4);
    while(pending.length) { pending.shift()(); await tick(); }
    await run;
    expect(peak).toBe(3);
    expect(sent.flat()).toEqual(ids);
    expect(sent.map(b => b.length)).toEqual([3,3,3,3,1]);
  });
  it('单批内容级失败只记入失败，其它批次继续，全部单元都会送出', async () => {
    const pending=[], onProgress=vi.fn(), onResult=vi.fn();
    const request=vi.fn(batch => new Promise(resolve => pending.push(result => resolve(result || success(batch)))));
    const run=classifyInBatches(ids,{request,onProgress,onResult});
    pending[0]({results:[],failed_unit_ids:['0','1','2'],rejected:['0','1','2'].map(unit_id => ({unit_id,error:'invalid_json'}))}); await tick();
    expect(request).toHaveBeenCalledTimes(4);
    for (let i = 1; i < 5; i++) { pending[i](); await tick(); }
    const summary = await run;
    expect(request.mock.calls.flatMap(([batch]) => batch)).toEqual(ids);
    expect(summary).toEqual({completed:10,failed:3,total:13,stopped:null});
    expect(onProgress).toHaveBeenLastCalledWith({completed:10,failed:3,total:13,running:false,stopped:null});
  });
  it('连续两批内容级失败视为模型不可用：停止新批次，但等待已发请求完成并保留成功结果', async () => {
    const pending=[], onProgress=vi.fn(), onResult=vi.fn();
    const bad = batch => ({results:[],failed_unit_ids:batch,rejected:batch.map(unit_id => ({unit_id,error:'invalid_json'}))});
    const request=vi.fn(batch => new Promise(resolve => pending.push(result => resolve(result || success(batch)))));
    const run=classifyInBatches(ids,{request,onProgress,onResult});
    pending[0](bad(['0','1','2'])); await tick();
    pending[1](bad(['3','4','5'])); await tick();
    expect(request).toHaveBeenCalledTimes(4);
    pending[2](); pending[3](); const summary = await run;
    expect(onResult).toHaveBeenCalledTimes(4);
    expect(summary).toEqual({completed:6,failed:6,total:13,stopped:'repeated'});
    expect(onProgress).toHaveBeenLastCalledWith({completed:6,failed:6,total:13,running:false,stopped:'repeated'});
  });
  it('成功批次清零连续失败计数', async () => {
    const bad = batch => ({results:[],failed_unit_ids:batch,rejected:batch.map(unit_id => ({unit_id,error:'output_truncated'}))});
    let call = 0;
    // 失败、成功交替：从未连续两批失败，所以全部送完。
    const summary = await classifyInBatches(ids,{request: async batch => (++call % 2 ? bad(batch) : success(batch)),onProgress:vi.fn(),onResult:vi.fn()});
    expect(call).toBe(5);
    expect(summary.stopped).toBeNull();
  });
  it('回包里出现限流、鉴权、熔断等系统级错误码时立即停止派发', async () => {
    const pending=[], onProgress=vi.fn();
    const request=vi.fn(batch => new Promise(resolve => pending.push(result => resolve(result || success(batch)))));
    const run=classifyInBatches(ids,{request,onProgress,onResult:vi.fn()});
    pending[0]({results:[],failed_unit_ids:['0','1','2'],rejected:[{unit_id:'0',error:'rate_limited'},{unit_id:'1',error:'rate_limited'},{unit_id:'2',error:'rate_limited'}]}); await tick();
    expect(request).toHaveBeenCalledTimes(3);
    pending[1](); pending[2](); const summary = await run;
    expect(summary).toEqual({completed:6,failed:3,total:13,stopped:'provider'});
  });
  it('累计视图里其它单元的旧错误码不影响本批判断', async () => {
    const request=vi.fn(async batch => batch[0] === '0'
      ? {results:[],failed_unit_ids:batch,rejected:[{unit_id:'old',error:'rate_limited'},...batch.map(unit_id => ({unit_id,error:'invalid_json'}))]}
      : success(batch));
    const summary = await classifyInBatches(ids,{request,onProgress:vi.fn(),onResult:vi.fn()});
    expect(request).toHaveBeenCalledTimes(5);
    expect(summary).toEqual({completed:10,failed:3,total:13,stopped:null});
  });
  it('HTTP 异常也等待在途请求，最终抛出错误', async () => {
    let finish;
    const error=new Error('HTTP 503'), onResult=vi.fn();
    const request=vi.fn().mockRejectedValueOnce(error).mockImplementationOnce(batch => new Promise(resolve => {finish=()=>resolve(success(batch));}));
    const run=classifyInBatches(ids.slice(0,6),{request,onResult,onProgress:vi.fn()});
    const assertion=expect(run).rejects.toThrow('HTTP 503');
    await tick(); finish(); await assertion;
    expect(onResult).toHaveBeenCalledTimes(1);
  });
  it('乱序响应不覆盖较新的累计快照', async () => {
    const pending=[], onResult=vi.fn();
    const run=classifyInBatches(ids.slice(0,6),{request:batch=>new Promise(resolve=>pending.push(time=>resolve({...success(batch),created_at:time}))),onResult,onProgress:vi.fn()});
    pending[1]('2026-09-29T10:00:02'); await tick();
    pending[0]('2026-09-29T10:00:01'); await run;
    expect(onResult).toHaveBeenCalledTimes(1);
  });
  it('上下文变化后停止发送并丢弃在途响应', async () => {
    let current=true;
    const pending=[], onResult=vi.fn();
    const request=vi.fn(batch=>new Promise(resolve=>pending.push(()=>resolve(success(batch)))));
    const run=classifyInBatches(ids,{request,onResult,onProgress:vi.fn(),isCurrent:()=>current});
    current=false; pending.forEach(done=>done()); await run;
    expect(request).toHaveBeenCalledTimes(3);
    expect(onResult).not.toHaveBeenCalled();
  });
});
