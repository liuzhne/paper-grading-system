import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import SourceReviewPanel from './SourceReviewPanel.vue';
const units = [{ unit_id:'docx:p[1]', text:'每处扣1分', blocking:true, signals:['verb'], suspected:true }, { unit_id:'docx:p[2]', text:'摘要要求', blocking:false, suspected:true }];
const state = { coverage: { unclaimed:units, documents:[] }, unit_classifications:{stale:false, results:[{unit_id:'docx:p[1]', label:'rule', confidence:'high', suggested_criterion:'C1', reason:'有扣分要求'}]} };
function setup(extra = {}) { return mount(SourceReviewPanel, {props:{state, previews:units.map(u => ({...u, locator:{section_path:['摘要']}})), criteria:[{code:'C1',name:'规范'}], editable:true, connection:{name:'启用连接',model_name:'test'}, ...extra}}); }
describe('原文识别双栏校对', () => {
  it('选择段落后显示完整上下文，归类使用真实编号', async () => {
    const w=setup();
    expect(w.find('.context-panel').text()).toContain('每处扣1分');
    expect(w.findAll('.paragraph select')).toHaveLength(0);
    await w.find('.criterion-buttons button').trigger('click');
    expect(w.emitted('resolve')[0][0]).toMatchObject({unitIds:['docx:p[1]'],criterionCode:'C1',action:'assign'});
    await w.setProps({state:{...state,coverage:{...state.coverage,unclaimed:units.slice(1)}}});
    expect(w.find('.context-panel h3').text()).toContain('docx:p[2]');
  });
  it('批量只处理选中单元，切换筛选不会误提交隐藏条目', async () => {
    const w=setup();
    await w.find('input[type=checkbox]').setValue(true);
    await w.get('[data-test=batch-not-rule]').trigger('click');
    expect(w.emitted('resolve')[0][0].unitIds).toEqual(['docx:p[1]']);
  });
  it('键盘操作限定在工作区，输入控件和只读状态不能修改', async () => {
    const w=setup();
    await w.trigger('keydown',{key:'1'});
    expect(w.emitted('resolve')).toHaveLength(1);
    await w.find('input').trigger('keydown',{key:'x'});
    expect(w.emitted('resolve')).toHaveLength(1);
    await w.setProps({editable:false});
    await w.trigger('keydown',{key:'x'});
    expect(w.emitted('resolve')).toHaveLength(1);
    await w.trigger('keydown',{key:'j'});
    expect(w.find('.context-panel h3').text()).toContain('docx:p[2]');
  });
  it('过期建议、无效评分项和低置信建议不可批量采纳', async () => {
    const w=setup();
    expect(w.text()).toContain('采纳高置信 AI 建议');
    await w.setProps({state:{...state,unit_classifications:{...state.unit_classifications,stale:true}}});
    expect(w.text()).not.toContain('采纳高置信 AI 建议');
    await w.setProps({state,criteria:[{code:'OTHER'}]});
    expect(w.text()).not.toContain('采纳高置信 AI 建议');
  });
  it('已处理状态从服务端来源台账读取，刷新后仍可核对', async () => {
    const w=setup({previews:[{unit_id:'done',text:'已人工排除',locator:{review:{status:'context',extracted_by:'human',reason:'已核对'}}}]});
    await w.findAll('[role=tab]')[3].trigger('click');
    expect(w.find('.paragraph-list').text()).toContain('已人工排除');
  });
});

it('AI 归类只提交选中单元，失败显示原因与重试范围', async () => {
  const w=setup();
  await w.find('input[type=checkbox]').setValue(true);
  await w.get('[data-test=classify]').trigger('click');
  expect(w.emitted('classify')[0][0].unitIds).toEqual(['docx:p[1]']);
  await w.setProps({state:{...state,unit_classifications:{results:[],failed_unit_ids:['docx:p[1]'],rejected:[{unit_id:'docx:p[1]',error:'output_truncated'}]}}});
  expect(w.text()).toContain('模型输出达到长度上限');
  await w.find('.classification-result button').trigger('click');
  expect(w.emitted('classify')[1][0].unitIds).toEqual(['docx:p[1]']);
});

it('批量采纳只作用于当前筛选或显式选择范围', async () => {
  const w = setup({state:{...state,unit_classifications:{results:units.map(u => ({unit_id:u.unit_id,label:'rule',confidence:'high',suggested_criterion:'C1'}))}}});
  await w.findAll('[role=tab]')[1].trigger('click');
  await w.findAll('button').find(b => b.text() === '采纳高置信 AI 建议').trigger('click');
  expect(w.emitted('accept-suggestions')[0][0].map(a => a.unitIds[0])).toEqual(['docx:p[1]']);
  await w.findAll('[role=tab]')[0].trigger('click');
  await w.findAll('input[type=checkbox]')[1].setValue(true);
  await w.findAll('button').find(b => b.text() === '采纳高置信 AI 建议').trigger('click');
  expect(w.emitted('accept-suggestions')[1][0].map(a => a.unitIds[0])).toEqual(['docx:p[2]']);
});

it('显示超时原因与逐批进度', () => {
  const w = setup({progress:{completed:3,total:44,running:false},state:{...state,unit_classifications:{results:[],rejected:[{error:'request_timeout'}]}}});
  expect(w.text()).toContain('3 / 44');
  expect(w.text()).toContain('模型响应超时');
  expect(w.text()).toContain('已完成的建议已保留');
});

