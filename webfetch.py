"""
奈奈機器人 — 讀取使用者給的網址

跟 websearch.py 的差別：那支是「丟關鍵字給搜尋引擎」，這支是「抓使用者指定的
網址」。後者是把外部輸入變成伺服器主動發出的請求，屬於 SSRF 面，所以這裡的
重點其實不在抓網頁，而在**擋住內網**。

這台機器上跑著大量內部服務（llama-server :10003、PM API、gateway、
語音服務 :8891、docker 內的 MySQL/Redis…），沒有防護的話使用者只要貼
`http://127.0.0.1:10003/v1/models` 就能叫奈奈幫他偵察內網並把結果念出來。

防護做法：
  - 只允許 http / https
  - 解析主機名 → **所有** A/AAAA 記錄都必須是公網位址
  - 不用 httpx 的自動轉址，改成手動逐跳驗證（否則公網網址可以 302 到內網）
  - 限制大小與逾時，避免有人丟 500MB 的檔案

內容處理：HTML 走 trafilatura 抽正文（整份 HTML 塞進 prompt 會被導覽列和
script 洗掉，而每個 slot 只有 32k context）；PDF 直接複用 attachments 那套。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

import config

logger = logging.getLogger("nana.webfetch")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}

# Discord 會把網址包在 <> 裡避免預覽，也要能抓到
_URL_RE = re.compile(r"<?(https?://[^\s<>\"'）)】」』]+)>?", re.IGNORECASE)


@dataclass
class Page:
    url: str
    title: str
    text: str = ""
    images: list[str] | None = None   # data URI（PDF 掃描檔轉圖用）
    error: str = ""


def find_urls(text: str) -> list[str]:
    """抓出訊息裡的網址，去重並保留順序。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(1).rstrip(".,;:!?、。，")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ── SSRF 防護 ───────────────────────────────────────────

def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # is_private 已涵蓋 10/8、172.16/12、192.168/16、fc00::/7 等
    # is_link_local 涵蓋 169.254.0.0/16（含 169.254.169.254 雲端 metadata）
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _check_url(url: str) -> tuple[bool, str]:
    """回傳 (是否放行, 拒絕原因)。"""
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False, "網址格式不正確"

    if p.scheme.lower() not in ("http", "https"):
        return False, "只支援 http / https"
    host = p.hostname
    if not host:
        return False, "網址沒有主機名"

    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "找不到這個網域"

    ips = {i[4][0] for i in infos}
    if not ips:
        return False, "找不到這個網域"
    # 只要有任何一筆解析到內網就拒絕（避免 round-robin 繞過）
    bad = [ip for ip in ips if not _ip_is_public(ip)]
    if bad:
        logger.warning("拒絕內網網址 %s → %s", url[:80], ", ".join(sorted(bad)))
        return False, "這是內部網址，我不能去讀"
    return True, ""


# ── 抓取 ────────────────────────────────────────────────

async def _get(url: str) -> tuple[bytes, str, str, str]:
    """手動逐跳驗證的 GET。回傳 (內容, content-type, 最終網址, 錯誤)。"""
    current = url
    async with httpx.AsyncClient(
        timeout=config.FETCH_TIMEOUT, follow_redirects=False, headers=_HEADERS
    ) as client:
        for _hop in range(config.FETCH_MAX_REDIRECTS + 1):
            ok, why = _check_url(current)
            if not ok:
                return b"", "", current, why

            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location", "")
                        if not loc:
                            return b"", "", current, "轉址沒有目的地"
                        # 相對路徑要補回 scheme/host
                        if loc.startswith("/"):
                            p = urlparse(current)
                            loc = urlunparse((p.scheme, p.netloc, loc, "", "", ""))
                        current = loc
                        continue

                    if resp.status_code >= 400:
                        return b"", "", current, f"網站回傳 {resp.status_code}"

                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > config.FETCH_MAX_MB * 1024 * 1024:
                            return b"", "", current, f"內容超過 {config.FETCH_MAX_MB:g} MB"
                    return bytes(buf), ctype, current, ""
            except httpx.TimeoutException:
                return b"", "", current, "連線逾時"
            except Exception as e:  # noqa: BLE001
                logger.warning("抓取 %s 失敗：%s", current[:80], e)
                return b"", "", current, "連不上這個網址"

    return b"", "", current, "轉址次數過多"


