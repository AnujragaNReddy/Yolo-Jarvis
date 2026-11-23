import ast
import math
import operator
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from .. import config, db
from . import tool

_http = httpx.Client(timeout=15, headers={"User-Agent": config.USER_AGENT}, follow_redirects=True)
_where_cache: tuple[float, dict] | None = None

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
    82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


@tool("Current local date and time.")
def get_time() -> str:
    now = datetime.now()
    return now.strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " ")


def _here() -> dict:
    """Approximate location from this PC's public IP, cached for an hour."""
    global _where_cache
    if _where_cache and time.time() - _where_cache[0] < 3600:
        return _where_cache[1]
    r = _http.get("http://ip-api.com/json/?fields=status,city,country,countryCode,lat,lon").json()
    if r.get("status") != "success":
        raise RuntimeError("could not work out where you are; set a city in Settings")
    place = {"name": r["city"], "country": r["country"], "country_code": r["countryCode"],
             "lat": r["lat"], "lon": r["lon"]}
    _where_cache = (time.time(), place)
    return place


def _place(city: str | None) -> dict:
    city = (city or db.setting("city")).strip()
    if not city:
        return _here()
    r = _http.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1}).json()
    if not r.get("results"):
        raise RuntimeError(f"no place called {city}")
    g = r["results"][0]
    return {"name": g["name"], "country": g.get("country", ""), "country_code": g.get("country_code", ""),
            "lat": g["latitude"], "lon": g["longitude"]}


@tool("Current weather and today's forecast. Leave city empty for the user's own location.",
      city="string?: city name, e.g. London")
def get_weather(city: str = "") -> dict:
    p = _place(city)
    imperial = db.setting("units") == "imperial"
    r = _http.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": p["lat"], "longitude": p["lon"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone": "auto", "forecast_days": 1,
        "temperature_unit": "fahrenheit" if imperial else "celsius",
        "wind_speed_unit": "mph" if imperial else "kmh",
    }).json()
    c, d = r["current"], r["daily"]
    unit = "°F" if imperial else "°C"
    return {
        "place": f"{p['name']}, {p['country']}".strip(", "),
        "now": WEATHER_CODES.get(c["weather_code"], "unknown"),
        "temperature": f"{round(c['temperature_2m'])}{unit}",
        "feels_like": f"{round(c['apparent_temperature'])}{unit}",
        "humidity": f"{c['relative_humidity_2m']}%",
        "wind": f"{round(c['wind_speed_10m'])} {'mph' if imperial else 'km/h'}",
        "today_high": f"{round(d['temperature_2m_max'][0])}{unit}",
        "today_low": f"{round(d['temperature_2m_min'][0])}{unit}",
        "rain_chance": f"{d['precipitation_probability_max'][0]}%",
        "today": WEATHER_CODES.get(d["weather_code"][0], "unknown"),
        "is_day": bool(c["is_day"]),
        "code": c["weather_code"],
    }


@tool("Search the web for current information, facts, prices, sports results, people or anything you don't know.",
      query="string: what to search for")
def web_search(query: str) -> str:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    try:
        results = DDGS().text(query, max_results=5)
    except DDGSException:
        results = []
    if not results:
        return f"The web search found nothing for {query}."
    return "\n".join(f"- {r['title']}: {r['body'][:240]}" for r in results)


@tool("Short encyclopedia summary of a person, place, thing or concept from Wikipedia.",
      topic="string: what to look up")
def wikipedia(topic: str) -> str:
    try:
        r = _http.get("https://en.wikipedia.org/w/api.php", params={
            "action": "opensearch", "search": topic, "limit": 1, "namespace": 0, "format": "json"})
        r.raise_for_status()
        found = r.json()
        if not found[1]:
            return f"Nothing on Wikipedia for {topic}."
        title = found[1][0]
        s = _http.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'))}")
        s.raise_for_status()
        s = s.json()
        return f"{s.get('title', title)}: {s.get('extract', 'no summary')}"
    except (httpx.HTTPError, ValueError, IndexError):
        # Wikipedia throttles now and then; a web search answers the same question.
        return web_search(topic)


@tool("Latest news headlines, optionally about a topic.", topic="string?: e.g. technology, cricket, India")
def get_news(topic: str = "") -> list[dict]:
    try:
        cc = (_here().get("country_code") or "US").upper()
    except Exception:
        cc = "US"
    params = {"hl": f"en-{cc}", "gl": cc, "ceid": f"{cc}:en"}
    url = "https://news.google.com/rss"
    if topic:
        url += "/search"
        params["q"] = topic
    root = ET.fromstring(_http.get(url, params=params).text)
    items = []
    for item in root.iter("item"):
        title = item.findtext("title", "")
        source = item.findtext("source", "")
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        items.append({"title": title, "source": source, "link": item.findtext("link", "")})
        if len(items) == 6:
            break
    return items


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {k: getattr(math, k) for k in ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil",
                                         "factorial", "radians", "degrees")}
_FUNCS.update(abs=abs, round=round, min=min, max=max)
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and args and args[0] > 1000:
            raise ValueError("number too large")
        return _FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    raise ValueError("unsupported expression")


@tool("Exact arithmetic. Use it for any maths instead of working it out yourself.",
      expression="string: e.g. (12.5 * 4) / 3 or sqrt(144) or 15% as 0.15*x")
def calculate(expression: str) -> str:
    expr = expression.replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    value = _eval(ast.parse(expr, mode="eval").body)
    if isinstance(value, float):
        value = round(value, 10)
        if value.is_integer():
            value = int(value)
    return f"{expression} = {value}"
