import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
const root = resolve(process.cwd(), '../..');
const python = process.env.PGS_PYTHON || resolve(root, '.venv/bin/python');
const workbook = execFileSync(python, ['-c', `import sys
from io import BytesIO
from openpyxl import load_workbook
from backend.app.tests.rubric_parse_fixtures import merged_parent_dimension_xlsx
b=load_workbook(BytesIO(merged_parent_dimension_xlsx()))
o=BytesIO(); b.save(o); sys.stdout.buffer.write(o.getvalue())`], {cwd:root});
const document = execFileSync(python, ['-c', `import sys
from io import BytesIO
from docx import Document
book=Document()
book.add_heading('摘要',level=1)
book.add_paragraph('错别字每处扣1分。')
book.add_paragraph('摘要应当说明研究方法。')
out=BytesIO(); book.save(out); sys.stdout.buffer.write(out.getvalue())`],{cwd:root});
test('分步识别：分值核对后进入原文归类、移出恢复及窄屏布局', async ({page,request}) => {
  const name = `识别校对合成-${Date.now()}`;
  const imported = await request.post('/api/rubrics/import-files',{multipart:{name,version:'v1',rules_file:{name:'synthetic.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:workbook},template_file:{name:'synthetic.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:document}}});
  expect(imported.ok(),await imported.text()).toBeTruthy();
  const id=(await imported.json()).rubric.id;
  await page.goto('/workbench/rubrics');
  await page.getByRole('button',{name:'模板库',exact:true}).click();
  await page.locator('.library-menu .lib-item',{hasText:name}).click();
  const next=page.locator('.recognition-footer').getByRole('button',{name:'保存评分项，下一步',exact:true});
  await expect(page.getByText('规则来源 · 第 2 步使用',{exact:true})).toBeVisible();
  await expect(page.locator('.source-review')).toHaveCount(0);
  await expect(next).toBeDisabled();
  await page.getByRole('button',{name:'分值无误，全部确认',exact:true}).click();
  await page.getByRole('button',{name:'原表对照',exact:true}).click();
  await expect(page.locator('.raw-table')).toContainText('指导教师成绩项2');
  await expect(page.locator('.raw-table .role-chip',{hasText:'表头'}).first()).toBeVisible();
  await expect(page.locator('.raw-table')).toContainText('评分项 →');
  await next.click();
  const blockers=page.locator('[data-test=step-two-blockers]');
  const ruleBlocker=blockers.getByRole('button',{name:'待归类 · 1 条疑似规则',exact:true});
  await expect(ruleBlocker).toBeVisible();
  const missingBlocker=blockers.getByRole('button',{name:/缺少完整计分细则$/});
  const firstMissing=(await missingBlocker.textContent()).trim().split(' ')[0];
  await missingBlocker.click();
  await expect(page.locator('[data-test=rule-sources]')).toBeVisible();
  await expect(page.locator('.criteria-nav .lib-item.active')).toContainText(firstMissing);
  await expect(page.locator('.source-review')).toHaveCount(0);
  await ruleBlocker.click();
  await expect(page.getByRole('tab',{name:'疑似规则',exact:true})).toHaveAttribute('aria-selected','true');
  await expect(page.locator('.context-panel')).toContainText('错别字每处扣1分');
  await page.locator('.criterion-buttons button').first().click();
  await expect(page.locator('.paragraph-list')).not.toContainText('错别字每处扣1分');
  await page.getByRole('tab',{name:'已处理',exact:true}).click();
  await expect(page.locator('.paragraph-list')).toContainText('错别字每处扣1分');
  await page.reload();
  await page.getByRole('button',{name:'模板库',exact:true}).click();
  await page.locator('.library-menu .lib-item',{hasText:name}).click();
  await page.getByRole('button',{name:'分值无误，全部确认',exact:true}).click();
  await next.click();
  await page.locator('.source-nav').click();
  await page.getByRole('tab',{name:'已处理',exact:true}).click();
  await expect(page.locator('.paragraph-list')).toContainText('错别字每处扣1分');
  const state=await (await request.get(`/api/rubrics/${id}/parse-coverage`)).json();
  expect(state.coverage.blocking_count).toBe(0);
  await page.screenshot({path:'test-results/recognition-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('.source-review')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({path:'test-results/recognition-mobile.png',fullPage:true});
  await page.setViewportSize({width:1280,height:900});
  await page.locator('.criteria-nav .lib-item').nth(1).click();
  await expect(page.locator('[data-test=rule-sources]')).toContainText('错别字每处扣1分');
  await expect(page.locator('[data-test=rule-sources]')).toContainText('归入的原文要求 · 1 个单元');
  await page.screenshot({path:'test-results/rule-sources-desktop.png',fullPage:true});
  await page.locator('[data-test=rule-sources]').getByRole('button',{name:'移出',exact:true}).click();
  await page.locator('.source-nav').click();
  await expect(page.locator('.paragraph-list')).toContainText('错别字每处扣1分');
  expect((await (await request.get(`/api/rubrics/${id}/parse-coverage`)).json()).coverage.blocking_count).toBe(1);
});

test('评分项的归入原文默认只展开第一条，其余可展开和收起', async ({page,request}) => {
  const name = `来源折叠合成-${Date.now()}`;
  const rules = execFileSync(python, ['-c', `import sys
from io import BytesIO
from docx import Document
book=Document()
book.add_heading('格式要求',level=1)
for text in ('错别字每处扣1分。','图表不规范每处扣2分。','参考文献格式错误每处扣1分。'):
    book.add_paragraph(text)
out=BytesIO(); book.save(out); sys.stdout.buffer.write(out.getvalue())`],{cwd:root});
  const imported = await request.post('/api/rubrics/import-files',{multipart:{name,version:'v1',rules_file:{name:'synthetic.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:workbook},template_file:{name:'synthetic.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:rules}}});
  expect(imported.ok(),await imported.text()).toBeTruthy();
  const id=(await imported.json()).rubric.id;
  const unitIds=(await (await request.get(`/api/rubrics/${id}/parse-coverage`)).json()).coverage.unclaimed.map(u => u.unit_id).filter(u => u.startsWith('docx:'));
  expect(unitIds.length).toBeGreaterThan(1);
  const code=(await (await request.get(`/api/rubrics/${id}/source-workspace`)).json()).criteria[0].code;
  const resolved=await request.post(`/api/rubrics/${id}/units/resolve-batch`,{data:{unit_ids:unitIds,action:'assign',criterion_code:code,reason:'合成数据归入同一评分项'}});
  expect(resolved.ok(),await resolved.text()).toBeTruthy();
  await page.goto('/workbench/rubrics');
  await page.getByRole('button',{name:'模板库',exact:true}).click();
  await page.locator('.library-menu .lib-item',{hasText:name}).click();
  await page.getByRole('button',{name:'分值无误，全部确认',exact:true}).click();
  await page.locator('.recognition-footer').getByRole('button',{name:'保存评分项，下一步',exact:true}).click();
  await page.locator('.criteria-nav .lib-item',{hasText:code}).click();
  const sources=page.locator('[data-test=rule-sources]');
  await expect(sources).toContainText(`归入的原文要求 · ${unitIds.length} 个单元`);
  await expect(sources.locator('article')).toHaveCount(1);
  const toggle=sources.locator('[data-test=toggle-assigned-sources]');
  await expect(toggle).toHaveText(`展开其余 ${unitIds.length - 1} 个单元`);
  await toggle.click();
  await expect(sources.locator('article')).toHaveCount(unitIds.length);
  await expect(toggle).toHaveText('收起');
  await toggle.click();
  await expect(sources.locator('article')).toHaveCount(1);
});
