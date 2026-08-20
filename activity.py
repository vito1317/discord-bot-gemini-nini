"""
奈奈的 Discord Activity —— 語音頻道裡的操作台 🎛️

Discord 不讓機器人開螢幕分享（見 browser.py 的說明），但 Activity 是官方允許的路：
語音頻道裡開一個「活動」，內容其實是一個嵌在 Discord 裡的網頁。
所以這支就是那個網頁的後端 —— 它跑在 bot 自己的 event loop 裡，
因此可以**直接讀 browser 的 session**（不用另外開 API 或共享狀態）。

裡面看得到／做得到：
  • 瀏覽器的即時畫面（每 1~2 秒換一張）
  • 她做過的每一步
  • ✅ 確認送出／✋ 取消／🏃 一律允許
  • 🔒 送資料給她（身分證、驗證碼）—— 打在 Activity 裡，不會出現在頻道
  • ⏹ 停止任務、▶ 直接交代新任務

## 授權（這裡最重要）
Activity 裡按一下就會送出真的掛號，所以**不能相信前端說自己是誰**。
流程照 Discord 官方的走：
  1. 前端用 SDK 拿 OAuth code（scope: identify）
  2. **後端**拿 code 去換 access token，再打 /users/@me 問出真正的 user id
  3. 後端簽一個 HMAC token 給前端，之後每次操作都帶著它
  4. 每個操作都再確認一次「這個人是這個任務的參與者」（交代的人或被代辦的人）
沒有 DISCORD_CLIENT_SECRET 就不啟動 —— 沒有它就無法驗身分，
那等於讓語音頻道裡任何人都能幫別人送出掛號。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx
from aiohttp import web

import browser
import config

logger = logging.getLogger("nana.activity")

_STATIC = Path(__file__).parent / "activity" / "static"
_SECRET = secrets.token_bytes(32)        # 每次重啟換一把 → 舊 token 自動失效
_runner: web.AppRunner | None = None

# 誰在看（給 /api/state 顯示觀眾數用）
_watchers: dict[int, float] = {}


# ── token ──────────────────────────────────────────────

def _sign(user_id: int, ttl: int = 21600, scope: str = "api") -> str:
    """簽一個短期 token（預設 6 小時）。

    scope 進的是 MAC 的輸入而不是 token 本體 —— 所以拿「只能看畫面」的票去打
    /api/confirm 會直接驗不過（MAC 對不上），不需要另外檢查它的用途。
    需要這個是因為 <img> 沒辦法帶 Authorization header，串流的憑證只能放在網址上，
    而網址會被寫進 nginx 的 access log。放一張 2 分鐘、只能看畫面的票進去，
    比把 6 小時、什麼都能做的 token 留在 log 裡安全得多。
    """
    exp = int(time.time()) + ttl
    body = f"{user_id}.{exp}"
    mac = hmac.new(_SECRET, f"{scope}:{body}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def _verify(token: str, scope: str = "api") -> int | None:
    """驗 token，回傳 user_id；壞了、過期、或用途不符都回 None。"""
    try:
        uid_s, exp_s, mac = (token or "").split(".")
        body = f"{uid_s}.{exp_s}"
        good = hmac.new(_SECRET, f"{scope}:{body}".encode(),
                        hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, good):
            return None
        if int(exp_s) < time.time():
            return None
        return int(uid_s)
    except (ValueError, AttributeError):
        return None


def _who(request: web.Request) -> int | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _verify(auth[7:])
    return None


def _need_auth(request: web.Request) -> int:
    uid = _who(request)
    if uid is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "請先在 Discord 裡授權"}),
                                   content_type="application/json")
    _watchers[uid] = time.time()
    # 任務做完之後畫面會留著，靠的就是這個「還有人在看」的訊號：面板每 1~2 秒
    # 打一次 API，一停就代表關掉或斷線了，reaper 會把留著的瀏覽器收掉。
    browser.mark_watched(uid)
    return uid


# ── 靜態頁 ─────────────────────────────────────────────

def _asset_version() -> str:
    """靜態檔的版號 = 檔案最後修改時間。

    index.html 自己是 no-store，但 sdk.js / app.js 走 aiohttp 的 static handler，
    Discord 的代理會把它們快取起來 —— 改完前端部署下去，客戶端還是跑舊版
    （實測發生過，害人以為改的東西沒效）。網址帶版號才會真的重抓。
    """
    try:
        return str(int(max((_STATIC / n).stat().st_mtime
                           for n in ("app.js", "sdk.js")
                           if (_STATIC / n).exists())))
    except ValueError:
        return "0"


async def _index(request: web.Request) -> web.StreamResponse:
    # 暫時：把進來的 header 全部記下來。要查的是 Discord 的請求帶了什麼
    # X-Forwarded-For（WAF 的 header_attack 偵測器說裡面有 localhost）。
    # 查完就把這段拿掉。
    if config.ACTIVITY_DEBUG_HEADERS:
        interesting = {k: v for k, v in request.headers.items()
                       if k.lower().startswith(("x-", "cf-", "true-", "forwarded"))}
        logger.info("🎛️ 進來的請求 %s %s │ headers=%s",
                    request.method, request.path_qs[:120], interesting)
    # 把 client_id 嵌進頁面。不能讓前端自己從 URL 讀 —— Discord 的 iframe 網址
    # 沒有 client_id，SDK 就會卡在 handshake（實測前端一直停在「連線中…」）。
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("__NANA_CLIENT_ID__", str(config.DISCORD_CLIENT_ID or ""))
    html = html.replace("__NANA_ASSET_V__", _asset_version())
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


# ── 前端回報（診斷用）──────────────────────────────────

_clientlog_window: list[float] = []


async def _clientlog(request: web.Request) -> web.Response:
    """讓 iframe 裡的頁面把錯誤跟載入進度打回來。

    Activity 是跑在 *.discordsays.com 的 iframe 裡，外面看不到它的 console ——
    「頁面一片黑」跟「JS 在第二行就掛了」從 Discord 那端長得一模一樣。之前查
    白畫面／黑畫面全靠猜（猜過 X-Frame-Options、client_id、redirect_uri…），
    所以開這條路讓前端自己講它卡在哪一步。

    刻意不要求授權 —— 授權失敗本身就是最需要看到的那一種錯誤。因此要防灌：
    每分鐘最多 60 筆、每筆截到 500 字。
    """
    now = time.time()
    _clientlog_window[:] = [t for t in _clientlog_window if now - t < 60]
    if len(_clientlog_window) >= 60:
        return web.Response(status=204)
    _clientlog_window.append(now)

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=204)

    stage = str(data.get("stage", "?"))[:40]
    msg = str(data.get("msg", ""))[:500]
    if data.get("bad"):
        logger.warning("🎛️ 前端出錯 │ %s │ %s", stage, msg)
    else:
        logger.info("🎛️ 前端進度 │ %s │ %s", stage, msg)
    return web.Response(status=204)


# ── OAuth ──────────────────────────────────────────────

async def _auth(request: web.Request) -> web.Response:
    """前端把 SDK 拿到的 code 送來，這裡換成真正的身分。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text='{"error":"bad json"}',
                                 content_type="application/json")
    code = str(data.get("code") or "")
    if not code:
        raise web.HTTPBadRequest(text='{"error":"缺少 code"}',
                                 content_type="application/json")

    secret = config.DISCORD_CLIENT_SECRET
    if not secret:
        raise web.HTTPServiceUnavailable(
            text='{"error":"伺服器沒有設定 DISCORD_CLIENT_SECRET，無法驗證身分"}',
            content_type="application/json")

    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://discord.com/api/oauth2/token", data={
            "client_id": str(config.DISCORD_CLIENT_ID),
            "client_secret": secret,
            "grant_type": "authorization_code",
            "code": code,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            logger.warning("OAuth 換 token 失敗：%s %s", r.status_code, r.text[:200])
            raise web.HTTPUnauthorized(text='{"error":"授權失敗"}',
                                       content_type="application/json")
        access = r.json().get("access_token")

        me = await c.get("https://discord.com/api/users/@me",
                         headers={"Authorization": f"Bearer {access}"})
        if me.status_code != 200:
            raise web.HTTPUnauthorized(text='{"error":"讀不到你的身分"}',
                                       content_type="application/json")
        u = me.json()

    uid = int(u["id"])
    logger.info("🎛️ Activity 授權：%s（%d）", u.get("username"), uid)
    return web.json_response({
        "token": _sign(uid),
        "user": {"id": str(uid), "name": u.get("global_name") or u.get("username")},
    })


