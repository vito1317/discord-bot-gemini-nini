"""
奈奈機器人 — Agent 工具層

讓奈奈除了聊天以外還能「做事」。做法沿用這個專案裡已驗證可靠的模式：
用 force_json 讓 Gemma 輸出結構化意圖，再由這裡分派給對應的工具執行。

之所以不用 OpenAI function calling：llama-server 上的 Gemma 4 對 tools 參數
的支援視 chat template 而定，而 force_json 這條路在本專案的情緒偵測、搜尋
判斷、記憶抽取都已經穩定運作，沒必要換一套不確定的機制。

工具以 TOOLS 註冊，之後要加新能力（查行事曆、記帳、開關裝置…）只要多寫一個
handler 並補進 prompt 即可，不用動 main.py。
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import config
import llm_client
import reminders
import weather

logger = logging.getLogger("nana.agent")

_WEEKDAY = "一二三四五六日"


@dataclass
class AgentResult:
    """工具執行結果。

    reply    — 直接回給使用者的話（有值就不再走一般聊天）
    context  — 補進 prompt 讓奈奈自己組織說法的資訊（reply 為空時使用）
    handled  — 是否真的動用了工具
    """
    handled: bool = False
    reply: str = ""
    context: str = ""


def _now_header() -> str:
    now = datetime.now()
    return f"{now:%Y-%m-%d %H:%M}（星期{_WEEKDAY[now.weekday()]}）"


def build_prompt() -> str:
    """每次呼叫都重建 —— 現在時間必須是即時的，不能在 import 時就固定。"""
    return config.AGENT_PROMPT_TEMPLATE.format(now=_now_header())


async def _decide(text: str) -> dict | None:
    raw = await llm_client.chat_completion(
        [{"role": "system", "content": build_prompt()},
         {"role": "user", "content": text}],
        temperature=0.1, max_tokens=400, timeout=60.0,
        base_url=config.GEMMA4_BASE_URL, model=config.GEMMA4_MODEL,
        force_json=True,
    )
    if not raw:
        return None
    try:
        cleaned = raw.strip()
        if "```" in cleaned:
            m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
            if m:
                cleaned = m.group(1).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        return json.loads(m.group() if m else cleaned)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("無法解析 agent 意圖：%s", raw[:200])
        return None


# ── 工具：提醒 ──────────────────────────────────────────

async def _tool_remind_create(d: dict, *, user_id: int, user_name: str,
                              channel_id: int, target_user_id: int | None = None,
                              target_user_name: str | None = None,
                              is_admin: bool = False, **_kw) -> AgentResult:
    when_raw = str(d.get("when") or "").strip()
    text = str(d.get("text") or "").strip()
    repeat = str(d.get("repeat") or "none").strip()

    # 管理員 @某人 → 幫那個人設提醒；否則設自己的
    if target_user_id and target_user_id != user_id:
        if not is_admin:
            return AgentResult(handled=True,
                               context="（他想幫別人設提醒，但他不是管理員。"
                                       "溫柔地說只有管理員能幫別人設，他可以設自己的）")
        tgt_id = target_user_id
        tgt_name = target_user_name or "對方"
        for_other = True
    else:
        tgt_id, tgt_name, for_other = user_id, user_name, False

    try:
        due = datetime.strptime(when_raw[:16], reminders.FMT)
    except ValueError:
        logger.warning("提醒時間解析失敗：%r", when_raw)
        return AgentResult(handled=True,
                           context="（使用者想設提醒，但你沒聽懂是什麼時候。"
                                   "請溫柔地請他講明確一點，例如「明天早上9點」）")

    rid, err = await reminders.add(tgt_id, tgt_name, channel_id, text, due, repeat)
    if err:
        return AgentResult(handled=True, context=f"（提醒設定失敗：{err}。請告訴對方這件事）")

    rem = reminders.Reminder(rid, tgt_id, channel_id, text, due, repeat)
    logger.info("⏰ 新增提醒 #%d │ %s%s │ %s %s │ %s",
                rid, tgt_name, "（代設）" if for_other else "",
                rem.when_str(), rem.repeat_str(), text)

    rep = f"（{rem.repeat_str()}）" if rem.repeat_str() else ""
    if for_other:
        context = (f"（管理員請你幫 {tgt_name} 設提醒，已設好：{rem.when_str()}{rep} "
                   f"到時候會提醒他「{text}」。自然地確認一下）")
    else:
        context = (f"（你剛剛幫他設好提醒了：{rem.when_str()}{rep} 提醒他「{text}」。"
                   f"自然地確認一下就好，不用重複太多細節）")
    return AgentResult(handled=True, context=context)


async def _tool_remind_list(_d: dict, *, user_id: int, **_kw) -> AgentResult:
    items = await reminders.list_for(user_id)
    if not items:
        return AgentResult(handled=True, context="（他目前沒有任何提醒。告訴他這件事）")
    lines = [f"#{r.id} {r.when_str()} {r.repeat_str()} {r.text}".replace("  ", " ")
             for r in items[:20]]
    return AgentResult(
        handled=True,
        context="（他目前的提醒清單如下，請自然地念給他聽）\n" + "\n".join(lines),
    )


async def _tool_remind_cancel(d: dict, *, user_id: int, **_kw) -> AgentResult:
    kw = str(d.get("text") or "").strip()
    matches = await reminders.find(user_id, kw) if kw else await reminders.list_for(user_id)

    if not matches:
        return AgentResult(handled=True,
                           context=f"（找不到跟「{kw}」有關的提醒。告訴他沒找到）")
    if len(matches) > 1:
        lines = [f"#{r.id} {r.when_str()} {r.text}" for r in matches[:10]]
        return AgentResult(
            handled=True,
            context="（符合的提醒不只一個，請他說要取消哪一個）\n" + "\n".join(lines),
        )

    rem = matches[0]
    ok = await reminders.cancel(user_id, rem.id)
    logger.info("⏰ 取消提醒 #%d │ %s", rem.id, rem.text)
    return AgentResult(
        handled=True,
        context=(f"（已經幫他取消「{rem.text}」這個提醒了，自然地確認一下）"
                 if ok else "（取消失敗，可能已經被刪掉了）"),
    )


# ── 工具：主動關心 ──────────────────────────────────────

async def _tool_checkin_set(d: dict, *, user_id: int, user_name: str,
                            channel_id: int, **_kw) -> AgentResult:
    """定時主動關心。存成 kind='checkin' 的重複提醒，投遞時不念字面，
    改由奈奈依長期記憶臨場想一句關心的話。"""
    when_raw = str(d.get("when") or "").strip()
    repeat = str(d.get("repeat") or "daily").strip()
    if repeat == "none":
        repeat = "daily"        # 「關心我」預設是持續性的，不是一次性

    try:
        due = datetime.strptime(when_raw[:16], reminders.FMT)
    except ValueError:
        return AgentResult(handled=True,
                           context="（他想要你定時關心他，但沒講清楚時間。"
                                   "請問他希望你什麼時候找他）")

    rid, err = await reminders.add(user_id, user_name, channel_id,
                                   "主動關心", due, repeat, kind="checkin")
    if err:
        return AgentResult(handled=True, context=f"（設定失敗：{err}。請告訴對方）")

    rem = reminders.Reminder(rid, user_id, channel_id, "主動關心", due, repeat, "checkin")
    logger.info("💛 新增主動關心 #%d │ %s │ %s %s", rid, user_name,
                rem.when_str(), rem.repeat_str())
    return AgentResult(
        handled=True,
        context=f"（你答應了他：{rem.repeat_str()} {due:%H:%M} 會主動來找他聊聊、"
                f"問他過得好不好。溫暖地確認一下這個約定）",
    )


async def _tool_checkin_off(_d: dict, *, user_id: int, **_kw) -> AgentResult:
    """關掉所有主動關心 —— 包含他自己設的定時關心，也包含系統自動排的追蹤關心。

    使用者說「不要主動找我」時，期待的是「全部都不要」，只取消其中一種
    等於沒關掉，下次還是會被打擾。
    """
    items = [r for r in await reminders.list_for(user_id)
             if r.kind in ("checkin", "auto_checkin")]
    for r in items:
        await reminders.cancel(user_id, r.id)
    await reminders.set_auto_checkin(user_id, False)   # 之後也不再自動排
    logger.info("💛 關閉主動關心 │ user=%d │ 取消 %d 個", user_id, len(items))
    return AgentResult(handled=True,
                       context="（已經完全關掉主動找他這件事了，以後不會再自己跑去打擾他。"
                               "溫柔地說你還是隨時都在，他想聊隨時可以找你）")


async def _tool_checkin_on(_d: dict, *, user_id: int, **_kw) -> AgentResult:
    await reminders.set_auto_checkin(user_id, True)
    logger.info("💛 重新開啟主動關心 │ user=%d", user_id)
    return AgentResult(handled=True,
                       context="（他同意讓你偶爾主動關心他了。溫暖地回應這件事）")


# ── 工具：待辦 ──────────────────────────────────────────

async def _tool_todo_add(d: dict, *, user_id: int, **_kw) -> AgentResult:
    text = str(d.get("text") or "").strip()
    tid, err = await reminders.todo_add(user_id, text)
    if err:
        return AgentResult(handled=True, context=f"（記待辦失敗：{err}）")
    logger.info("📝 新增待辦 #%d │ %s", tid, text)
    return AgentResult(handled=True,
                       context=f"（你把「{text}」記進他的待辦清單了。簡短確認一下就好）")


async def _tool_todo_list(_d: dict, *, user_id: int, **_kw) -> AgentResult:
    items = await reminders.todo_list(user_id)
    if not items:
        return AgentResult(handled=True, context="（他的待辦清單是空的。告訴他這件事）")
    lines = [f"#{i} {t}" for i, t in items[:25]]
    return AgentResult(handled=True,
                       context="（他的待辦清單如下，自然地念給他聽）\n" + "\n".join(lines))


async def _tool_todo_done(d: dict, *, user_id: int, **_kw) -> AgentResult:
    kw = str(d.get("text") or "").strip()
    matches = await reminders.todo_find(user_id, kw) if kw else []
    if not matches:
        return AgentResult(handled=True, context=f"（待辦清單裡找不到「{kw}」）")
    if len(matches) > 1:
        lines = [f"#{i} {t}" for i, t in matches[:10]]
        return AgentResult(handled=True,
                           context="（符合的待辦不只一個，請他說是哪一個）\n" + "\n".join(lines))
    tid, text = matches[0]
    await reminders.todo_done(user_id, tid)
    logger.info("📝 完成待辦 #%d │ %s", tid, text)
    return AgentResult(handled=True,
                       context=f"（他完成了「{text}」，已經從清單劃掉。給他一點肯定）")


# ── 工具：天氣 ──────────────────────────────────────────

async def _tool_weather(d: dict, **_kw) -> AgentResult:
    place = str(d.get("text") or "").strip() or "台北"
    info = await weather.forecast(place)
    if not info:
        return AgentResult(handled=True,
                           context=f"（查不到「{place}」的天氣，可能是地名不明確。"
                                   f"請他換個說法，不要自己編天氣）")
    return AgentResult(handled=True,
                       context=f"（你查到的天氣資料如下，用你自己的話講給他聽，"
                               f"可以順帶提醒帶傘或加外套）\n{info}")


# ── 工具：計算 ──────────────────────────────────────────

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Tuple,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)


_CALC_FIXES = str.maketrans({
    "×": "*", "✕": "*", "＊": "*", "·": "*",
    "÷": "/", "／": "/",
    "－": "-", "−": "-", "＋": "+",
    "（": "(", "）": ")", "，": "", ",": "", " ": "", "　": "",
    "．": ".", "。": ".",
    # 全形數字
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})


def _safe_eval(expr: str) -> float | int:
    """只允許純算術的 AST 求值。

    絕不用 eval() —— 這是使用者可控的字串，eval 等於直接開後門。
    """
    # 模型常輸出 ^ 當次方（Python 的 ^ 是 XOR，會被擋下或算出錯的答案），
    # 全形符號與千分位逗號也一併正規化。
    expr = expr.translate(_CALC_FIXES).replace("^", "**")
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"不允許的語法：{type(node).__name__}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("只接受數字")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            # 2**999999 會直接把 CPU 吃滿
            r = node.right
            if isinstance(r, ast.Constant) and isinstance(r.value, (int, float)) and abs(r.value) > 100:
                raise ValueError("次方太大")
    return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})  # noqa: S307


async def _tool_calc(d: dict, **_kw) -> AgentResult:
    expr = str(d.get("text") or "").strip()
    try:
        val = _safe_eval(expr)
    except Exception:  # noqa: BLE001
        return AgentResult(handled=True,
                           context=f"（算式「{expr}」看不懂，請他寫清楚一點）")
    if isinstance(val, float):
        val = round(val, 10)
    logger.info("🔢 計算 │ %s = %s", expr, val)
    return AgentResult(handled=True,
                       context=f"（算出來了：{expr} = {val}。這是精確值，直接告訴他）")


TOOLS = {
    "remind_create": _tool_remind_create,
    "remind_list": _tool_remind_list,
    "remind_cancel": _tool_remind_cancel,
    "checkin_set": _tool_checkin_set,
    "checkin_off": _tool_checkin_off,
    "checkin_on": _tool_checkin_on,
    "todo_add": _tool_todo_add,
    "todo_list": _tool_todo_list,
    "todo_done": _tool_todo_done,
    "weather": _tool_weather,
    "calc": _tool_calc,
}


# ── 入口 ────────────────────────────────────────────────

_HINTS = (
    # 提醒
    "提醒", "叫我", "記得", "別忘", "不要忘", "鬧鐘", "行程",
    "分鐘後", "小時後", "明天", "後天", "下週", "下星期", "每天", "每週",
    "取消", "remind",
    # 待辦
    "待辦", "todo", "清單", "記一下", "幫我記", "做完", "完成了",
    # 主動關心
    "關心", "陪我", "找我", "問我", "打擾", "別來", "不要來",
    # 天氣
    "天氣", "下雨", "氣溫", "溫度", "冷嗎", "熱嗎", "帶傘",
    # 計算（「算」單獨用會誤中「打算」「算了」「就算」，所以列具體講法）
    "算一下", "算算", "幫我算", "等於幾", "等於多少", "計算",
    "乘以", "除以", "平方", "開根號",
)


async def handle(text: str, *, user_id: int, user_name: str, channel_id: int,
                 target_user_id: int | None = None,
                 target_user_name: str | None = None,
                 is_admin: bool = False) -> AgentResult:
    """判斷這句話要不要動用工具；要的話執行並回傳結果。

    先用關鍵字過濾，避免每一句閒聊都多花一次 LLM 呼叫 —— 奈奈的本業是陪聊，
    agent 只是附加能力。

    target_user_* / is_admin：管理員 @某人 時，可代替那個人設提醒。
    """
    if not config.AGENT_ENABLED or not text:
        return AgentResult()
    if not any(h in text for h in _HINTS):
        return AgentResult()

    d = await _decide(text)
    if not d:
        return AgentResult()

    action = str(d.get("action") or "none").strip()
    if action in ("none", ""):
        return AgentResult()

    tool = TOOLS.get(action)
    if tool is None:
        logger.warning("未知的 agent action：%r", action)
        return AgentResult()

    try:
        return await tool(d, user_id=user_id, user_name=user_name, channel_id=channel_id,
                          target_user_id=target_user_id,
                          target_user_name=target_user_name, is_admin=is_admin)
    except Exception as e:  # noqa: BLE001
        logger.exception("agent 工具 %s 執行失敗：%s", action, e)
        return AgentResult(handled=True, context="（剛剛想幫他處理但出錯了，請他等一下再試）")
