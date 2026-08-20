"""
奈奈機器人 — LLM 客戶端

目前聊天／情緒偵測／審核都走 Gemma 4（port 10003 的 llama-server，
啟動時有掛 mmproj，所以同一個端點也吃圖片）。
原本聊天用的 Qwen3.5（port 10001，llama-proxy）已停用。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

import httpx

import config

logger = logging.getLogger("nana.llm")

# ── HTTP 客戶端 ────────────────────────────────────────
_http_client = httpx.AsyncClient(timeout=300.0)


# ── 通用 chat_completion ───────────────────────────────

async def chat_completion(
    messages: list[dict],
    temperature: float = 0.8,
    max_tokens: int = 8192,
    timeout: float = 300.0,
    *,
    base_url: str | None = None,
    model: str | None = None,
    force_json: bool = False,
) -> Optional[str]:
    """
    向 llama-server 發送聊天完成請求。
    base_url / model 可以指定使用哪一台模型伺服器。
    force_json=True 時加入 response_format 限制（僅用於需要 JSON 輸出的場景）。
    """
    _base_url = base_url or config.LM_STUDIO_BASE_URL
    _model = model or config.LM_STUDIO_MODEL
    url = f"{_base_url}/chat/completions"

    payload = {
        "model": _model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if force_json:
        payload["response_format"] = {"type": "json_object"}
        # 禁用 reasoning/thinking，讓模型直接輸出 JSON
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    try:
        resp = await _http_client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        choice_msg = data["choices"][0]["message"]
        content = choice_msg.get("content", "") or ""
        reasoning = choice_msg.get("reasoning_content", "") or ""

        if content:
            # 過濾 <think>...</think>，保留之後的內容
            if "<think>" in content:
                after_think = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                content = after_think if after_think else content
            return content

        if reasoning:
            logger.debug("content 為空，使用 reasoning_content（%d 字）", len(reasoning))
            return reasoning

        logger.warning("API 回覆的 content 和 reasoning_content 都是空的")
        return None

    except httpx.TimeoutException:
        logger.error("LLM 請求逾時（%ds）│ %s", timeout, _base_url)
        return None
    except Exception as e:
        logger.error("LLM 請求失敗: %s │ %s", e, _base_url)
        return None


# ── 現在時間 ───────────────────────────────────────────

_WEEKDAY = "一二三四五六日"


def now_context() -> str:
    """給 system prompt 用的即時時間區塊。

    必須每次呼叫時重算 —— 在 import 時算一次的話，跑幾天後奈奈就會活在
    開機那天。伺服器固定 Asia/Taipei，使用者也在同一時區，直接用本機時間。
    """
    now = datetime.now()
    return (
        f"\n\n## 現在時間\n"
        f"{now:%Y年%m月%d日 %H:%M}（星期{_WEEKDAY[now.weekday()]}）\n"
        f"這是台灣時間，也是對方所在的時區。被問到時間、日期、星期幾時直接用這個回答，"
        f"**不要去搜尋**（搜尋結果常常是 UTC 或別的時區，會答錯）。"
    )


# ── 聊天交談 ──────────────────────────────────────

async def generate_support_response(
    user_message: str | list[dict],
    conversation_history: list[dict] | None = None,
    memory_context: str = "",
) -> Optional[str]:
    """根據使用者訊息產生情緒支持回應（使用 Gemma 4）。

    user_message 可以是純文字，也可以是 OpenAI 的 content blocks
    （含 image_url 時走 Gemma 4 的 vision，見 attachments.build_content）。
    memory_context 是從長期記憶撈出來的內容，附在 system prompt 後面。
    """
    # 現在時間一定要帶 —— 沒帶的話問「現在幾點」它會跑去搜尋，然後拿到 UTC
    # 的日期答錯；「明天」「今天」這類日常相對時間也會亂掉。
    messages = [{
        "role": "system",
        "content": config.SYSTEM_PROMPT + now_context() + memory_context,
    }]

    if conversation_history:
        messages.extend(conversation_history)

    messages.append({"role": "user", "content": user_message})

    return await chat_completion(
        messages,
        temperature=0.85,
        max_tokens=8192,
        base_url=config.GEMMA4_BASE_URL,
        model=config.GEMMA4_MODEL,
    )


# ── Gemma 4 情緒偵測 ──────────────────────────────────

async def detect_emotion(text: str, extra_system: str = "") -> Optional[dict]:
    """使用 Gemma 4 分析訊息的情緒，回傳 JSON dict。

    開著自動表情回應時，同一次判斷會順便回 react / emojis 兩個欄位 ——
    危險訊息偵測和表情判斷共用這一次呼叫，同一則訊息不會被檢測兩次。

    extra_system 給表情那條線補上下文用（例如「最近已經給過他這些表情」），
    只在有附加表情判斷時才接上去。
    """
    system = config.EMOTION_DETECTOR_PROMPT
    if config.REACTION_ENABLED:
        system += config.EMOTION_REACTION_ADDENDUM.replace(
            "{emojis}", " ".join(config.REACTION_EMOJIS)
        ) + extra_system

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]

    result = await chat_completion(
        messages,
        temperature=0.1,
        max_tokens=512,
        base_url=config.GEMMA4_BASE_URL,
        model=config.GEMMA4_MODEL,
        force_json=True,
    )
    if not result:
        return None

    try:
        cleaned = result.strip()

        # 移除 markdown code block
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1).strip()

        # 嘗試找到 JSON object
        json_match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())

        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("無法解析情緒偵測結果: %s", result[:200])
        return None


# ── 判斷要不要上網搜尋 ─────────────────────────────────

async def decide_search(text: str) -> tuple[bool, str, bool, str]:
    """問模型這句話要不要上網，以及該用哪一種方式。

    回傳 (need_search, query, need_browser, browser_task)。

    「要不要開瀏覽器」是搭這一次呼叫的便車判斷的 —— 這句話本來就要問模型
    「需不需要上網」，順便問「是查資料還是要動手操作」不多花一次呼叫。

    判斷失敗一律當作「都不用」—— 奈奈的本業是陪聊，上網只是加分，
    寧可不做也不要因為判斷器掛掉就卡住回覆。
    """
    result = await chat_completion(
        [
            {"role": "system", "content": config.SEARCH_DECIDER_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        max_tokens=200,
        timeout=45.0,
        base_url=config.GEMMA4_BASE_URL,
        model=config.GEMMA4_MODEL,
        force_json=True,
    )
    if not result:
        return False, "", False, ""

    try:
        cleaned = result.strip()
        if "```" in cleaned:
            m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
            if m:
                cleaned = m.group(1).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        data = json.loads(m.group() if m else cleaned)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("無法解析搜尋判斷結果: %s", result[:200])
        return False, "", False, ""

    need = bool(data.get("need_search", False))
    query = str(data.get("query", "") or "").strip()
    want_browser = bool(data.get("need_browser", False))
    browser_task = str(data.get("browser_task", "") or "").strip()

    # 兩個都說要的話以「動手操作」為準 —— 使用者要的是把事情辦好，
    # 不是拿一堆搜尋結果。prompt 已經交代不要同時 true，這裡再收一次。
    if want_browser and browser_task:
        return False, "", True, browser_task
    return (need and bool(query)), query, False, ""


# ── 挑一個表情回應 ─────────────────────────────────────

async def decide_reaction(
    text: str,
    emoji_catalog: str,
    extra_prompt: str = "",
) -> list[str]:
    """問模型這則訊息值不值得按表情、要按哪個。回傳 emoji 字串 list（不按就空）。

    判斷失敗一律當作「不按」—— 按表情是加分項，寧可不按也不要送出奇怪的東西。
    回傳的字串還沒驗證過，能不能真的按由 reactions.py 對白名單檢查。
    """
    # 用 replace 不用 format：prompt 裡有 JSON 範例的大括號
    system = config.REACTION_DECIDER_PROMPT.replace("{emojis}", emoji_catalog) + extra_prompt

    result = await chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        temperature=0.5,   # 高一點，免得每次都按同一個
        max_tokens=200,
        timeout=45.0,
        base_url=config.GEMMA4_BASE_URL,
        model=config.GEMMA4_MODEL,
        force_json=True,
    )
    if not result:
        return []

    try:
        cleaned = result.strip()
        if "```" in cleaned:
            m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
            if m:
                cleaned = m.group(1).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        data = json.loads(m.group() if m else cleaned)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("無法解析表情判斷結果: %s", result[:200])
        return []

    if not isinstance(data, dict) or not data.get("react", False):
        return []

    picks = data.get("emojis", [])
    if isinstance(picks, str):      # 有時候只回一個字串
        picks = [picks]
    if not isinstance(picks, list):
        return []

    out = [str(e).strip() for e in picks if str(e).strip()]
    return out[:config.REACTION_MAX_EMOJIS]


# ── 瀏覽器：看畫面決定下一步 ───────────────────────────

async def decide_browser_action(
    *,
    task: str,
    url: str,
    title: str,
    page_text: str,
    elements: list[dict],
    history: list[str],
    shot: bytes | None = None,
) -> Optional[dict]:
    """看現在的畫面決定下一步要做什麼。回傳一個 action dict，判斷失敗回 None。

    截圖走 Gemma 4 的 vision（llama-server 有掛 mmproj）；同時也把可操作元素
    列成文字送過去 —— 純看圖點座標很不準，帶編號清單讓它挑編號可靠得多。
    """
    lines = []
    for e in elements:
        bits = [f"[{e['i']}] <{e['tag']}"]
        if e.get("type"):
            bits.append(f" type={e['type']}")
        bits.append(">")
        if e.get("label"):
            bits.append(f" {e['label']}")
        if e.get("value"):
            bits.append(f"（目前值：{e['value']}）")
        if e.get("placeholder") and not e.get("label"):
            bits.append(f"（提示：{e['placeholder']}）")
        if e.get("options"):
            bits.append(f"（選項：{'／'.join(e['options'][:12])}）")
        if e.get("checked"):
            bits.append("（已勾選）")
        if not e.get("onScreen"):
            bits.append("（要捲動才看得到）")
        lines.append("".join(bits))

    prompt = (
        f"## 使用者交代的事\n{task}\n\n"
        f"## 現在這一頁\n網址：{url}\n標題：{title}\n\n"
        f"## 可以操作的東西\n" + ("\n".join(lines) or "（這頁沒有可操作的元素）") + "\n\n"
        f"## 頁面文字\n{page_text or '（讀不到文字）'}\n\n"
        f"## 你已經做過的步驟\n" + ("\n".join(history) or "（還沒開始）")
    )

    content: str | list[dict] = prompt
    if shot:
        import base64
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(shot).decode()}},
        ]

    result = await chat_completion(
        [
            {"role": "system", "content": config.BROWSER_AGENT_PROMPT},
            {"role": "user", "content": content},
        ],
        temperature=0.2,
        max_tokens=400,
        timeout=120.0,
        base_url=config.GEMMA4_BASE_URL,
        model=config.GEMMA4_MODEL,
        force_json=True,
    )
    # 帶圖失敗（格式不合、圖太大）→ 退成純文字再試一次，別讓整個任務卡死
    if not result and shot:
        logger.warning("瀏覽器判斷帶圖失敗，改用純文字重試")
        result = await chat_completion(
            [
                {"role": "system", "content": config.BROWSER_AGENT_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2, max_tokens=400, timeout=120.0,
            base_url=config.GEMMA4_BASE_URL, model=config.GEMMA4_MODEL,
            force_json=True,
        )
    if not result:
        return None

    try:
        cleaned = result.strip()
        if "```" in cleaned:
            m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
            if m:
                cleaned = m.group(1).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        data = json.loads(m.group() if m else cleaned)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("無法解析瀏覽器動作：%s", result[:200])
        return None

    if not isinstance(data, dict) or not data.get("action"):
        return None
    if data.get("index") is not None:
        try:
            data["index"] = int(data["index"])
        except (TypeError, ValueError):
            data["index"] = None
    return data


# ── 抽取值得長期記住的事 ───────────────────────────────

async def extract_memories(text: str) -> list[dict]:
    """從一段對話抽出值得長期記住的使用者事實。抽不到就回空 list。

    這支是在回覆送出「之後」於背景跑的，失敗完全不影響聊天。
    """
    result = await chat_completion(
        [
            {"role": "system", "content": config.MEMORY_EXTRACTOR_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        max_tokens=512,
        timeout=60.0,
        base_url=config.GEMMA4_BASE_URL,
        model=config.GEMMA4_MODEL,
        force_json=True,
    )
    if not result:
        return []

    try:
        cleaned = result.strip()
        if "```" in cleaned:
            m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
            if m:
                cleaned = m.group(1).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        data = json.loads(m.group() if m else cleaned)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("無法解析記憶抽取結果: %s", result[:200])
        return []

    items = data.get("memories", [])
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict) and it.get("content")][:3]


# ── Gemma 4 審核用 ─────────────────────────────────────

async def review_completion(
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 1024,
    timeout: float = 120.0,
) -> Optional[str]:
    """
    審核專用：使用 Gemma 4，強制 JSON 輸出。
    """
    return await chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        base_url=config.GEMMA4_BASE_URL,
        model=config.GEMMA4_MODEL,
        force_json=True,
    )