# ── 任務狀態 ───────────────────────────────────────────

def _session_for(uid: int):
    """這個人能操作的 session（他交代的、或是幫他辦的）。"""
    _key, s = browser._find(uid)
    return s


async def _ticket(request: web.Request) -> web.Response:
    """換一張只能看畫面的短期票（給 <img src> 用，見 _sign 的說明）。"""
    uid = _need_auth(request)
    return web.json_response({"ticket": _sign(uid, ttl=120, scope="stream")})


async def _state(request: web.Request) -> web.Response:
    uid = _need_auth(request)
    s = _session_for(uid)
    live = sum(1 for t in _watchers.values() if time.time() - t < 30)

    if s is None:
        return web.json_response({
            "active": False,
            "watchers": live,
            "hint": "現在沒有進行中的任務。可以在下面直接交代一件事給奈奈。",
        })

    return web.json_response({
        "active": True,
        "watchers": live,
        "task": s.task,
        "url": s._page.url if s._page else "",
        "title": s.last_title.split("\n")[0][:80],
        "running": s.running,
        "awaiting": s.awaiting or "",
        "owner": str(s.user_id),
        "delegate": str(s.delegate_id) if s.delegate_id else "",
        "you_can_act": uid in s.participants,
        "sensitive": s.sensitive,
        "paused": s.paused,
        "lingering": s.lingering,
        # 伺服器這邊看到的串流連線數。前端拿它當「我的串流還活著嗎」的真相來源 ——
        # <img> 的 onerror 在串流被中途掐斷時不一定會觸發（手機版實測就不會），
        # 光靠前端自己判斷會以為還在播，畫面就凍在最後一幀。
        "streaming": s.stream_viewers > 0,
        # 這個瀏覽器發不發得出聲音、現在有沒有接進語音頻道
        "audio_capable": s.audio_on,
        "audio_on": bool(_hooks.get("sound_state")
                         and _hooks["sound_state"](uid)),
        "viewport": [config.BROWSER_WIDTH, config.BROWSER_HEIGHT],
        "steps": [{"n": st.n, "action": st.action, "detail": st.detail}
                  for st in s.steps[-12:]],
        "frame_seq": len(s.steps) * 1000 + int(time.time()),
    })


