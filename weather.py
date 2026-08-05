"""
奈奈機器人 — 天氣查詢

用 Open-Meteo（免金鑰、不需註冊）。天氣是少數「用搜尋引擎抓摘要會很爛」的
查詢 —— 搜尋結果只會給你一堆天氣網站的連結，抓不到實際數字，所以值得做成
獨立工具。

地名 → 座標採三層備援：
  1. 內建台灣縣市表 —— 零延遲、無失敗可能，涵蓋絕大多數查詢
  2. Open-Meteo geocoding
  3. Nominatim (OSM)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("nana.weather")

_UA = "nana-bot/1.0 (Discord companion bot)"
_client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": _UA})

# 內建表：台灣 22 縣市 + 常見別稱。避開網路查詢，也避開中文 geocoding 的坑。
_TW: dict[str, tuple[float, float, str]] = {
    "台北": (25.038, 121.564, "臺北市"), "臺北": (25.038, 121.564, "臺北市"),
    "新北": (25.012, 121.465, "新北市"), "板橋": (25.012, 121.465, "新北市"),
    "桃園": (24.993, 121.301, "桃園市"), "台中": (24.147, 120.674, "臺中市"),
    "臺中": (24.147, 120.674, "臺中市"), "台南": (22.999, 120.227, "臺南市"),
    "臺南": (22.999, 120.227, "臺南市"), "高雄": (22.627, 120.301, "高雄市"),
    "基隆": (25.128, 121.742, "基隆市"), "新竹": (24.804, 120.972, "新竹市"),
    "苗栗": (24.560, 120.821, "苗栗縣"), "彰化": (24.081, 120.539, "彰化縣"),
    "南投": (23.961, 120.972, "南投縣"), "雲林": (23.709, 120.431, "雲林縣"),
    "嘉義": (23.480, 120.449, "嘉義市"), "屏東": (22.552, 120.549, "屏東縣"),
    "宜蘭": (24.702, 121.738, "宜蘭縣"), "花蓮": (23.991, 121.601, "花蓮縣"),
    "台東": (22.758, 121.144, "臺東縣"), "臺東": (22.758, 121.144, "臺東縣"),
    "澎湖": (23.571, 119.579, "澎湖縣"), "金門": (24.437, 118.317, "金門縣"),
    "馬祖": (26.160, 119.949, "連江縣"), "連江": (26.160, 119.949, "連江縣"),
}

# WMO weather code → 中文
_WMO = {
    0: "晴朗", 1: "大致晴朗", 2: "多雲時晴", 3: "陰天",
    45: "有霧", 48: "凍霧",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "凍雨", 57: "強凍雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "凍雨", 67: "強凍雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪珠",
    80: "陣雨", 81: "強陣雨", 82: "劇烈陣雨",
    85: "陣雪", 86: "強陣雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "強雷雨伴冰雹",
}


@dataclass
class Place:
    lat: float
    lon: float
    name: str


async def geocode(query: str) -> Place | None:
    q = (query or "").strip().replace("市", "").replace("縣", "")
    if not q:
        return None

    for key, (lat, lon, name) in _TW.items():
        if key in q:
            return Place(lat, lon, name)

    try:
        r = await _client.get("https://geocoding-api.open-meteo.com/v1/search",
                              params={"name": query, "count": 1, "language": "zh"})
        if r.status_code == 200:
            res = (r.json() or {}).get("results") or []
            if res:
                return Place(res[0]["latitude"], res[0]["longitude"], res[0].get("name") or query)
    except Exception as e:  # noqa: BLE001
        logger.debug("open-meteo geocoding 失敗：%s", e)

    try:
        r = await _client.get("https://nominatim.openstreetmap.org/search",
                              params={"q": query, "format": "json", "limit": 1,
                                      "accept-language": "zh-TW"})
        r.raise_for_status()
        res = r.json() or []
        if res:
            name = (res[0].get("name") or "").strip() or query
            return Place(float(res[0]["lat"]), float(res[0]["lon"]), name)
    except Exception as e:  # noqa: BLE001
        logger.warning("nominatim geocoding 失敗：%s", e)

    return None


async def forecast(query: str) -> str:
    """回傳可直接塞進 prompt 的天氣描述；查不到回空字串。"""
    place = await geocode(query)
    if place is None:
        return ""

    try:
        r = await _client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place.lat, "longitude": place.lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "precipitation,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max",
                "timezone": "Asia/Taipei", "forecast_days": 3,
            },
        )
        r.raise_for_status()
        d = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("天氣查詢失敗：%s", e)
        return ""

    cur = d.get("current") or {}
    day = d.get("daily") or {}
    lines = [f"{place.name} 現在 {cur.get('temperature_2m')}°C"
             f"（體感 {cur.get('apparent_temperature')}°C）"
             f"，{_WMO.get(cur.get('weather_code'), '未知天氣')}"
             f"，濕度 {cur.get('relative_humidity_2m')}%"]

    labels = ("今天", "明天", "後天")
    times = day.get("time") or []
    for i, label in enumerate(labels[:len(times)]):
        lines.append(
            f"{label}：{_WMO.get((day.get('weather_code') or [None])[i], '未知')}，"
            f"{(day.get('temperature_2m_min') or [None])[i]}–"
            f"{(day.get('temperature_2m_max') or [None])[i]}°C，"
            f"降雨機率 {(day.get('precipitation_probability_max') or [None])[i]}%"
        )
    logger.info("🌤️ 天氣 │ %s", place.name)
    return "\n".join(lines)
