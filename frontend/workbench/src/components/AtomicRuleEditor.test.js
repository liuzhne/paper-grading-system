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
