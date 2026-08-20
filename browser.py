"""
奈奈的瀏覽器操作 🖥️

用 Playwright 開一個無頭 Chromium，看畫面、點按鈕、填欄位，做完把截圖傳回去。
和 webfetch.py 的差別很重要：那支只是「把一頁抓下來讀」，這支會**真的動手操作**
別人的網站（掛號、查詢、送表單）。

會動手就有風險，所以幾條紅線寫死在程式裡，不靠模型自律：

  1. **內網一律不去** —— 沿用 webfetch._check_url 的 SSRF 檢查，而且**每次換頁都重驗**
     （網站可能 302 到內網）
  2. **送出前一定要本人確認** —— 看起來會造成不可逆結果的按鈕（送出／確認／付款／
     刪除／掛號）一律先停下來，把當下截圖丟回 Discord 等本人按確認才繼續
  3. **不自己編個人資料** —— 身分證、生日、電話、卡號這類只能用使用者自己給的；
     模型憑空生出來的一律不填（見 _value_allowed）
  4. **填進欄位的值不進 log** —— 一律遮蔽，只記長度
  5. 步數、總時間、同時開幾個 session 都有上限，逾時就收掉

模型只負責「看畫面決定下一步」，每一步都回來過這邊的關卡，所以就算它被網頁上的
文字騙了（prompt injection），能做的動作範圍還是被這裡框住。
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import audio_sink
import config
import llm_client
import webfetch

logger = logging.getLogger("nana.browser")

# Playwright 是選用相依 —— 沒裝也不能讓整個 bot 起不來
try:
    from playwright.async_api import async_playwright, Error as PWError
    AVAILABLE = True
except ImportError:  # pragma: no cover
    async_playwright = None
    PWError = Exception
    AVAILABLE = False


# ── 收集畫面上可以操作的東西 ────────────────────────────
# 在頁面裡跑，順手把 data-nana-idx 標上去 —— 之後用屬性選擇器點，
# 比用座標點可靠得多（座標會因為捲動、動畫、RWD 而失效）。
_COLLECT_JS = """
(maxN) => {
  const out = [];
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link],' +
              '[role=tab], [role=checkbox], [role=radio], [onclick], [contenteditable=true]';
  document.querySelectorAll('[data-nana-idx]').forEach(e => e.removeAttribute('data-nana-idx'));
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= maxN) break;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;                       // 看不見的跳過
    if (r.bottom < 0 || r.top > innerHeight * 3) continue;           // 離畫面太遠的跳過
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (el.disabled) continue;
    const key = el.tagName + '|' + r.x + '|' + r.y + '|' + r.width;
    if (seen.has(key)) continue;                                     // 重疊的同一個東西
    seen.add(key);

    const idx = out.length;
    el.setAttribute('data-nana-idx', String(idx));
    const label =
      (el.getAttribute('aria-label') || '') ||
      (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA'
         ? (el.labels && el.labels[0] ? el.labels[0].innerText : '') : '') ||
      (el.innerText || el.value || el.getAttribute('placeholder') ||
       el.getAttribute('title') || el.getAttribute('name') || '');
    out.push({
      i: idx,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      label: String(label).replace(/\\s+/g, ' ').trim().slice(0, 70),
      value: String(el.value ?? '').replace(/\\s+/g, ' ').trim().slice(0, 40),
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 40),
      checked: !!el.checked,
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).slice(0, 25).map(o => o.text.trim().slice(0, 40)) : [],
      onScreen: r.top >= 0 && r.bottom <= innerHeight,
    });
  }
  return out;
}
"""

# 給模型看的截圖上畫編號；使用者看到的截圖不畫（見 _shot）
_MARK_JS = """
() => {
  const box = document.createElement('div');
  box.id = '__nana_marks';
  box.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646';
  document.querySelectorAll('[data-nana-idx]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) return;
    const o = document.createElement('div');
    o.style.cssText = 'position:absolute;border:2px solid #ff2d95;box-sizing:border-box;' +
      'left:' + r.left + 'px;top:' + r.top + 'px;width:' + r.width + 'px;height:' + r.height + 'px';
    const t = document.createElement('div');
    t.textContent = el.getAttribute('data-nana-idx');
    t.style.cssText = 'position:absolute;left:' + r.left + 'px;top:' + Math.max(0, r.top - 15) +
      'px;background:#ff2d95;color:#fff;font:bold 12px monospace;padding:0 4px;border-radius:3px';
    box.appendChild(o); box.appendChild(t);
  });
  document.body.appendChild(box);
}
"""

_UNMARK_JS = "() => { const m = document.getElementById('__nana_marks'); if (m) m.remove(); }"


# ── 個資／敏感值的樣子 ──────────────────────────────────
# 只用來判斷「這個值是不是使用者自己給的」，不是用來抓格式正確性
_PII_PATTERNS = (
    re.compile(r"[A-Za-z][12]\d{8}"),          # 身分證
    re.compile(r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}"),   # 卡號
    re.compile(r"09\d{2}[- ]?\d{3}[- ]?\d{3}"),           # 手機
    re.compile(r"\b(19|20)\d{2}[-/年]\d{1,2}[-/月]\d{1,2}"),  # 生日
)


def _norm(s: str) -> str:
    return re.sub(r"[\s\-/年月日]", "", s or "").lower()


def _value_allowed(value: str, task: str) -> tuple[bool, str]:
    """要填進欄位的值可不可以填。

    看起來像個資（身分證／卡號／手機／生日）的話，**必須是使用者自己在指令裡給的**
    —— 模型很會「幫你填一個看起來合理的身分證」，那會送錯資料到真的系統裡去。
    """
    v = (value or "").strip()
    if not v:
        return True, ""
    for pat in _PII_PATTERNS:
        if pat.search(v):
            if _norm(v) in _norm(task):
                return True, ""
            return False, "這個值看起來是個人資料，但使用者沒有提供，不能自己編"
    return True, ""


def redact(text: str) -> str:
    """把看起來像個資的部分遮掉。

    要把「奈奈幫他做了什麼」寫進長期記憶之前一定先過這一關 ——
    使用者交代的話裡很可能就帶著身分證和生日，那種東西不該進資料庫。
    """
    out = text or ""
    for pat in _PII_PATTERNS:
        out = pat.sub("（個資已遮蔽）", out)
    return out


_CHATTY = re.compile(
    r"(幫我|幫他|幫她|替我|代我|幫忙|請你|麻煩你|可以嗎|好嗎|謝謝|拜託"
    r"|因為我|因為他|因為|所以|我想|我要|我需要|順便|一下|的話)")


def search_query(task: str, given: str = "") -> str:
    """組出丟給搜尋引擎的字。

    優先用模型給的關鍵字；沒有的話就把任務句子裡「講給人聽」的部分刮掉。
    直接把整句丟進去會變成 ?q=在嘉義找診所掛號，因為我發燒了 ——
    搜尋引擎不是這樣用的。
    """
    if given.strip():
        return re.sub(r"\s+", " ", given).strip()[:100]
    q = _CHATTY.sub(" ", task or "")
    q = re.sub(r"[，。、！？!?（）()「」]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:100] or (task or "")[:60]


def _sig(url: str, title: str, text: str, n_elements: int) -> str:
    """畫面內容指紋 —— 判斷「剛剛那個動作有沒有讓畫面真的改變」。

    **兩邊一定要用同一個算法**（_observe 和 _refresh_sig），
    不然算出來永遠不一樣，撞牆偵測就等於沒有。
    只比網址＋標題也不行：ASP.NET 的 postback（慈濟、長庚的掛號系統都是）
    換頁之後網址標題一模一樣，內容卻整個換掉。
    """
    return (f"{url}|{(title or '')[:60]}|{len(text or '')}"
            f"|{hash((text or '')[:600])}|{n_elements}")


def _mask(value: str) -> str:
    """log 用：只留長度，不留內容。"""
    n = len(value or "")
    return f"<{n} 字>" if n else "<空>"


def _looks_irreversible(label: str, tag: str, itype: str, *, typed: int = 0) -> bool:
    """這個東西按下去會不會造成不可逆的結果。

    不能單純看字面有沒有「掛號」——「網路掛號」這種其實只是導覽連結，
    把它當成送出會變成每點一個連結都要問一次（實測台大醫院首頁就中了）。
    所以分成兩種字：

      強動作詞（確認／送出／付款／刪除…）→ 不管是連結還是按鈕，一律先問
      主題詞（掛號／預約／報名…）→ 只有在「真的會送資料」的情況才算：
          • 元素本身是 button / submit
          • 或這一頁已經被填過欄位（typed > 0）—— 資料都填了，下一步很可能就是送出
    """
    text = f"{label} {itype}".lower()

    if any(w.lower() in text for w in config.BROWSER_STRONG_CONFIRM_WORDS):
        return True
    if tag == "input" and itype == "submit":
        return True

    if any(w.lower() in text for w in config.BROWSER_TOPIC_CONFIRM_WORDS):
        is_button = tag == "button" or (tag == "input" and itype in ("submit", "button"))
        return bool(is_button or typed > 0)
    return False


def _domain_ok(url: str) -> tuple[bool, str]:
    """網域政策 + 內網檢查。每次換頁都會再跑一次。"""
    ok, why = webfetch._check_url(url)      # 沿用既有的 SSRF 防護（含逐跳驗證的那套規則）
    if not ok:
        return False, why

    host = (urlparse(url).hostname or "").lower()
    for bad in config.BROWSER_BLOCK_DOMAINS:
        if host == bad.lower() or host.endswith("." + bad.lower()):
            return False, f"{host} 在黑名單裡"
    allow = config.BROWSER_ALLOW_DOMAINS
    if allow:
        if not any(host == a.lower() or host.endswith("." + a.lower()) for a in allow):
            return False, f"只允許這些網站：{'、'.join(allow)}"
    return True, ""


# ── 結果 ───────────────────────────────────────────────

@dataclass
class Step:
    n: int
    action: str
    detail: str
    url: str


@dataclass
class Result:
    """一次瀏覽任務的結果。

    status:
      done         做完了，summary 是給使用者的說明
      need_confirm 停在「要送出」前面，等本人確認（question 是要問的話）
      need_input   缺資料（例如沒給生日），要問本人
      blocked      被紅線擋住或網站不給進
      max_steps    步數／時間用完還沒做完
      error        壞掉了
    """
    status: str
    summary: str = ""
    url: str = ""
    shot: bytes | None = None
    question: str = ""
    steps: list[Step] = field(default_factory=list)
    # 畫面上有個資 —— 呼叫端要判斷這張圖能不能貼在公開頻道
    sensitive: bool = False
    video: str | None = None     # 操作錄影（webm）的路徑
    # 任務結束後畫面有沒有留著（留著的話錄影會晚一點才送）
    linger: bool = False
    # 最後看到的頁面文字。做完之後使用者常會追問「那個 XX 有什麼特點」，
    # 沒有這個的話她會說「我還沒看到」—— 明明剛剛才看完。
    page_text: str = ""

    @property
    def finished(self) -> bool:
        return self.status not in ("need_confirm", "need_input")


# ── 一個瀏覽 session ───────────────────────────────────

class Session:
    """一位使用者的一次瀏覽任務。

    停在確認點時整個 session 會留著（瀏覽器不關），等 resume() 再繼續 ——
    重開一個瀏覽器就得從頭登入／重填一遍，那對使用者很煩。
    """

    def __init__(self, user_id: int, task: str,
                 delegate_id: int | None = None,
                 delegate_name: str = "") -> None:
        self.user_id = user_id          # 交代這件事的人（結果回報給他）
        # 這件事是幫誰辦的。要填身分證、生日、驗證碼時問的是「他」而不是交代的人 ——
        # 別人的個資只有本人給得出來，也只有本人該點頭。
        self.delegate_id = delegate_id if delegate_id != user_id else None
        self.delegate_name = delegate_name if self.delegate_id else ""
        self.task = task
        self.search_hint = ""       # 模型給的搜尋關鍵字（沒給就從 task 推）
        self.steps: list[Step] = []
        self.started = time.time()
        self.touched = time.time()
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._video_dir: str | None = None
        self.video_path: str | None = None
        self._pending: dict | None = None      # 等確認的那個動作
        self._history: list[str] = []           # 給模型看的「已經做過什麼」
        # 現在卡在等什麼：confirm=等他按確認、input=等他補資料、None=不在等
        self.awaiting: str | None = None
        # 進度查詢用：跑到哪了、最後看到的畫面長什麼樣
        self.running = False
        self.last_shot: bytes | None = None
        self.last_shot_at = 0.0     # 最後一次截圖的時間（避免同一瞬間重複截）
        self.audio_on = False       # 這個瀏覽器發不發得出聲音（見 audio_sink）
        # 連續畫面（MJPEG 串流）用的最新一張，多個觀眾共用（見 live_shot）
        self.live_frame: bytes | None = None
        self.live_frame_at = 0.0
        self._live_lock = asyncio.Lock()
        self.stream_viewers = 0     # 現在有幾個人掛在串流上
        self.last_title = ""
        self.last_text = ""         # 最後讀到的頁面文字（給事後追問用）
        self.page_sig = ""          # 畫面內容指紋（判斷有沒有真的變化）
        self._on_frame = None       # 即時畫面的回呼（見 _pump_frames）
        self._on_closed = None      # 瀏覽器真的收掉時的回呼（錄影是那時候才寫完）
        # 人接手時暫停 —— 不然模型會跟人搶同一個 page（他點一下、模型又點別的）
        self.paused = False
        # 任務做完之後「畫面留著」的狀態。她已經不再自己動了，但 Chromium 還活著，
        # 人可以在操作台繼續看、自己點（例如叫她開 YouTube，開完自己接著看）。
        self.lingering = False
        self.linger_since = 0.0
        # 最後一次有人在操作台上看這個 session 的時間。面板每 1~2 秒會打一次 API，
        # 所以「很久沒更新」等於面板關了或斷線 —— 留著的畫面就該收掉。
        self.watched_at = 0.0
        # 新開的視窗（target=_blank 的連結會丟到這裡，見 _follow_new_tab）
        self._new_pages: list = []
        # 每一步的截圖：step 編號 → png。留著給 Discord 那邊翻頁看，
        # 不必每張都發一則訊息洗頻。有上限，不然長任務會吃掉一堆記憶體。
        self.shots: dict[int, bytes] = {}
        # 這一頁填過幾個欄位 —— 填過資料之後「掛號」那種按鈕就要當成送出
        self._typed_here = 0
        # 有沒有把個資填進畫面。填過之後截圖上就看得到身分證／生日，
        # 在公開頻道貼那種圖等於幫他公開個資（見 private_channel）。
        self.sensitive = False
        # 這個任務所在的頻道是不是私密的（私訊／私人聊天室）
        self.private_channel = False
        # 同一個動作重複了幾次而畫面沒變（撞牆偵測，見 _stuck_key）
        self._stuck: dict[str, int] = {}
        # 使用者按了「一律允許」→ 之後的送出不再一個一個問。
        # **只影響「要不要按下去」這件事**：缺資料還是會停下來問他（kind == "ask"），
        # 個資不能自己編、內網不能去這些紅線也都還在。
        self.auto_confirm = False

    # ── 生命週期 ──

    async def _ensure(self) -> None:
        if self._page is not None:
            return
        self._pw = await async_playwright().start()

        # 用「完整的 Chromium」而不是預設的 headless shell，並關掉自動化旗標。
        # 這不是為了騙人，是為了不要一開口就被當成爬蟲：
        #   預設的 headless shell → navigator.webdriver=true、plugins=0、
        #   UA 直接寫 HeadlessChrome —— 搜尋引擎第一次拜訪就會丟驗證碼給你，
        #   跟你搜了幾次完全無關。
        # 改成完整 Chromium + --disable-blink-features=AutomationControlled 之後
        # webdriver 變 false、plugins 有 5 個，看起來就是一般瀏覽器。
        # （遇到驗證碼還是會停下來交還給使用者，我們不去解它。）
        args = ["--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage"]

        # 聲音。要能聽到得同時做三件事，少一件就是完全無聲：
        #   1. 把 Playwright 自己加的 --mute-audio 拿掉（它預設就靜音，
        #      從 chrome://version 看得到；實測靜音時錄到 -91 dB）
        #   2. 關掉「要有使用者手勢才能自動播放」—— 沒有人會去點這個無頭瀏覽器
        #   3. 用 PULSE_SINK 把聲音導到我們自己的假裝置（見 audio_sink.py）
        # 音效環境起不來就照舊靜音跑 —— 聲音是加分的，不能讓它擋掉操作本身。
        launch_kw: dict = {}
        if await audio_sink.ensure():
            launch_kw["ignore_default_args"] = ["--mute-audio"]
            launch_kw["env"] = {**os.environ, **audio_sink.browser_env()}
            args.append("--autoplay-policy=no-user-gesture-required")
            self.audio_on = True

        try:
            self._browser = await self._pw.chromium.launch(
                headless=config.BROWSER_HEADLESS, channel="chromium",
                args=args, **launch_kw)
        except Exception as e:  # noqa: BLE001
            logger.warning("完整 Chromium 起不來（%s），退回預設的 headless shell",
                           str(e)[:100])
            self._browser = await self._pw.chromium.launch(
                headless=config.BROWSER_HEADLESS,
                args=args + ["--no-sandbox"], **launch_kw)
        opts = dict(
            viewport={"width": config.BROWSER_WIDTH, "height": config.BROWSER_HEIGHT},
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            user_agent=webfetch._UA,
        )
        if config.BROWSER_RECORD:
            # 影片要等 context 關掉才會寫完，所以檔名在 close() 裡才拿得到
            self._video_dir = tempfile.mkdtemp(prefix="nana-rec-")
            opts["record_video_dir"] = self._video_dir
            opts["record_video_size"] = {"width": config.BROWSER_WIDTH,
                                         "height": config.BROWSER_HEIGHT}
        ctx = await self._browser.new_context(**opts)
        self._ctx = ctx
        ctx.set_default_timeout(config.BROWSER_STEP_TIMEOUT_MS)
        self._page = await ctx.new_page()
        # 很多掛號／訂票網站的連結是 target=_blank，會開新視窗。
        # 不跟過去的話原本那頁完全沒變，模型就會以為沒點到、一直重複點同一個連結。
        #
        # 監聽一定要在主頁面建立**之後**才註冊：不然 new_page() 自己會觸發這個
        # 事件，主頁面被當成「新視窗」丟進清單，第一次點擊時 _follow_new_tab
        # 就拿它去做網域檢查，about:blank 不合格 → 把主頁面關掉。
        ctx.on("page", lambda pg: self._new_pages.append(pg))

    async def _follow_new_tab(self) -> str:
        """剛剛有開新視窗就跟過去。回傳要補在步驟說明後面的字。"""
        if not self._new_pages:
            return ""
        fresh = [pg for pg in self._new_pages if pg is not self._page]
        self._new_pages.clear()
        if not fresh:
            return ""            # 只有目前這頁被通知到，不是真的新視窗
        page = fresh[-1]
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        ok, why = _domain_ok(page.url)
        if not ok:
            logger.info("🖥️ 新視窗去了不該去的地方，關掉：%s", why)
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
            return "（它想開一個我不能去的網址，我把那個視窗關掉了）"
        self._page = page
        self._typed_here = 0        # 換頁了，重新算這一頁填過幾個欄位
        return "（開了新視窗，我跟過去了）"

    async def close(self) -> None:
        # 一定要先關 context —— Playwright 是在 context 關閉時才把影片寫完整
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
            self._ctx = None
        if self._video_dir:
            try:
                vids = sorted(glob.glob(os.path.join(self._video_dir, "*.webm")),
                              key=os.path.getsize, reverse=True)
                if vids:
                    self.video_path = vids[0]
                    logger.info("🎥 操作錄影：%.1f MB",
                                os.path.getsize(vids[0]) / 1024 / 1024)
            except Exception as e:  # noqa: BLE001
                logger.warning("找不到錄影檔：%s", e)

        for closer in (self._browser, self._pw):
            try:
                if closer is self._pw:
                    await closer.stop()          # type: ignore[union-attr]
                else:
                    await closer.close()         # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        self._page = self._browser = self._pw = None

    @property
    def task_for_model(self) -> str:
        """給模型看的任務描述。

        代辦說明只加在這裡，**不能寫進 self.task** —— task 會被拿去組搜尋網址，
        混進去會變成 ?q=在嘉義找診所%0A（這件事是幫「虫合」辦的…），搜出來的
        結果整個歪掉。
        """
        if not self.delegate_name:
            return self.task
        return (f"{self.task}\n（這件事是幫「{self.delegate_name}」辦的，"
                f"需要的個人資料要向他本人拿）")

    @property
    def participants(self) -> set[int]:
        """誰可以給資料、按確認：交代的人 + 被代辦的人。"""
        who = {self.user_id}
        if self.delegate_id:
            who.add(self.delegate_id)
        return who

    @property
    def ask_target(self) -> int:
        """要資料時該問誰 —— 有代辦對象就問他本人。"""
        return self.delegate_id or self.user_id

    @property
    def idle_for(self) -> float:
        return time.time() - self.touched

    # ── 畫面 ──

    async def _shot(self, *, marked: bool = False) -> bytes | None:
        """截圖。marked=True 是給模型看的（畫編號），False 是給使用者看的。"""
        try:
            if marked:
                await self._page.evaluate(_MARK_JS)
            try:
                return await self._page.screenshot(type="png")
            finally:
                if marked:
                    await self._page.evaluate(_UNMARK_JS)
        except Exception as e:  # noqa: BLE001
            # 換頁到一半截圖會失敗。等一下重試一次，還是不行就記 warning ——
            # 壓成 debug 的話使用者只會看到「畫面截圖如上」卻沒有圖，查不出原因
            try:
                await self._page.wait_for_timeout(600)
                return await self._page.screenshot(type="png")
            except Exception as e2:  # noqa: BLE001
                logger.warning("截圖失敗（重試也失敗）：%s / %s",
                               str(e)[:80], str(e2)[:80])
                return None

    async def _settle(self) -> None:
        """等頁面安定下來。

        點完按鈕的瞬間常常正在換頁，這時候對舊的 document 下 evaluate 會炸
        （Execution context was destroyed）—— 送出表單後第一次觀察最容易中。
        """
        for state in ("domcontentloaded", "load"):
            try:
                await self._page.wait_for_load_state(state, timeout=6000)
            except Exception:  # noqa: BLE001
                pass

    async def _observe(self, *, retry: bool = True) -> tuple[list[dict], str, str, bytes | None]:
        """看一眼現在的畫面：可操作元素、頁面文字、標題、（標了編號的）截圖。"""
        await self._settle()
        try:
            elements = await self._page.evaluate(_COLLECT_JS, config.BROWSER_MAX_ELEMENTS)
            try:
                text = await self._page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
            except Exception:  # noqa: BLE001
                text = ""
            title = await self._page.title()
            # 捲動位置：模型只看得到視窗內的截圖，要讓它知道下面還有多少沒看
            scroll = await self._page.evaluate(
                "() => ({y: window.scrollY, h: innerHeight,"
                " total: document.body ? document.body.scrollHeight : 0})")
        except PWError as e:
            # 換頁換到一半 → 等它換完再看一次
            if retry:
                logger.debug("觀察畫面時頁面正在換，等一下重試：%s", str(e)[:80])
                await asyncio.sleep(1.2)
                return await self._observe(retry=False)
            raise

        text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())[:config.BROWSER_PAGE_TEXT_CHARS]

        # 順手留一張「沒畫編號」的乾淨截圖給進度查詢用。
        # 中途被問進度時直接拿這張 —— 不能在跑到一半時另外去截圖，
        # 那會和主迴圈同時操作同一個 page，可能撞在換頁的瞬間。
        self.last_shot = await self._shot()
        self.last_title = title
        if text:
            self.last_text = text

        # 內容指紋：用來判斷「剛剛那個動作有沒有讓畫面真的改變」。
        # 只比網址＋標題是不夠的 —— ASP.NET 的 postback（慈濟、長庚都是）
        # 換頁後網址標題完全一樣，害我把有效的操作誤判成「沒反應」然後停手。
        self.page_sig = _sig(self._page.url, title, text, len(elements))

        y, h, total = scroll.get("y", 0), scroll.get("h", 0), scroll.get("total", 0)
        if total > h + 20:
            below = max(0, total - y - h)
            pos = (f"（畫面在整頁的 {int(y / max(total - h, 1) * 100)}%，"
                   f"下面還有約 {int(below)} px 沒看到 —— 找不到東西就往下捲）")
        else:
            pos = "（整頁都看得到，不用捲動）"
        title = f"{title}\n捲動位置：{pos}"

        return elements, text, title, await self._shot(marked=True)

    async def _pump_frames(self, on_frame, stop: asyncio.Event) -> None:
        """等模型想下一步的時候持續截圖，做成「慢速直播」。

        刻意只在**等待模型**的空檔跑：那段時間瀏覽器是閒著的，
        不會和正在進行的點擊／填字搶同一個 page。
        """
        if not (on_frame and config.BROWSER_LIVE):
            return
        if self.sensitive and not self.private_channel:
            return                      # 畫面上有個資，公開頻道不播
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.BROWSER_LIVE_INTERVAL)
                return                  # 被叫停了
            except asyncio.TimeoutError:
                pass
            try:
                shot = await self._page.screenshot(type="png")
            except Exception:  # noqa: BLE001
                continue                # 換頁中截不到，下一輪再試
            self.last_shot = shot
            try:
                await on_frame(shot)
            except Exception as e:  # noqa: BLE001
                logger.debug("送即時畫面失敗（忽略）：%s", e)

    async def _refresh_sig(self) -> None:
        """重算內容指紋（不截圖、不收元素，很輕）。"""
        text = await self._page.evaluate(
            "() => document.body ? document.body.innerText : ''") or ""
        n = await self._page.evaluate(
            "() => document.querySelectorAll('a,button,input,select,textarea').length")
        title = await self._page.title()
        self.page_sig = _sig(self._page.url, title, text, n)

    async def human_click(self, x: float, y: float) -> str:
        """人在畫面上點一下。座標是 viewport 座標（前端從縮放後的圖換算好）。"""
        if self._page is None:
            return "瀏覽器還沒開"
        self.touched = time.time()   # 人在動它，別讓閒置回收把它收掉
        x = max(0, min(float(x), config.BROWSER_WIDTH - 1))
        y = max(0, min(float(y), config.BROWSER_HEIGHT - 1))
        await self._page.mouse.click(x, y)
        await self._settle()
        note = await self._follow_new_tab()
        ok, why = _domain_ok(self._page.url)
        if not ok:
            return f"點完跑到不該去的地方（{why}）"
        return f"你點了 ({int(x)}, {int(y)}){note}"

    async def human_type(self, text: str, enter: bool = False) -> str:
        """人打字送到網頁（打在目前有焦點的欄位）。

        這條路**不過 _value_allowed 那道個資檢查** —— 那道是防「模型自己編身分證」，
        現在是本人親手打自己的資料，不需要也不該擋。
        """
        if self._page is None:
            return "瀏覽器還沒開"
        self.touched = time.time()   # 人在動它，別讓閒置回收把它收掉

        # 打字是打進「目前有焦點的元素」。剛載完的頁面焦點在 body 上，這時候打字
        # 會安靜地掉到地上 —— 人只會看到「我打了字但畫面什麼都沒變」，以為壞了。
        # 所以先確認有沒有選到欄位，沒選到就直接講。
        try:
            focus = await self._page.evaluate(
                "() => { const e = document.activeElement;"
                " return e ? e.tagName.toLowerCase() : ''; }")
        except Exception:  # noqa: BLE001
            focus = ""
        if focus in ("", "body", "html"):
            return "畫面上沒有選到輸入框 —— 先點一下你要打字的那個欄位，再打字"

        await self._page.keyboard.type(text, delay=15)
        if enter:
            await self._page.keyboard.press("Enter")
            await self._settle()
        if any(pat.search(text) for pat in _PII_PATTERNS):
            self.sensitive = True       # 畫面上出現個資了
        return f"你打了 {_mask(text)}" + ("（並按了 Enter）" if enter else "")

    async def human_key(self, key: str) -> str:
        """送一個按鍵（Enter / Tab / Backspace / Escape…）。"""
        if self._page is None:
            return "瀏覽器還沒開"
        self.touched = time.time()   # 人在動它，別讓閒置回收把它收掉
        allowed = {"Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown",
                   "ArrowLeft", "ArrowRight", "Delete", "Home", "End"}
        if key not in allowed:
            return f"不支援這個按鍵：{key}"
        await self._page.keyboard.press(key)
        await self._settle()
        return f"你按了 {key}"

    async def human_scroll(self, dy: float) -> str:
        if self._page is None:
            return "瀏覽器還沒開"
        self.touched = time.time()   # 人在動它，別讓閒置回收把它收掉
        await self._page.evaluate("(d) => window.scrollBy(0, d)", float(dy))
        await self._page.wait_for_timeout(120)
        return "你捲動了畫面"

    async def live_shot(self) -> bytes | None:
        """給「連續畫面」用的一張 JPEG。

        和 fresh_shot 分開的兩個理由：

        1. **格式**：這條路一秒要送好幾張，PNG 太大。JPEG 品質降一點換頻寬，
           但 last_shot（給 Discord 的截圖、事後回顧用）維持 PNG 不動。
        2. **共用**：好幾個人同時在看的時候不該各自截一張 —— 截圖是 50ms 級的
           操作，三個觀眾就變成三倍負擔。所以剛截過的直接重用。

        重用的時間窗故意設成「兩幀之間的間隔一半」：夠短，不會讓人看到延遲；
        夠長，足以讓同一輪的觀眾共用同一張。
        """
        now = time.time()
        window = 0.5 / max(config.ACTIVITY_STREAM_FPS, 1)
        if self.live_frame is not None and now - self.live_frame_at < window:
            return self.live_frame

        async with self._live_lock:
            # 等鎖的期間別人可能已經截好了
            now = time.time()
            if self.live_frame is not None and now - self.live_frame_at < window:
                return self.live_frame
            if self._page is None or self._page.is_closed():
                return None
            try:
                shot = await self._page.screenshot(
                    type="jpeg", quality=config.ACTIVITY_STREAM_QUALITY)
            except Exception as e:  # noqa: BLE001
                # 換頁中截不到很正常，下一幀再說 —— 不要每次都寫 log 洗版
                logger.debug("即時畫面截不到（忽略）：%s", e)
                return self.live_frame
            self.live_frame = shot
            self.live_frame_at = time.time()
            self.touched = self.live_frame_at      # 有人在看＝這個 session 還活著
            return shot

    async def fresh_shot(self) -> bytes | None:
        """立刻拍一張。只在模型沒有在動這個 page 的時候用（人接手中、或做完留著）。"""
        self.touched = time.time()      # 有人在看＝還活著
        shot = await self._shot()
        if shot:
            self.last_shot = shot
            self.last_shot_at = time.time()
        return shot

    def snapshot(self) -> dict:
        """現在的進度。中途查詢用，完全不碰瀏覽器，所以不會和主迴圈打架。"""
        return {
            "task": self.task,
            "step_no": len(self.steps),
            "max_steps": config.BROWSER_MAX_STEPS,
            "elapsed": int(time.time() - self.started),
            "url": self._page.url if self._page else "",
            "title": self.last_title,
            "steps": list(self.steps),
            "shot": self.last_shot,
            "running": self.running,
            "awaiting": self.awaiting,
            # 做完了但畫面還留著（她不再自己動，人可以接著自己點）
            "lingering": self.lingering,
        }

    # ── 動作 ──

    async def _goto(self, url: str) -> tuple[bool, str]:
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        ok, why = _domain_ok(url)
        if not ok:
            return False, why
        try:
            await self._page.goto(url, wait_until="domcontentloaded",
                                  timeout=config.BROWSER_STEP_TIMEOUT_MS)
        except PWError as e:
            return False, f"打不開這一頁：{str(e)[:120]}"
        # 轉址後可能已經在別的網域 —— 落地位址要再驗一次
        landed = self._page.url
        ok, why = _domain_ok(landed)
        if not ok:
            try:
                await self._page.goto("about:blank")
            except Exception:  # noqa: BLE001
                pass
            return False, f"這一頁把我導去不該去的地方（{why}）"
        return True, landed

    def _sel(self, idx: int) -> str:
        return f'[data-nana-idx="{int(idx)}"]'

    async def _do(self, act: dict, elements: list[dict]) -> tuple[bool, str]:
        """執行一個動作。回傳 (成功, 說明)。"""
        kind = act.get("action")
        idx = act.get("index")
        el = next((e for e in elements if e["i"] == idx), None) if idx is not None else None

        if kind == "click":
            if el is None:
                return False, f"找不到編號 {idx} 的元素"
            before = self._page.url
            try:
                await self._page.click(self._sel(idx), timeout=config.BROWSER_STEP_TIMEOUT_MS)
            except PWError as e:
                return False, f"點不到「{el['label']}」：{str(e)[:100]}"
            await self._settle()      # 點下去很可能在換頁，先等它換完
            note = await self._follow_new_tab()    # target=_blank 的話跟過去
            ok, why = _domain_ok(self._page.url)
            if not ok:
                return False, f"點完之後跑到不該去的地方（{why}）"
            if self._page.url != before:
                self._typed_here = 0               # 換頁了，重新算填過幾個欄位
            return True, f"點了「{el['label'] or el['tag']}」{note}"

        if kind == "type":
            if el is None:
                return False, f"找不到編號 {idx} 的欄位"
            value = str(act.get("text") or "")
            allowed, why = _value_allowed(value, self.task)
            if not allowed:
                return False, why
            try:
                await self._page.fill(self._sel(idx), value,
                                      timeout=config.BROWSER_STEP_TIMEOUT_MS)
            except PWError as e:
                return False, f"填不進「{el['label']}」：{str(e)[:100]}"
            self._typed_here += 1
            if any(pat.search(value) for pat in _PII_PATTERNS):
                self.sensitive = True
            return True, f"在「{el['label'] or '欄位'}」填了 {_mask(value)}"

        if kind == "select":
            if el is None:
                return False, f"找不到編號 {idx} 的選單"
            value = str(act.get("text") or "")
            try:
                await self._page.select_option(self._sel(idx), label=value,
                                               timeout=config.BROWSER_STEP_TIMEOUT_MS)
            except PWError:
                try:
                    await self._page.select_option(self._sel(idx), value=value)
                except PWError as e:
                    return False, f"選不到「{value}」：{str(e)[:100]}"
            # 有些網站的下拉選單 onchange 會直接換頁或開新視窗（長庚的院區選單就是），
            # 不等它、不跟過去的話畫面看起來完全沒變，模型就會一直重選同一個。
            await self._settle()
            note = await self._follow_new_tab()
            self._typed_here += 1
            return True, f"在「{el['label'] or '選單'}」選了「{value}」{note}"

        if kind == "scroll":
            # 有指定元素就直接捲到它 —— 比盲捲可靠，長表單especially
            if el is not None:
                try:
                    await self._page.locator(self._sel(idx)).scroll_into_view_if_needed(
                        timeout=config.BROWSER_STEP_TIMEOUT_MS)
                    return True, f"捲到「{el['label'] or el['tag']}」"
                except PWError:
                    pass
            where = str(act.get("text") or "down").lower()

            # 一律用 window.scroll* 而不是 mouse.wheel。
            # mouse.wheel 只「派送滾輪事件」、不等它捲完，下一步讀到的還是舊位置
            # —— 實測連按兩次 down 第二次看起來完全沒動，模型就會以為捲不動、
            # 一直重複同一個動作直到撞牆停手。
            before = await self._page.evaluate("() => Math.round(window.scrollY)")
            if where in ("top", "頂端", "最上面"):
                await self._page.evaluate("() => window.scrollTo(0, 0)")
                label = "捲到最上面"
            elif where in ("bottom", "底部", "最下面"):
                await self._page.evaluate(
                    "() => window.scrollTo(0, document.body.scrollHeight)")
                label = "捲到最下面"
            else:
                down = where != "up"
                await self._page.evaluate(
                    "(dy) => window.scrollBy(0, dy)", 700 if down else -700)
                label = "往下捲" if down else "往上捲"

            await self._page.wait_for_timeout(250)      # 讓平滑捲動跑完
            after = await self._page.evaluate("() => Math.round(window.scrollY)")
            if after == before:
                # 明確講出來，模型才知道要換做法而不是再捲一次
                edge = "最下面" if where not in ("up", "top", "頂端", "最上面") else "最上面"
                return True, f"{label}：已經在{edge}了，捲不動（別再捲，改用別的方法）"
            return True, f"{label}（位置 {before} → {after}）"

        if kind == "goto":
            ok, info = await self._goto(str(act.get("url") or ""))
            return ok, (f"開了 {info}" if ok else info)

        if kind == "back":
            try:
                await self._page.go_back(timeout=config.BROWSER_STEP_TIMEOUT_MS)
            except PWError as e:
                return False, f"回不去上一頁：{str(e)[:80]}"
            return True, "回上一頁"

        if kind == "wait":
            await asyncio.sleep(min(float(act.get("seconds") or 2), 5))
            return True, "等一下讓頁面載完"

        return False, f"不認得的動作：{kind}"

    # ── 主迴圈 ──

    async def run(self, *, start_url: str | None = None,
                  on_step=None) -> Result:
        """跑到做完、或停在需要人介入的地方。"""
        try:
            await self._ensure()
        except Exception as e:  # noqa: BLE001
            logger.warning("瀏覽器起不來：%s", e)
            return Result(status="error", summary=f"瀏覽器起不來：{str(e)[:150]}")

        # 任務描述裡本來就有網址 → 直接開，不要拿去搜。
        # 模型偶爾會忘記把使用者貼的網址填進 url 欄位，那時候「先去搜」這條後路
        # 會變成「把網址當關鍵字搜」—— 使用者明明已經指定要看哪一頁了。
        if not start_url:
            found = webfetch.find_urls(self.task)
            if found:
                start_url = found[0]
                logger.info("🖥️ 從任務內容裡找到網址，直接開：%s", start_url)

        if not start_url and config.BROWSER_SEARCH_URL:
            # 停在 about:blank 的話畫面上什麼都沒有，模型只能憑空猜網址。
            # 先用任務內容去搜，讓它有一頁真的連結可以下手。
            from urllib.parse import quote_plus
            query = search_query(self.task, self.search_hint)
            start_url = config.BROWSER_SEARCH_URL.format(q=quote_plus(query))
            logger.info("🖥️ 沒給網址，先搜「%s」", query)

        if start_url:
            ok, info = await self._goto(start_url)
            if not ok:
                return Result(status="blocked", summary=info)
            self._note("goto", f"開了 {info}")
            await self._tell(on_step)

        return await self._loop(on_step=on_step)

    async def resume(self, *, confirmed: bool, extra: str = "",
                     always: bool = False, on_step=None) -> Result:
        """使用者按了確認／取消／一律允許，或補了資料之後繼續。"""
        self.touched = time.time()
        self.awaiting = None
        if always:
            self.auto_confirm = True
            logger.info("🖥️ 使用者選了「一律允許」，之後的送出不再逐一詢問")
        if extra:
            self.task = f"{self.task}\n（使用者補充：{extra}）"

        if self._pending is not None:
            act, elements = self._pending["act"], self._pending["elements"]
            self._pending = None
            if not confirmed:
                self._note("cancel", "使用者說不要送出，停手")
                shot = await self._shot()
                return Result(status="done", url=self._page.url, shot=shot,
                              steps=self.steps, sensitive=self.sensitive, page_text=self.last_text,
                              summary="好，那我就沒有送出去，停在這裡了。")
            ok, detail = await self._do(act, elements)
            self._note(act.get("action", "?"), detail if ok else f"失敗：{detail}")
        return await self._loop(on_step=on_step)

    async def _tell(self, on_step) -> None:
        """通知外面「這一步做完了」，順便附上做完之後的畫面。

        截圖是在動作**之後**拍的 —— 使用者要看的是這一步造成了什麼結果。
        整段包 try：回報進度壞掉不能影響任務本身。
        """
        if not on_step or not self.steps:
            return
        try:
            want = config.BROWSER_STEP_SHOTS
            if self.sensitive and not self.private_channel:
                want = False        # 畫面上已經有個資了，公開頻道不留也不貼
            shot = await self._shot() if want else None
            if shot and len(self.shots) < config.BROWSER_MAX_KEPT_SHOTS:
                self.shots[self.steps[-1].n] = shot
            await on_step(self.steps[-1], shot)
        except Exception as e:  # noqa: BLE001
            logger.debug("回報單步進度失敗（忽略）：%s", e)

    def _note(self, action: str, detail: str) -> None:
        self.steps.append(Step(len(self.steps) + 1, action, detail,
                               self._page.url if self._page else ""))
        self._history.append(f"{len(self.steps)}. {detail}")
        logger.info("🖥️ 第 %d 步 │ %s │ %s", len(self.steps), action, detail)

    async def _loop(self, *, on_step=None) -> Result:
        self.running = True
        try:
            return await self._loop_inner(on_step=on_step)
        finally:
            self.running = False

    async def _loop_inner(self, *, on_step=None) -> Result:
        while True:
            self.touched = time.time()

            # 人接手了 → 停在這裡等他交還。暫停的時間不算進總時限，
            # 不然他慢慢填一個表單就把任務逾時掉了。
            if self.paused:
                # 等的期間也要繼續播。原本這個迴圈裡沒有直播，頻道那邊的畫面會停在
                # 他按下「我來操作」前的最後一張 —— 他自己在操作台點得好好的，
                # 頻道裡的人卻看到畫面完全不動，跟卡死長得一樣。
                stop_pause = asyncio.Event()
                pause_pump = asyncio.create_task(
                    self._pump_frames(self._on_frame, stop_pause))
                try:
                    while self.paused:
                        await asyncio.sleep(1)
                        self.touched = time.time()
                        self.started += 1
                finally:
                    stop_pause.set()
                    try:
                        await pause_pump
                    except Exception:  # noqa: BLE001
                        pass

            # 步數上限 0 = 不限（預設）。時間、撞牆偵測、連續失敗那三道還在。
            if 0 < config.BROWSER_MAX_STEPS <= len(self.steps):
                return Result(status="max_steps", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              summary="步驟用完了還沒做完，先把畫面拍給你看。")
            if time.time() - self.started > config.BROWSER_MAX_SECONDS:
                mins = config.BROWSER_MAX_SECONDS // 60
                return Result(status="max_steps", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              summary=(f"已經弄了 {mins} 分鐘還沒完成，先停下來把畫面拍給你看，"
                                       f"免得一直卡在這裡。"))

            try:
                elements, text, title, marked = await self._observe()
            except Exception as e:  # noqa: BLE001
                logger.warning("看畫面失敗：%s", e)
                # 讀不到內容也要把畫面拍給他 —— 通常畫面本身是好的，
                # 使用者看一眼截圖就知道進行到哪了
                return Result(status="error", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              summary=f"讀不到畫面內容：{str(e)[:120]}")

            stop = asyncio.Event()
            pump = asyncio.create_task(self._pump_frames(self._on_frame, stop))
            try:
                act = await llm_client.decide_browser_action(
                    task=self.task_for_model, url=self._page.url, title=title,
                    page_text=text, elements=elements,
                    history=self._history[-8:], shot=marked,
                )
            finally:
                stop.set()
                try:
                    await pump
                except Exception:  # noqa: BLE001
                    pass
            if not act:
                return Result(status="error", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              summary="我看著這一頁但想不出下一步，先把畫面拍給你。")

            kind = act.get("action")

            if kind == "done":
                return Result(status="done", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              summary=str(act.get("summary") or "做完了"))
            if kind == "blocked":
                return Result(status="blocked", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              summary=str(act.get("summary") or act.get("reason") or "做不下去"))
            if kind == "ask":
                self.awaiting = "input"
                return Result(status="need_input", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              question=str(act.get("question") or act.get("summary")
                                           or "我需要一點資料才能繼續，可以給我嗎？"),
                              summary=str(act.get("reason") or ""))

            # 要按的東西看起來會造成不可逆的結果 → 停下來等本人確認
            idx = act.get("index")
            el = next((e for e in elements if e["i"] == idx), None) if idx is not None else None
            if (kind == "click" and el and not self.auto_confirm
                    and _looks_irreversible(
                        el["label"], el["tag"], el["type"], typed=self._typed_here)):
                self._pending = {"act": act, "elements": elements}
                self.awaiting = "confirm"
                logger.info("🖥️ 停在確認點：%s", el["label"])
                return Result(
                    status="need_confirm", url=self._page.url,
                    shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                    question=f"我準備按「{el['label'] or '送出'}」了，這一步送出去就不能反悔，"
                             f"要我按下去嗎？",
                    summary=str(act.get("reason") or ""),
                )

            before_sig = self.page_sig

            ok, detail = await self._do(act, elements)

            # 撞牆偵測：同一個動作重複做、畫面卻沒有變化 → 別再撞
            # （實測台大醫院首頁就是這樣：目標在畫面下方，它看不到，就一直點同一個連結）
            key = f"{kind}:{idx}:{(el or {}).get('label', '')}"
            # 重新取一次指紋來比對（_do 之後畫面可能還在換）
            try:
                await self._settle()
                await self._refresh_sig()
            except Exception as e:  # noqa: BLE001
                # 算不出來就當「有變化」。寧可多做一步，也不要把還在換頁的畫面
                # 誤判成「沒反應」然後把整個任務停掉。
                logger.debug("取指紋失敗，當成有變化：%s", str(e)[:80])
                self.page_sig = f"?{time.time()}"
            same_page = (self.page_sig == before_sig)
            if same_page and ok:
                self._stuck[key] = self._stuck.get(key, 0) + 1
            else:
                self._stuck.clear()

            hits = self._stuck.get(key, 0)
            if hits >= config.BROWSER_STUCK_LIMIT:
                logger.info("🖥️ 同一個動作做了 %d 次都沒反應，停手", hits)
                return Result(
                    status="blocked", url=self._page.url,
                    shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                    summary=(f"我一直點「{(el or {}).get('label', '同一個東西')}」但畫面都沒變，"
                             f"再點下去只是白費工，所以停下來。"
                             f"把畫面拍給你看，你看看是不是要換個入口？"))
            if hits:
                detail += ("（畫面沒變）" if hits == 1
                           else f"（畫面沒變，同一招第 {hits} 次）")

            self._note(kind or "?", detail if ok else f"失敗：{detail}")
            if hits:
                # 明確寫進歷史，讓模型下一步知道要換做法而不是再點一次
                self._history[-1] += (
                    "  ← 這個點了沒反應，畫面完全沒變。不要再點同一個！"
                    "改成往下捲（scroll）看看下面有什麼，或換別的元素。")
            await self._tell(on_step)
            if not ok and len(self.steps) >= 3 and all(
                    s.detail.startswith("失敗") for s in self.steps[-3:]):
                return Result(status="error", url=self._page.url,
                              shot=await self._shot(), steps=self.steps,
                              sensitive=self.sensitive, page_text=self.last_text,
                              summary="連續幾步都做不動，先停下來把畫面拍給你看。")


# ── session 管理 ───────────────────────────────────────

_sessions: dict[int, Session] = {}
_lock = asyncio.Lock()

# 最近做過的任務：user_id → {task, status, summary, when}
# 「換一間」「再試一次」這種跟進的話本身沒有資訊，要靠這個才知道在講什麼。
_recent: dict[int, dict] = {}


def recent_task(user_id: int, within: int = 1800) -> dict | None:
    """這個人最近（預設 30 分鐘內）做過的瀏覽任務。沒有就回 None。"""
    r = _recent.get(user_id)
    if not r or time.time() - r["when"] > within:
        return None
    return r


async def _retire(key: int, s: Session, why: str) -> None:
    """真的把一個 session 收掉：從表裡拿掉、關瀏覽器、通知呼叫端。

    錄影是 close() 之後才寫完的，所以「影片可以拿了」只有這裡知道 ——
    任務做完但畫面留著的情況，影片不是在任務結束時送出，而是在這裡。
    """
    _sessions.pop(key, None)
    await s.close()
    logger.info("🖥️ 收掉 session（user=%d，%s）", key, why)
    cb = s._on_closed
    if cb is not None:
        try:
            await cb(key, s.video_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("session 收掉後的回呼失敗：%s", e)


def _linger_expired(s: Session) -> str:
    """留著的畫面該不該收了。回傳原因字串，還不用收就回空字串。

    「沒人在看就收」是刻意的：操作台每 1~2 秒會打一次 API，所以 watched_at 一停
    就代表面板關了或斷線了。人都走了還留著一個 Chromium 只是在燒記憶體。
    """
    if not s.lingering:
        return ""
    now = time.time()
    if now - s.linger_since > config.BROWSER_LINGER_MAX_S:
        return f"留著超過 {config.BROWSER_LINGER_MAX_S // 60} 分鐘上限"
    # 剛做完給一段寬限 —— 任務是在聊天室發動的話，人要幾秒才點開操作台
    if now - s.linger_since < config.BROWSER_LINGER_GRACE_S:
        return ""
    if now - s.watched_at > config.BROWSER_LINGER_WATCH_TIMEOUT_S:
        return "沒有人在看了（面板關掉或斷線）"
    return ""


async def _sweep() -> None:
    """收掉閒置太久的 session —— 每個都是一個真的 Chromium，不收會吃光記憶體。"""
    for uid, s in list(_sessions.items()):
        why = _linger_expired(s)
        if why:
            await _retire(uid, s, why)
        elif s.idle_for > config.BROWSER_SESSION_IDLE_S:
            await _retire(uid, s, f"閒置 {s.idle_for:.0f} 秒")


async def reaper() -> None:
    """定時檢查該收的 session。

    非常必要：留著的畫面要靠「沒人在看」才收得掉，而那個判斷需要有人定期去看。
    在這之前 _sweep 只在「有人開新任務」時才跑，留著的 Chromium 會活到下一次
    有人用瀏覽器為止。
    """
    while True:
        try:
            await asyncio.sleep(config.BROWSER_REAP_INTERVAL_S)
            async with _lock:
                await _sweep()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("session 回收巡邏出錯（繼續跑）：%s", e)


def mark_watched(user_id: int) -> None:
    """有人正在操作台上看這個人的 session（activity.py 每次收到請求都會叫）。"""
    _, s = _find(user_id)
    if s is not None:
        s.watched_at = time.time()


def pending_for(user_id: int) -> Session | None:
    """這個人有沒有停在等確認的 session。"""
    s = _sessions.get(user_id)
    return s if s and s.idle_for <= config.BROWSER_SESSION_IDLE_S else None


def _find(user_id: int) -> tuple[int, Session] | tuple[None, None]:
    """從「參與者」找出 session（交代的人或被代辦的人都算）。

    回傳 (session 的 key, session)。key 是交代者的 id，不一定等於傳進來的 user_id
    —— 被代辦的人回話時就是這種情況。
    """
    s = _sessions.get(user_id)
    if s is not None:
        return user_id, s
    for key, sess in _sessions.items():
        if user_id in sess.participants:
            return key, sess
    return None, None


def progress_for(user_id: int) -> dict | None:
    """這個人現在有沒有在跑的瀏覽任務；有的話回傳進度快照。

    只讀記錄和快取的截圖，不對瀏覽器下任何指令 —— 中途查進度不能干擾正在做的事。
    """
    _key, s = _find(user_id)
    if s is None:
        return None
    if not (s.running or s.awaiting):
        return None
    return s.snapshot()


def awaiting_input(user_id: int) -> Session | None:
    """有沒有任務正卡在「等資料」、而且**這個人有資格回答**。

    交代的人和被代辦的人都算 —— 「幫 @虫合 掛號」時要的是虫合的身分證，
    所以虫合講的那句話也要能接回這個任務。
    """
    _key, s = _find(user_id)
    if s is None or s.awaiting != "input":
        return None
    return s if s.idle_for <= config.BROWSER_SESSION_IDLE_S else None


async def start(user_id: int, task: str, *, start_url: str | None = None,
                delegate_id: int | None = None, delegate_name: str = "",
                private_channel: bool = False, search_hint: str = "",
                on_step=None, on_frame=None, on_closed=None) -> Result:
    """開一個新任務。同一個人同時只能有一個。"""
    if not config.BROWSER_ENABLED:
        return Result(status="blocked", summary="瀏覽器操作功能目前關閉。")
    if not AVAILABLE:
        return Result(status="error",
                      summary="這台機器還沒裝好 Playwright，我沒辦法開瀏覽器。")

    async with _lock:
        await _sweep()
        old = _sessions.get(user_id)
        if old:
            # 可能是上一趟留著的畫面，它的錄影還沒送出去 —— 要走 _retire
            await _retire(user_id, old, "換新任務")
        if len(_sessions) >= config.BROWSER_MAX_SESSIONS:
            return Result(status="blocked",
                          summary="現在同時有太多人在用瀏覽器了，等一下再試好嗎？")
        s = Session(user_id, task, delegate_id=delegate_id,
                    delegate_name=delegate_name)
        s.private_channel = private_channel
        s.search_hint = search_hint
        s._on_frame = on_frame
        # 畫面留著的時候，錄影要等真的收掉才送得出去 —— 見 _retire
        s._on_closed = on_closed
        _sessions[user_id] = s

    try:
        result = await s.run(start_url=start_url, on_step=on_step)
    except Exception as e:  # noqa: BLE001
        logger.warning("瀏覽任務爆掉：%s", e)
        result = Result(status="error", summary=f"中途出錯了：{str(e)[:150]}")

    if result.finished:
        _recent[user_id] = {"task": s.task, "status": result.status,
                            "summary": result.summary, "when": time.time(),
                            "url": result.url, "text": s.last_text}
        await _finish(user_id, s, result)
    return result


async def _finish(key: int, s: Session, result: Result) -> None:
    """任務結束後：把畫面留著，或直接收掉。

    留著的理由：她做完的東西人常常還想繼續用 —— 「幫我開 YouTube」開完就關掉，
    等於什麼都沒得看。所以做完之後 Chromium 不關，人可以在操作台接著自己點。
    她不再自己動了（paused），所以不會跟人搶同一個 page。

    留不留得看有沒有頁面可看；沒開起來過（blocked／一開始就爆掉）就沒意義。
    收掉的時機交給 reaper：沒人在看就收（見 _linger_expired）。
    """
    can_linger = (config.BROWSER_LINGER and s._page is not None
                  and not s._page.is_closed())
    if not can_linger:
        async with _lock:
            await _retire(key, s, f"任務結束（{result.status}）")
        result.video = s.video_path       # close() 之後影片才寫完
        return

    s.lingering = True
    s.paused = True                       # 換人操作 —— 她不再自己動
    s.linger_since = time.time()
    s.touched = time.time()
    result.linger = True
    # result.video 這時候還拿不到（影片要等 close），改由 _retire 的回呼補送
    logger.info("🖥️ 任務結束但畫面留著 │ user=%d │ 沒人看就收（寬限 %d 秒）",
                key, config.BROWSER_LINGER_GRACE_S)


async def resume(user_id: int, *, confirmed: bool, extra: str = "",
                 always: bool = False, on_step=None, on_frame=None) -> Result:
    """接續一個停在確認點／等補資料的任務。"""
    key, s = _find(user_id)
    if s is None:
        return Result(status="error", summary="那個任務已經過期了，要不要重新跟我說一次？")
    if on_frame is not None:
        s._on_frame = on_frame

    try:
        result = await s.resume(confirmed=confirmed, extra=extra,
                                always=always, on_step=on_step)
    except Exception as e:  # noqa: BLE001
        logger.warning("接續瀏覽任務爆掉：%s", e)
        result = Result(status="error", summary=f"中途出錯了：{str(e)[:150]}")

    if result.finished:
        _recent[key] = {"task": s.task, "status": result.status,
                        "summary": result.summary, "when": time.time(),
                        "url": result.url, "text": s.last_text}
        await _finish(key, s, result)
    return result


async def shutdown() -> None:
    """關機／重啟時把所有瀏覽器收掉。"""
    for uid, s in list(_sessions.items()):
        await s.close()
        _sessions.pop(uid, None)


def cleanup_video(path: str | None) -> None:
    """傳完影片就把暫存目錄刪掉 —— 不清的話 /tmp 會一直長。"""
    if not path:
        return
    try:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("清錄影檔失敗（忽略）：%s", e)


async def stop(user_id: int) -> bool:
    """使用者要求中止 —— 把他的 session 收掉（Activity 的 ⏹ 用這個）。"""
    key, sess = _find(user_id)
    if sess is None:
        return False
    logger.info("🖥️ 使用者%s │ user=%d │ 已做 %d 步",
                "收起留著的畫面" if sess.lingering else "中止任務", key, len(sess.steps))
    # 已經做完、只是畫面留著的話，別把「做完了」的紀錄改寫成「被中止」——
    # 那筆紀錄是事後追問（「剛剛那個 XX 有什麼特點」）用的
    if not sess.lingering:
        _recent[key] = {"task": sess.task, "status": "stopped",
                        "summary": "（你要我停下來了）", "when": time.time(),
                        "url": "", "text": sess.last_text}
    async with _lock:
        await _retire(key, sess, "使用者按了停止")
    return True