def _extract_html(raw: bytes, url: str) -> tuple[str, str]:
    """HTML → (標題, 正文)。"""
    import trafilatura

    html = raw.decode("utf-8", errors="replace")
    text = trafilatura.extract(
        html, include_comments=False, include_tables=True, favor_precision=True
    ) or ""

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()

    if not text:
        # trafilatura 抽不到（SPA、或整頁都是 JS 產生的）→ 粗略去標籤保底
        body = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    return title or url, text


async def fetch(url: str) -> Page:
    """抓一個網址並抽出可讀內容。"""
    raw, ctype, final_url, err = await _get(url)
    if err:
        return Page(url=url, title=url, error=err)

    if "pdf" in ctype or final_url.lower().endswith(".pdf"):
        import attachments
        text, imgs, perr = await asyncio.to_thread(
            attachments._read_pdf, raw, final_url.rsplit("/", 1)[-1] or "檔案.pdf"
        )
        if perr:
            return Page(url=final_url, title=final_url, error=perr)
        return Page(url=final_url, title=final_url.rsplit("/", 1)[-1] or final_url,
                    text=text, images=imgs)

    if ctype.startswith("image/"):
        import base64
        if len(raw) > config.MAX_IMAGE_MB * 1024 * 1024:
            return Page(url=final_url, title=final_url, error="圖片太大了")
        uri = f"data:{ctype};base64,{base64.b64encode(raw).decode()}"
        return Page(url=final_url, title=final_url.rsplit("/", 1)[-1] or final_url,
                    text="", images=[uri])

    if ctype and not (ctype.startswith("text/") or "html" in ctype
                      or "json" in ctype or "xml" in ctype):
        return Page(url=final_url, title=final_url, error=f"不支援的內容型態（{ctype}）")

    title, text = await asyncio.to_thread(_extract_html, raw, final_url)
    if not text.strip():
        return Page(url=final_url, title=title, error="這一頁抓不到文字內容")
    return Page(url=final_url, title=title, text=text)


async def fetch_all(urls: list[str]) -> list[Page]:
    """抓多個網址（有上限）。"""
    urls = urls[:config.FETCH_MAX_URLS]
    pages = await asyncio.gather(*(fetch(u) for u in urls), return_exceptions=True)
    out: list[Page] = []
    for u, p in zip(urls, pages):
        if isinstance(p, Exception):
            logger.warning("抓取 %s 例外：%s", u[:80], p)
            out.append(Page(url=u, title=u, error="讀取時出錯"))
        else:
            out.append(p)
    return out


def format_for_prompt(pages: list[Page], budget: int | None = None) -> str:
    """把抓到的網頁組成要塞進 prompt 的區塊。"""
    if not pages:
        return ""
    budget = budget or config.FETCH_CHARS_TOTAL
    blocks: list[str] = []
    for p in pages:
        if p.error:
            blocks.append(f"\n\n===== 網頁：{p.url} =====\n（讀不到：{p.error}。"
                          f"請誠實告訴對方，不要編造內容）\n===== 網頁結束 =====")
            continue
        if not p.text and p.images:
            blocks.append(f"\n\n===== 網頁：{p.url} =====\n"
                          f"（這個網址是圖片或掃描檔，內容以圖片形式附在後面）\n===== 網頁結束 =====")
            continue
        share = max(500, budget // max(1, len([x for x in pages if not x.error])))
        text = p.text[:share]
        if len(p.text) > share:
            text += f"\n…（全文過長，只讀了前 {share} 字）"
        blocks.append(f"\n\n===== 網頁：{p.title}\n{p.url} =====\n{text}\n===== 網頁結束 =====")
    return "".join(blocks) + (
        "\n（請根據上面的網頁內容回答，並自然地提到這是從那個連結看到的；"
        "內容不足以回答就老實說，不要編造）"
    )