async def _frame(request: web.Request) -> web.StreamResponse:
    """瀏覽器的即時畫面。前端就是一直重抓這個當「直播」。"""
    uid = _need_auth(request)
    s = _session_for(uid)
    if s is None:
        raise web.HTTPNotFound(text="no frame")
    if (s.paused or s.lingering) and time.time() - s.last_shot_at > 0.4:
        # 人正在操作、或任務做完畫面留著 → 拍即時的，讓他看得到自己點的結果
        # （這兩種情況模型都沒有在動這個 page，不會搶）
        # 0.4 秒的門檻是因為操作的回應裡已經帶了一張新的，緊接著的輪詢不用再拍。
        await s.fresh_shot()
    if not s.last_shot:
        raise web.HTTPNotFound(text="no frame")
    return web.Response(body=s.last_shot, content_type="image/png",
                        headers={"Cache-Control": "no-store"})


# ── 操作 ───────────────────────────────────────────────

async def _act_guard(request: web.Request):
    """回傳 (uid, session)；沒權限就擋掉。"""
    uid = _need_auth(request)
    s = _session_for(uid)
    if s is None:
        raise web.HTTPNotFound(text='{"error":"沒有進行中的任務"}',
                               content_type="application/json")
    if uid not in s.participants:
        raise web.HTTPForbidden(
            text='{"error":"這是別人的任務，只有當事人能操作"}',
            content_type="application/json")
    return uid, s


