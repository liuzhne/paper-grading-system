"""前后端接口契约版本（版本守卫）。

后端每个响应带 ``X-PGS-Contract``；前端记着自己构建时的值，发现不一致时，在下一次写
操作或路由切换之前静默刷新页面。已经打开的旧页面因此不会调用已停用的接口。

**停用或改变任何前端在用的接口时，同时修改这里和 frontend/workbench/src/api/contract.js**
（``test_ai_tasks.py`` 校验两边一致）。只改界面、接口不变时不必修改。
"""

API_CONTRACT_HEADER = "X-PGS-Contract"
API_CONTRACT_VERSION = "2026-10-10.ai-tasks-a2"

__all__ = ["API_CONTRACT_HEADER", "API_CONTRACT_VERSION"]
