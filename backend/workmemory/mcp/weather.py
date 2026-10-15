"""Read-only weather adapter. Never requests device location."""

import httpx

TOOLS = [
    {
        "name": "search_cities",
        "description": "按用户提供的城市名查找经纬度；有同名城市时让用户选择，不能猜测当前位置。",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 2, "maxLength": 100}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_weather",
        "description": "读取已确定城市的实时天气和未来七天预报。必须使用用户明确指定、城市查询返回或已确认常用城市的坐标。",
        "parameters": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                "city": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            "required": ["latitude", "longitude", "city"],
            "additionalProperties": False,
        },
    },
]


async def call(name, args):
    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
        if name == "search_cities":
            response = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": args["name"], "count": 5, "language": "zh", "format": "json"},
            )
            response.raise_for_status()
            result = response.json()
            return {
                "cities": [
                    {
                        k: row.get(k)
                        for k in ["name", "country", "admin1", "latitude", "longitude", "timezone"]
                    }
                    for row in result.get("results", [])
                ],
                "source_url": str(response.url),
                "title": "城市查询 · Open-Meteo",
            }
        if name != "get_weather":
            raise ValueError("未知天气工具")
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": args["latitude"],
                "longitude": args["longitude"],
                "timezone": "auto",
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": 7,
            },
        )
        response.raise_for_status()
        return {
            "city": args["city"],
            "weather": response.json(),
            "source_url": str(response.url),
            "title": args["city"] + "天气 · Open-Meteo",
        }