async def _confirm(request: web.Request) -> web.Response:
    uid, s = await _act_guard(request)
    data = await request.json()
    allow = bool(data.get("allow"))
    always = bool(data.get("always"))
    if s.awaiting != "confirm":
        return web.json_response({"ok": False, "msg": "現在沒有在等你確認"})

    logger.info("🎛️ Activity 確認送出：user=%d allow=%s always=%s", uid, allow, always)
    asyncio.create_task(_resume(uid, confirmed=allow, always=always))
    return web.json_response({"ok": True,
                              "msg": "好，我送出去了" if allow else "好，我不送"})


async def _input(request: web.Request) -> web.Response:
    uid, s = await _act_guard(request)
    data = await request.json()
    text = str(data.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "msg": "沒有內容"})
    if s.awaiting != "input":
        return web.json_response({"ok": False, "msg": "現在沒有在等你給資料"})

    logger.info("🎛️ Activity 收到資料：user=%d │ %d 字", uid, len(text))
    asyncio.create_task(_resume(uid, confirmed=True, extra=text))
    return web.json_response({"ok": True, "msg": "收到，我接著弄"})


async def _manual(request: web.Request) -> web.Response:
    """人接手 / 交還給奈奈。接手期間模型會停在原地不動。"""
    uid, s = await _act_guard(request)
    data = await request.json()
    on = bool(data.get("on"))
    if s.lingering and not on:
        # 任務已經結束了，沒有「交還」這回事 —— 真的把 paused 放掉的話，
        # 人就再也點不動這一頁（_click 那邊會擋），畫面變成只能看。
        return web.json_response(
            {"ok": False, "msg": "這個任務已經做完了，這一頁本來就交給你操作"})
    s.paused = on
    logger.info("🎛️ %s │ user=%d", "人接手操作" if on else "交還給奈奈", uid)
    return web.json_response({"ok": True,
                              "msg": "你接手了，奈奈先停著" if on else "交還給奈奈了"})


_BOUNDARY = "nanaframe"


async def _stream(request: web.Request) -> web.StreamResponse:
    """一條長連線持續推 JPEG（MJPEG）。前端直接拿它當 <img src>。

    為什麼不繼續一張一張抓：每張圖都是一個獨立請求，而每個請求都要繞 Discord 的
    代理 —— 那條路實測會出現好幾秒的離群值（前端的 frame-timeout 就是在抓它）。
    一秒抓一張還撐得住，要看影片就完全不行。改成一條連線推到底之後，代理只走一次。

    三個地方不做就會壞：

    1. **X-Accel-Buffering: no** —— 前面兩層都是 nginx（我們的 vhost 和 WAF）。
       它們預設會把回應緩衝起來，畫面就變成「卡很久然後一次跳好幾幀」。
    2. **每一幀都要 mark_watched** —— 做完留著的畫面靠「有人在看」才活著，而串流
       只在一開始打一次 API。不標記的話 reaper 會在他正看著的時候把瀏覽器收掉。
    3. **寫入失敗要當成正常結束** —— 使用者關掉面板就是連線斷掉，那不是錯誤。
    """
    uid = _verify(request.query.get("ticket", ""), scope="stream")
    if uid is None:
        raise web.HTTPUnauthorized(text="bad ticket")
    _watchers[uid] = time.time()
    browser.mark_watched(uid)
    s = _session_for(uid)
    if s is None:
        raise web.HTTPNotFound(text="no session")
    if not config.ACTIVITY_STREAM_ENABLED:
        raise web.HTTPServiceUnavailable(text="stream disabled")

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    s.stream_viewers += 1
    started = time.time()
    interval = 1.0 / max(config.ACTIVITY_STREAM_FPS, 1)
    n = 0
    try:
        while True:
            if time.time() - started > config.ACTIVITY_STREAM_MAX_SECONDS:
                break
            # session 可能在看的過程中被收掉（任務結束、沒人看、換新任務）
            if _session_for(uid) is not s:
                break

            frame = await s.live_shot()
            if frame:
                await resp.write(
                    f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode())
                await resp.write(frame)
                await resp.write(b"\r\n")
                n += 1
            browser.mark_watched(uid)
            await asyncio.sleep(interval)
    except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
        pass          # 面板關掉／切走 —— 正常結束
    except Exception as e:  # noqa: BLE001
        logger.debug("串流中斷（忽略）：%s", e)
    finally:
        s.stream_viewers = max(0, s.stream_viewers - 1)
        logger.info("🎛️ 串流結束 │ user=%d │ 推了 %d 幀 │ %.0f 秒",
                    uid, n, time.time() - started)
    return resp


