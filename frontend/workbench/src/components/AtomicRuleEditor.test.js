import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import AtomicRuleEditor from './AtomicRuleEditor.vue';

it('单次旧上限可显式清除，累计封顶输入仍可编辑；清除不改扣分或命中方式', async () => {
  const rule = { id: 'r', rule_code: 'T01.r1', changes: {
    name:'合成规则', rule_text:'合成条件', direction:'deduct', effect_type:'score',
    repeat_policy:'once', max_points:2, cap_points:6, levels:[],
  } };
  const wrapper = mount(AtomicRuleEditor, { props: { rules: [rule] } });
  const cap = wrapper.findAll('label').find(l => l.text().includes('单条累计上限')).find('input');
  expect(cap.element.disabled).toBe(true);
  const clear = wrapper.findAll('button').find(b => b.text() === '清除不适用的单条上限');
  await clear.trigger('click');
  expect(rule.changes.cap_points).toBeNull();
  expect(rule.changes.max_points).toBe(2);
  expect(rule.changes.repeat_policy).toBe('once');
  const repeat = wrapper.findAll('label').find(l => l.text().includes('重复命中方式')).find('select');
  await repeat.setValue('capped');
  expect(cap.element.disabled).toBe(false);
  await cap.setValue(6);
  expect(rule.changes.cap_points).toBe(6);
});

it('语义规则可选判定范围，写入 evidence_policy.judge_scope；选“自动”即删除，不动其它证据要求', async () => {
  const rule = { id: 'r', rule_code: 'T01.r1',
    judge_scope: { scope: 'cross_section', targets: ['绪论', '结论'], source: 'wording' },
    judge_scope_hint: null,
    changes: { name:'研究问题与结论呼应', rule_text:'绪论的研究问题在结论中没有回应', direction:'deduct', effect_type:'score',
      judge_type:'semantic', repeat_policy:'once', max_points:2, levels:[],
      evidence_policy: { mode:'source_quote', requirement:'required', minimum_coverage:'1' } } };
  const wrapper = mount(AtomicRuleEditor, { props: { rules: [rule] } });
  const field = wrapper.findAll('label').find(l => l.text().includes('判定范围'));
  expect(field.text()).toContain('跨章节（绪论、结论）');
  expect(field.text()).toContain('按规则文字');
  await field.find('select').setValue('section');
  expect(rule.changes.evidence_policy).toEqual({ mode:'source_quote', requirement:'required', minimum_coverage:'1', judge_scope:'section' });
  await field.find('select').setValue('');
  expect(rule.changes.evidence_policy).toEqual({ mode:'source_quote', requirement:'required', minimum_coverage:'1' });
});

it('确定性规则不显示判定范围，缺失类提示照原文显示', () => {
  const deterministic = { id: 'd', rule_code: 'T01.d1', changes: { name:'必备章节', rule_text:'缺少结论章', direction:'deduct',
    effect_type:'score', judge_type:'deterministic', levels:[] } };
  const semantic = { id: 's', rule_code: 'T01.s1', judge_scope: { scope: 'document', targets: [], source: 'default' },
    judge_scope_hint: '这条规则问的是某一章有没有，交给结构检查器（必备章节）更可靠；语义判断只能看到部分段落。',
    changes: { name:'缺少结论章', rule_text:'论文没有结论', direction:'deduct', effect_type:'score', judge_type:'semantic', levels:[] } };
  const wrapper = mount(AtomicRuleEditor, { props: { rules: [deterministic, semantic] } });
  const scopes = wrapper.findAll('label').filter(l => l.text().startsWith('判定范围'));
  expect(scopes).toHaveLength(1);
  expect(scopes[0].text()).toContain('交给结构检查器');
});