it('列表与对照区都能直接采纳 AI 建议，失效评分项和已处理内容不可采纳', async () => {
  const results = [
    { unit_id:'docx:p[1]', label:'rule', confidence:'high', suggested_criterion:'C1', reason:'有扣分要求' },
    { unit_id:'docx:p[2]', label:'noise', confidence:'low', suggested_criterion:null, reason:'示例正文' },
  ];
  const w = setup({ state:{ ...state, unit_classifications:{ stale:false, results } } });
  expect(w.get('[data-test="ai-line-docx:p[1]"]').text()).toContain('AI：归入 C1 规范');
  expect(w.get('[data-test="ai-line-docx:p[1]"]').text()).toContain('高置信');
  expect(w.get('[data-test="ai-line-docx:p[2]"]').classes()).toContain('tentative');
  await w.get('[data-test="ai-line-docx:p[2]"] button').trigger('click');
  expect(w.emitted('resolve')[0][0]).toEqual({ unitIds:['docx:p[2]'], action:'not_rule', reason:'用户采纳 AI 归类建议' });
  await w.get('[data-test=ai-card] button').trigger('click');
  expect(w.emitted('resolve')[1][0]).toEqual({ unitIds:['docx:p[1]'], action:'assign', criterionCode:'C1', reason:'用户采纳 AI 归类建议' });
  await w.setProps({ criteria:[{ code:'OTHER', name:'其他' }] });
  expect(w.get('[data-test="ai-line-docx:p[1]"]').text()).toContain('未匹配到现有评分项');
  expect(w.find('[data-test="ai-line-docx:p[1]"] button').exists()).toBe(false);
  expect(w.find('[data-test=ai-card] button').exists()).toBe(false);
  await w.setProps({ editable:false, criteria:[{ code:'C1', name:'规范' }] });
  expect(w.get('[data-test=ai-card] button').attributes('disabled')).toBeDefined();
  await w.setProps({ state:{ ...state, unit_classifications:{ stale:true, results } } });
  expect(w.find('[data-test=ai-card]').exists()).toBe(false);
  expect(w.find('[data-test="ai-line-docx:p[1]"]').exists()).toBe(false);
});

it('父组件可以通过 v-model:filter 直接打开疑似规则筛选', async () => {
  const w = setup({ filter:'blocking', 'onUpdate:filter': value => w.setProps({ filter:value }) });
  expect(w.findAll('.paragraph-row')).toHaveLength(1);
  expect(w.get('[role=tab][aria-selected=true]').text()).toBe('疑似规则');
  await w.findAll('[role=tab]')[0].trigger('click');
  expect(w.props('filter')).toBe('pending');
  expect(w.findAll('.paragraph-row')).toHaveLength(2);
});

it('未勾选时跳过已有有效建议的单元，作为「继续剩余」入口；勾选后照勾选重新判断', async () => {
  const w = setup();
  const button = w.get('[data-test=classify]');
  expect(button.text()).toBe('继续为剩余 1 条给出归类建议');
  expect(w.get('[data-test=classify-skipped]').text()).toContain('已有建议的 1 条不再重复调用');
  await button.trigger('click');
  expect(w.emitted('classify')[0][0].unitIds).toEqual(['docx:p[2]']);
  // 续跑不重新判断：后端同样会剔除已有有效建议的单元。
  expect(w.emitted('classify')[0][0].rejudge).toBe(false);
  await w.find('input[type=checkbox]').setValue(true);
  expect(w.get('[data-test=classify]').text()).toBe('用 AI 给出归类建议（1 条）');
  expect(w.find('[data-test=classify-skipped]').exists()).toBe(false);
  await w.get('[data-test=classify]').trigger('click');
  expect(w.emitted('classify')[1][0].unitIds).toEqual(['docx:p[1]']);
  expect(w.emitted('classify')[1][0].rejudge).toBe(true);
  // 建议过期后不再算「已有建议」，全部重新送出。
  await w.find('input[type=checkbox]').setValue(false);
  await w.setProps({ state:{ ...state, unit_classifications:{ ...state.unit_classifications, stale:true } } });
  expect(w.get('[data-test=classify]').text()).toBe('用 AI 给出归类建议（2 条）');
});

it('全部单元都已有建议时，未勾选不能再次整轮调用', () => {
  const results = units.map(u => ({ unit_id:u.unit_id, label:'requirement', confidence:'medium', suggested_criterion:'C1', reason:'要求' }));
  const w = setup({ state:{ ...state, unit_classifications:{ stale:false, results } } });
  expect(w.get('[data-test=classify]').text()).toBe('均已有 AI 建议');
  expect(w.get('[data-test=classify]').attributes('disabled')).toBeDefined();
});

it('进度来自后台归类任务：运行中、失败给出根因、停止与完成各自说明', async () => {
  const w = setup({ progress:{ completed:9, failed:3, total:60, running:true, stopped:null } });
  expect(w.text()).toContain('关闭页面不会中断');
  expect(w.text()).toContain('已有 3 条未获得建议');
  expect(w.find('[data-test=classify]').attributes('disabled')).toBeDefined();
  await w.setProps({ progress:{ completed:57, failed:3, total:60, running:false, stopped:null } });
  expect(w.text()).toContain('本轮处理完成，3 条未获得建议，可重试失败内容');
  await w.setProps({ progress:{ completed:15, failed:3, total:60, running:false, stopped:'provider', error:'模型额度已用完。' } });
  expect(w.text()).toContain('本轮未全部完成：模型额度已用完。');
  expect(w.text()).toContain('重试失败的批次');
  await w.setProps({ progress:{ completed:15, failed:0, total:60, running:false, stopped:'canceled' } });
  expect(w.text()).toContain('本轮已停止；已完成的建议已保留');
  await w.setProps({ progress:{ completed:60, failed:0, total:60, running:false, stopped:null } });
  expect(w.text()).toContain('本轮处理完成。');
});
