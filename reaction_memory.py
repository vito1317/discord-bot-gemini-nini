"""
表情符號的記憶 😊

記兩件事，都是「關於某一個人」的：

  1. **她對這個人按過哪些表情**（次數、最後一次是什麼時候）
  2. **這個人明確說過不要用哪個表情**

## 為什麼需要它

reactions.py 本來就有一份「最近按過什麼」的紀錄，但只在記憶體裡 ——
重開就忘了，於是她會在重啟後又貼一模一樣的表情。

更嚴重的是第二件：使用者直接跟她說「😅 這個表情在很多情況下都有挑釁意味，能少用嗎」、
「以後不要用 😅」，她當場道歉說「我會學著調整」，但程式裡沒有任何地方記下這件事 ——
下一則訊息又是全新的判斷，她照樣按下去。答應了卻做不到，比一開始就說不行更傷人。

## 設計

  • 「不要用」是**硬規則，寫死在程式裡**（見 reactions._repeat_blocked）。
    不能只寫進 prompt 讓模型自律 —— 模型偶爾不聽，而這件事一次都不該再發生。
  • 存在長期記憶那個 db 檔裡（不同表）—— 這是關於使用者的資料，備份要一起帶走
  • 同步 API：一次帶索引的 upsert 是微秒級，包成 async 只是多一層 await。
    呼叫點都在訊息處理流程裡，所以**絕不能拋例外**，失敗一律吞掉。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime

import config

logger = logging.getLogger("nana.reaction_memory")

# 「以後不要用這個表情」這類話。刻意只認講得很明白的講法 ——
# 判斷錯了會讓她從此不敢用某個表情，寧可漏抓也不要誤抓。
_DISLIKE = re.compile(
    r"(不要用|不要再用|別用|別再用|少用|不用|請勿|拜託不要|停止用|不准用"
    r"|很煩|很討厭|討厭|反感|挑釁|嘲諷|不舒服|不喜歡|能少|可以不要|不要按)")

# 只是在講那個表情、不是在抱怨（避免「我最喜歡 😂」被當成討厭）
_LIKE = re.compile(r"(喜歡|愛用|可愛|很讚|不錯|多用|很好|超讚|好可愛)")

# 訊息裡出現的 emoji。Discord 自訂表情是 <:name:id>，unicode emoji 直接抓。
_CUSTOM = re.compile(r"<a?:(\w+):(\d+)>")
_UNICODE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"      # 各種符號與圖形
    "\U0001F000-\U0001F2FF"
    "☀-➿"              # 雜項符號、裝飾符號
    "\U0001F1E6-\U0001F1FF"      # 國旗
    "]"
)
_VS16 = "️"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.MEMORY_DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS reaction_history (
            user_id INTEGER NOT NULL,
            emoji   TEXT NOT NULL,
            n       INTEGER DEFAULT 0,
            last_at REAL,
            PRIMARY KEY (user_id, emoji)
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS reaction_pref (
            user_id INTEGER NOT NULL,
            emoji   TEXT NOT NULL,
            verdict TEXT NOT NULL,
            said    TEXT,
            at      TEXT,
            PRIMARY KEY (user_id, emoji)
        )""")
    return c


def _norm(e: str) -> str:
    """比對用的形式。

    同一個 emoji 有帶不帶 VS16（U+FE0F）兩種寫法，字串比對會當成不同的東西 ——
    「不要用 ❤️」存進來卻擋不掉她按的 ❤ 就白做了。
    """
    return (e or "").strip().rstrip(_VS16)


def extract_emojis(text: str) -> list[str]:
    """把一段文字裡的表情抓出來（unicode 與 Discord 自訂表情都算）。"""
    out: list[str] = []
    for m in _CUSTOM.finditer(text or ""):
        out.append(f"<:{m.group(1)}:{m.group(2)}>")
    for ch in _UNICODE.findall(text or ""):
        out.append(ch)
    # 去重但保留順序
    return list(dict.fromkeys(out))


# ── 她按過什麼 ─────────────────────────────────────────

def note(user_id: int, emojis: list[str]) -> None:
    """她剛剛對這個人按了這些表情。"""
    if not emojis:
        return
    now = time.time()
    try:
        with _conn() as c:
            for e in emojis:
                k = _norm(str(e))
                if not k:
                    continue
                c.execute(
                    "INSERT INTO reaction_history (user_id,emoji,n,last_at)"
                    " VALUES (?,?,1,?)"
                    " ON CONFLICT(user_id,emoji) DO UPDATE SET"
                    " n = n + 1, last_at = excluded.last_at",
                    (user_id, k, now))
    except Exception as e:  # noqa: BLE001
        logger.debug("記表情紀錄失敗（忽略）：%s", e)


