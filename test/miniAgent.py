import json
import os
from typing import Any, Dict

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# 1. 初始化 Groq OpenAI-compatible 客户端
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)


# 2. 真实工具：通过 Open-Meteo 获取实时天气
def get_current_weather(location: str) -> str:
    """
    获取指定城市的实时天气。
    这里使用 Open-Meteo 的 Geocoding API + Forecast API，不需要 API Key。
    """

    # Step 1: 城市名 -> 经纬度
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    geo_params = {
        "name": location,
        "count": 1,
        "language": "zh",
        "format": "json",
    }

    geo_resp = requests.get(geo_url, params=geo_params, timeout=10)
    geo_resp.raise_for_status()
    geo_data = geo_resp.json()

    if not geo_data.get("results"):
        return json.dumps(
            {
                "error": f"未找到城市：{location}",
                "location": location,
            },
            ensure_ascii=False,
        )

    place = geo_data["results"][0]
    latitude = place["latitude"]
    longitude = place["longitude"]

    # Step 2: 经纬度 -> 当前天气
    weather_url = "https://api.open-meteo.com/v1/forecast"
    weather_params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "timezone": "auto",
    }

    weather_resp = requests.get(weather_url, params=weather_params, timeout=10)
    weather_resp.raise_for_status()
    weather_data = weather_resp.json()

    current = weather_data.get("current", {})

    result = {
        "location": place.get("name", location),
        "country": place.get("country"),
        "latitude": latitude,
        "longitude": longitude,
        "temperature": current.get("temperature_2m"),
        "unit": weather_data.get("current_units", {}).get("temperature_2m", "°C"),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_speed_unit": weather_data.get("current_units", {}).get("wind_speed_10m"),
        "weather_code": current.get("weather_code"),
        "time": current.get("time"),
    }

    return json.dumps(result, ensure_ascii=False)


# 3. 真实工具：本地精确计算税费
def calculate_tax(amount: float, tax_rate: float = 0.1) -> str:
    """
    计算特定金额的税费。
    amount: 税前金额
    tax_rate: 税率，例如 0.15 表示 15%
    """

    tax = amount * tax_rate
    total = amount + tax

    return json.dumps(
        {
            "base_amount": amount,
            "tax_rate": tax_rate,
            "tax": tax,
            "total_amount": total,
        },
        ensure_ascii=False,
    )


tools_map = {
    "get_current_weather": get_current_weather,
    "calculate_tax": calculate_tax,
}


# 4. 工具声明：告诉 Llama 有哪些函数可以调用
tools_definition = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "获取指定城市的实时天气，包括温度、风速、天气代码等信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市名称，例如 Tokyo、东京、San Francisco、北京。",
                    }
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_tax",
            "description": "根据基础金额和税率计算税费与含税总金额。",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "税前基础金额。",
                    },
                    "tax_rate": {
                        "type": "number",
                        "description": "税率，例如 0.15 表示 15%。如果用户没有指定，默认使用 0.1。",
                    },
                },
                "required": ["amount"],
            },
        },
    },
]


def safe_json_loads(raw: str) -> Dict[str, Any]:
    """
    防止模型返回的 function arguments 不是合法 JSON。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# 5. Agent 核心循环
def run_agent(user_prompt: str) -> str:
    print(f"🚀 用户问题: {user_prompt}")
    print("-" * 60)

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个可以使用工具的助手。"
                "当用户的问题需要实时信息或精确计算时，你应该调用工具。"
                "不要编造工具结果。"
                "如果需要多个步骤，你可以连续调用多个工具。"
            ),
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ]

    max_steps = 8

    for step in range(max_steps):
        print(f"\n🤖 [第 {step + 1} 轮] 请求 Llama 模型...")
        print(messages)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools_definition,
            tool_choice="auto",
            temperature=0.2,
        )

        response_message = response.choices[0].message

        # 关键：必须把 assistant 的 tool_calls 消息加入上下文
        messages.append(response_message)

        tool_calls = response_message.tool_calls

        # 如果模型不再请求工具，说明已经准备好最终回答
        if not tool_calls:
            print("\n💡 [最终答案] 模型没有继续调用工具。")
            return response_message.content or ""

        # 执行模型请求的每一个工具
        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = safe_json_loads(tool_call.function.arguments)

            print(f"🛠️ [工具调用] {function_name}")
            print(f"📦 [工具参数] {function_args}")

            run_function = tools_map.get(function_name)

            if not run_function:
                observation = json.dumps(
                    {
                        "error": f"找不到工具：{function_name}",
                    },
                    ensure_ascii=False,
                )
            else:
                try:
                    observation = run_function(**function_args)
                except Exception as e:
                    observation = json.dumps(
                        {
                            "error": str(e),
                            "function_name": function_name,
                            "arguments": function_args,
                        },
                        ensure_ascii=False,
                    )

            print(f"👁️ [工具结果] {observation}")

            # 关键：把工具执行结果作为 role=tool 的消息发回模型
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": observation,
                }
            )

    return "Agent 达到最大步数限制，未能产出最终答案。"


if __name__ == "__main__":
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("请先在 .env 中配置 GROQ_API_KEY")

    final_answer = run_agent(
        "帮我查一下东京现在的温度是多少？然后把这个温度数值乘以 100 后的金额计算 15% 的税费。"
    )

    print("\n================ 最终 Agent 答复 ================")
    print(final_answer)