async def _acted(s, msg: str) -> web.Response:
    """人操作完了 —— 把新畫面跟著回應一起送回去。

    不這樣做的話新畫面要等下一次輪詢才出現（最多 1.2 秒），操作起來就是一直在
    等；而且那是一趟額外的來回。Activity 的每個請求都要繞過 Discord 的代理，
    實測會出現好幾秒的離群值（前端的 frame-timeout 就是在抓這個），
    所以能少一趟就少一趟。
    """
    # 有人掛在串流上就不用回傳畫面了 —— 那條連線 8 fps，點擊結果 100 多毫秒就
    # 自己出現了，再塞一張 base64 只是白花頻寬（PNG 約 37KB → base64 約 50KB）。
    if s.stream_viewers > 0:
        return web.json_response({"ok": True, "msg": msg})

    frame = None
    try:
        shot = await s.fresh_shot()
        if shot:
            frame = base64.b64encode(shot).decode()
    except Exception as e:  # noqa: BLE001
        logger.debug("操作後截圖失敗（忽略，下一輪輪詢會補上）：%s", e)
    return web.json_response({"ok": True, "msg": msg, "frame": frame})


async def _click(request: web.Request) -> web.Response:
    uid, s = await _act_guard(request)
    if not s.paused:
        return web.json_response({"ok": False, "msg": "先按「我來操作」再點畫面"})
    data = await request.json()
    msg = await s.human_click(data.get("x", 0), data.get("y", 0))
    return await _acted(s, msg)


async def _type(request: web.Request) -> web.Response:
    uid, s = await _act_guard(request)
    if not s.paused:
        return web.json_response({"ok": False, "msg": "先按「我來操作」"})
    data = await request.json()
    text = str(data.get("text") or "")
    key = str(data.get("key") or "")
    if key:
        msg = await s.human_key(key)
    else:
        if not text:
            return web.json_response({"ok": False, "msg": "沒有內容"})
        msg = await s.human_type(text, bool(data.get("enter")))
    return await _acted(s, msg)


async def _scroll(request: web.Request) -> web.Response:
    uid, s = await _act_guard(request)
    if not s.paused:
        return web.json_response({"ok": False, "msg": "先按「我來操作」"})
    data = await request.json()
    msg = await s.human_scroll(data.get("dy", 300))
    return await _acted(s, msg)


async def _audio(request: web.Request) -> web.Response:
    """把瀏覽器的聲音接進語音頻道／收掉。

    真正的動作在 main（那邊才有 voice client 和奈奈的說話聲），
    所以跟 confirm/input 一樣走註冊進來的回呼。
    """
    uid, _s = await _act_guard(request)
    data = await request.json()
    on = bool(data.get("on"))
    cb = _hooks.get("sound")
    if cb is None:
        return web.json_response({"ok": False, "msg": "這台機器沒有接聲音的功能"})
    msg = await cb(uid, on)
    return web.json_response({"ok": True, "msg": msg})


