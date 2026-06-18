"""离线就绪审计：盘点网络触点，判定当前配置是否"纯本地"（CLI `pgs doctor` 与 Web 共用）。

scope 取值：offline（完全不触网，如 mock）/ local（连本地端口，如本地私有模型）/ external（外呼厂商云/网络服务）。
"""

from backend.app.core.config import settings
from backend.app.services.llm.factory import provider_network_scope

NETWORK_SHEET_PROVIDERS = {"google_sheets", "google_apps_script"}


def network_touchpoints():
    """返回 [{component, scope, detail}]，列出所有可能触网的环节及其当前网络属性。"""
    llm_scope = provider_network_scope()
    sheet = (settings.SHEET_WRITER_PROVIDER or "mock").lower()
    sheet_scope = "external" if sheet in NETWORK_SHEET_PROVIDERS else "offline"
    return [
        {"component": "LLM 评分", "scope": llm_scope, "detail": "本地私有模型/Mock 不触网；云厂商需外呼"},
        {"component": "表格导出", "scope": sheet_scope, "detail": "Google Sheets 需外呼；Excel/Mock 本地"},
    ]


def offline_ready():
    """当前配置是否纯本地：任何环节都不是 external 即就绪。"""
    return all(point["scope"] != "external" for point in network_touchpoints())
