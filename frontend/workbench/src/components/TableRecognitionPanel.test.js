import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import TableRecognitionPanel from './TableRecognitionPanel.vue';
it('展示真实列用途、原表单元格；估算后显式确认才调用 AI', async () => {
 const w = mount(TableRecognitionPanel,{props:{state:{extraction:{sheet_title:'评分',header_row:1,mapping:{name:1},records:[{row_number:2}],unmapped_columns:[{column:2,header:'成绩'}]},triggers:[]},previews:[{unit_id:'xlsx:评分!R2C1',text:'研究方法（20分）'}],editable:true,connection:{name:'连接',model_name:'test'}}});
 expect(w.text()).toContain('满分 · 从文字提取');
 await w.get('[data-test=structure-estimate]').trigger('click');
 expect(w.emitted('suggest-structure')[0][0]).toEqual({dryRun:true});
 await w.setProps({estimate:{chars:100,calls:1}});
 await w.get('[data-test=structure-run]').trigger('click');
 expect(w.emitted('suggest-structure')[1][0]).toEqual({dryRun:false});
 await w.findAll('header button')[1].trigger('click');
 expect(w.find('table').text()).toContain('研究方法（20分）');
});
it('原表对照按抽取结果与台账归属标出每行识别为什么', async () => {
 const cell = (row, text, claimed = []) => ({unit_id:`xlsx:评分!R${row}C1`, text, locator:{review:{claimed_by:claimed}}});
 const w = mount(TableRecognitionPanel,{props:{state:{extraction:{sheet_title:'评分',header_row:2,mapping:{name:1},records:[{row_number:3,name:'方法'},{row_number:4,name:'规范'}],total_row:{row_number:6},dropped_rows:[{row_number:5,reason:'整行合并的说明行'}],unmapped_columns:[]},triggers:[]},
  previews:[cell(1,'说明'),cell(2,'评分项目'),cell(3,'方法（20分）',['C1.name','C1.max_score']),cell(4,'规范（10分）'),cell(5,'附注',['C2.manual']),cell(6,'合计'),{unit_id:'xlsx:其他!R3C1',text:'别的表'}]}});
 await w.findAll('header button')[1].trigger('click');
 const role = n => w.findAll(`[data-test=raw-row-${n}] .role-chip`).map(c => c.text());
 expect(role(1)).toEqual(['表头前 · 未使用']);
 expect(role(2)).toEqual(['表头']);
 expect(role(3)).toEqual(['评分项 → C1', '其他工作表 · 未使用']);
 expect(role(4)).toEqual(['评分项 · 规范']);
 expect(role(5)).toEqual(['未计入 · 整行合并的说明行']);
 expect(role(6)).toEqual(['合计 · 不计入']);
});