def recent(user_id: int, limit: int = 8) -> list[str]:
    """最近按過的表情（新的在前）。"""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT emoji FROM reaction_history WHERE user_id=?"
                " ORDER BY last_at DESC LIMIT ?", (user_id, max(limit, 1))).fetchall()
        return [r["emoji"] for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("讀表情紀錄失敗（忽略）：%s", e)
        return []


def top(user_id: int, limit: int = 5) -> list[tuple[str, int]]:
    """對這個人最常用的表情。"""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT emoji, n FROM reaction_history WHERE user_id=?"
                " ORDER BY n DESC, last_at DESC LIMIT ?",
                (user_id, max(limit, 1))).fetchall()
        return [(r["emoji"], r["n"]) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("讀表情紀錄失敗（忽略）：%s", e)
        return []


# ── 誰不要哪個表情 ─────────────────────────────────────

def set_pref(user_id: int, emoji: str, verdict: str, said: str = "") -> None:
    k = _norm(emoji)
    if not k:
        return
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO reaction_pref (user_id,emoji,verdict,said,at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(user_id,emoji) DO UPDATE SET"
                " verdict=excluded.verdict, said=excluded.said, at=excluded.at",
                (user_id, k, verdict, (said or "")[:300],
                 datetime.now().strftime("%Y-%m-%d %H:%M")))
    except Exception as e:  # noqa: BLE001
        logger.debug("記表情偏好失敗（忽略）：%s", e)


def avoided(user_id: int) -> dict[str, str]:
    """這個人說過不要用的表情 → 他當時說的話。"""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT emoji, said FROM reaction_pref"
                " WHERE user_id=? AND verdict='avoid'", (user_id,)).fetchall()
        return {r["emoji"]: (r["said"] or "") for r in rows}
    except Exception as e:  # noqa: BLE001
        logger.debug("讀表情偏好失敗（忽略）：%s", e)
        return {}


def liked(user_id: int) -> list[str]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT emoji FROM reaction_pref"
                " WHERE user_id=? AND verdict='like'", (user_id,)).fetchall()
        return [r["emoji"] for r in rows]
    except Exception as e:  # noqa: BLE001
        return []


def learn_from_text(user_id: int, text: str) -> list[str]:
    """從一句話裡學到「他不要（或喜歡）哪個表情」。回傳學到的 avoid 清單。

    要同時滿足兩個條件才算：**句子裡有表情、而且有講得很明白的抱怨**。
    只有其中一個都不算 —— 「今天好累 😅」不是在抱怨那個表情。

    刻意不呼叫模型：這條路每則訊息都會經過，而且判斷錯的代價是「她從此不敢用
    某個表情」，寧可漏抓也不要誤抓。真的漏了，使用者再講一次明白的就會抓到。
    """
    t = (text or "").strip()
    if not t:
        return []
    emojis = extract_emojis(t)
    if not emojis:
        return []

    learned: list[str] = []
    if _DISLIKE.search(t) and not _LIKE.search(t):
        for e in emojis:
            set_pref(user_id, e, "avoid", t)
            learned.append(_norm(e))
        if learned:
            logger.info("😊 學到不要用的表情 │ user=%d │ %s │ 因為：%s",
                        user_id, " ".join(learned), t[:60])
    elif _LIKE.search(t) and not _DISLIKE.search(t):
        for e in emojis:
            set_pref(user_id, e, "like", t)
    return learned


# ── 給模型看的提示 ─────────────────────────────────────

def hint(user_id: int) -> str:
    """接在表情判斷 prompt 後面的一段。

    「不要用」那部分程式已經硬擋了（見 reactions._repeat_blocked），這裡再講一次
    是為了讓模型一開始就別挑 —— 被硬擋掉等於這次沒表情，不如一開始就換一個。
    """
    parts: list[str] = []

    no = avoided(user_id)
    if no:
        parts.append(
            "這個人**明確說過不要用**這些表情，絕對不能再用："
            + " ".join(no.keys())
            + "。（他當時說：" + list(no.values())[0][:60] + "）")

    used = recent(user_id, 8)
    used = [e for e in used if e not in no]
    if used:
        parts.append(
            "你最近給過他這些表情：" + " ".join(used)
            + "。這次挑不一樣的；挑不出更貼切的就不要按。")

    fav = liked(user_id)
    if fav:
        parts.append("他說過喜歡這些：" + " ".join(fav) + "。")

    if not parts:
        return ""
    return "\n\n（" + "\n".join(parts) + "）"
