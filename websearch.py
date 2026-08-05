"""
奈奈機器人 — 上網搜尋

來源策略跟 PAI 平台的 WebSearchSkill 一樣走「免金鑰 + 多來源備援」，但來源清單
是 2026-08-02 實測後重挑的：

  ✅ DuckDuckGo html  — 唯一穩定回結果的抓取來源（實測 10 筆）
  ✅ DuckDuckGo IA    — 官方 Instant Answer API，只有部分查詢有摘要，當補充
  ✅ Wikipedia API    — 官方 API，查人事時地物很穩，當最後手段
  ❌ Brave            — 改版成 JS 殼，抓不到結果（平台那支 PHP 也已失效）
  ❌ DuckDuckGo lite  — 回 202 挑戰頁
  ❌ Mojeek / searx.be — 都擋 captcha
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass

import httpx

import config

logger = logging.getLogger("nana.search")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}

_client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS)


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""

    def as_line(self, idx: int) -> str:
        line = f"{idx}. {self.title}\n   {self.url}"
        if self.snippet:
            line += f"\n   {self.snippet}"
        return line


def _clean(s: str) -> str:
    """去掉標籤與多餘空白。"""
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _unwrap_ddg(url: str) -> str:
    """DuckDuckGo 的轉址連結 → 還原原始網址。"""
    url = html.unescape(url)          # href 裡的 &amp; 要先還原，否則 uddg 抓不到
    m = re.search(r"[?&]uddg=([^&]+)", url)
    if m:
        url = urllib.parse.unquote(m.group(1))
    if url.startswith("//"):
        url = "https:" + url
    return url


def _is_ad(url: str) -> bool:
    """DuckDuckGo 會把贊助連結混在結果裡（y.js?ad_provider=…），要濾掉。"""
    return "duckduckgo.com/y.js" in url or "ad_provider=" in url


# ── 來源 1：DuckDuckGo html ─────────────────────────────

async def _ddg_html(query: str, limit: int) -> list[Result]:
    resp = await _client.post(
        "https://html.duckduckgo.com/html/", data={"q": query}
    )
    resp.raise_for_status()
    body = resp.text

    links = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S
    )
    snips = re.findall(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', body, re.S
    )

    out: list[Result] = []
    for i, (url, title) in enumerate(links):
        if len(out) >= limit:
            break
        t = _clean(title)
        real = _unwrap_ddg(url)
        if not t or _is_ad(real):
            continue
        out.append(Result(t, real, _clean(snips[i])[:200] if i < len(snips) else ""))
    return out


# ── 來源 2：DuckDuckGo Instant Answer ───────────────────

async def _ddg_instant(query: str, limit: int) -> list[Result]:
    resp = await _client.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
    )
    resp.raise_for_status()
    # 這支有時回 text/javascript，httpx 的 .json() 仍能解
    data = json.loads(resp.text)

    out: list[Result] = []
    if data.get("AbstractText"):
        out.append(Result(
            data.get("Heading") or query,
            data.get("AbstractURL", ""),
            data["AbstractText"][:300],
        ))
    for topic in data.get("RelatedTopics", []):
        if len(out) >= limit:
            break
        if "Text" in topic and topic.get("FirstURL"):
            out.append(Result(topic["Text"][:80], topic["FirstURL"], topic["Text"][:200]))
    return out


# ── 來源 3：Wikipedia ───────────────────────────────────

async def _wikipedia(query: str, limit: int) -> list[Result]:
    resp = await _client.get(
        "https://zh.wikipedia.org/w/api.php",
        params={
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": limit,
        },
    )
    resp.raise_for_status()
    hits = resp.json().get("query", {}).get("search", [])
    return [
        Result(
            h["title"],
            f"https://zh.wikipedia.org/wiki/{urllib.parse.quote(h['title'])}",
            _clean(h.get("snippet", ""))[:200],
        )
        for h in hits
    ]


_SOURCES = (("ddg-html", _ddg_html), ("ddg-instant", _ddg_instant), ("wikipedia", _wikipedia))


async def search(query: str, limit: int | None = None) -> list[Result]:
    """依序試各來源，第一個有結果的就回傳。全掛則回空 list。"""
    query = query.strip()
    if not query:
        return []
    limit = limit or config.SEARCH_RESULT_LIMIT

    for name, fn in _SOURCES:
        try:
            rows = await asyncio.wait_for(fn(query, limit), timeout=config.SEARCH_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            logger.warning("搜尋來源 %s 失敗：%s", name, e)
            continue
        if rows:
            logger.info("🔎 搜尋 │ %s │ %s 回 %d 筆", query[:50], name, len(rows))
            return rows
        logger.debug("搜尋來源 %s 沒有結果", name)

    logger.warning("🔎 搜尋 │ %s │ 所有來源都沒結果", query[:50])
    return []


def format_for_prompt(query: str, results: list[Result]) -> str:
    """把搜尋結果組成要塞進 prompt 的參考資料區塊。"""
    if not results:
        return (
            f"\n\n===== 網路搜尋：{query} =====\n"
            "（這次沒有查到結果，請誠實告訴對方你查不到，不要自己編造）\n"
            "===== 搜尋結束 ====="
        )
    body = "\n".join(r.as_line(i + 1) for i, r in enumerate(results))
    return (
        f"\n\n===== 網路搜尋結果：{query} =====\n{body}\n"
        "===== 搜尋結束 =====\n"
        "（請根據上面的搜尋結果回答，並自然地提到消息來源；"
        "若結果不足以回答就老實說，不要編造）"
    )
