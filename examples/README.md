# 示例

两份文件都是合成的，只用于演示评分流程，不含任何真实论文或学生信息。

- `rubric.xlsx`：评分标准，与 `GET /api/rubrics/import-template.xlsx` 下载的导入模板内容相同（7 个评分项，满分 100）。
- `thesis.docx`：一篇虚构的短论文（摘要、五章正文、参考文献）。

```bash
uv run pgs score examples/thesis.docx --no-db --rubric-file examples/rubric.xlsx --mock
```

去掉 `--mock` 并配置真实模型（见 [模型接入](../docs/integrations.md)）即可看到模型给出的逐项判断与证据。