async def _stop(request: web.Request) -> web.Response:
    uid, s = await _act_guard(request)
    logger.info("🎛️ Activity 停止任務：user=%d", uid)
    await browser.stop(uid)
    return web.json_response({"ok": True, "msg": "停了"})


async def _new_task(request: web.Request) -> web.Response:
    uid = _need_auth(request)
    data = await request.json()
    task = str(data.get("task") or "").strip()
    if not task:
        return web.json_response({"ok": False, "msg": "要做什麼？"})
    if not config.BROWSER_ENABLED:
        return web.json_response({"ok": False, "msg": "瀏覽器功能目前關閉"})

    cb = _hooks.get("start_task")
    if cb is None:
        return web.json_response({"ok": False, "msg": "還沒接上"})
    logger.info("🎛️ Activity 交代新任務：user=%d │ %s", uid, task[:60])
    asyncio.create_task(cb(uid, task))
    return web.json_response({"ok": True, "msg": "好，我去弄"})


# ── 和 main.py 的接點 ──────────────────────────────────
# 不直接 import main（會循環 import），改成讓 main 註冊 callback 進來。
_hooks: dict[str, object] = {}


def register(name: str, fn) -> None:
    _hooks[name] = fn


async def _resume(uid: int, **kw) -> None:
    cb = _hooks.get("resume")
    if cb is None:
        logger.warning("Activity：resume callback 還沒註冊")
        return
    await cb(uid, **kw)


# ── 啟動 / 收攤 ────────────────────────────────────────

def _build_app() -> web.Application:
    app = web.Application()
    routes = [
        ("GET", "/", _index),
        ("POST", "/api/auth", _auth),
        ("POST", "/api/clientlog", _clientlog),
        ("GET", "/api/state", _state),
        ("GET", "/api/ticket", _ticket),
        ("GET", "/api/frame", _frame),
        ("GET", "/api/stream", _stream),
        ("POST", "/api/confirm", _confirm),
        ("POST", "/api/input", _input),
        ("POST", "/api/stop", _stop),
        ("POST", "/api/audio", _audio),
        ("POST", "/api/manual", _manual),
        ("POST", "/api/click", _click),
        ("POST", "/api/type", _type),
        ("POST", "/api/scroll", _scroll),
        ("POST", "/api/task", _new_task),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
        # Discord 的 iframe 會把請求改寫成 /.proxy/... ，兩種路徑都收
        app.router.add_route(method, "/.proxy" + (path if path != "/" else "/"), handler)

    app.router.add_static("/static/", _STATIC)
    app.router.add_static("/.proxy/static/", _STATIC)
    return app


async def start() -> bool:
    """把 Activity 的後端跑起來（在 bot 自己的 event loop 裡）。"""
    global _runner
    if not config.ACTIVITY_ENABLED:
        return False
    if _runner is not None:
        return True
    if not (_STATIC / "index.html").exists():
        logger.warning("🎛️ 找不到 activity/static/index.html，Activity 不啟動")
        return False
    if not config.DISCORD_CLIENT_SECRET:
        logger.warning("🎛️ 沒設定 DISCORD_CLIENT_SECRET → Activity 不啟動"
                       "（沒有它無法驗證身分，任何人都能幫別人按送出）")
        return False

    try:
        _runner = web.AppRunner(_build_app(), access_log=None)
        await _runner.setup()
        site = web.TCPSite(_runner, config.ACTIVITY_HOST, config.ACTIVITY_PORT)
        await site.start()
    except Exception as e:  # noqa: BLE001
        logger.error("🎛️ Activity 起不來：%s", e)
        _runner = None
        return False

    logger.info("🎛️ Activity 後端已啟動 http://%s:%d（對外：%s）",
                config.ACTIVITY_HOST, config.ACTIVITY_PORT, config.ACTIVITY_PUBLIC_URL)
    return True


async def stop_server() -> None:
    global _runner
    if _runner is not None:
        try:
            await _runner.cleanup()
        except Exception:  # noqa: BLE001
            pass
        _runner = None
