"""
奈奈機器人 — 自動表情回應 🌸

不管有沒有在跟奈奈講話，頻道裡的訊息她都可能順手按一個貼合語氣的 emoji，
表示「我看到了、我在」。

兩個各自獨立、可以分別開關的功能：

  • **主動判斷**（config.REACTION_ENABLED）—— 交給模型看內容決定要不要按。
    **不是每則訊息都貼**：這裡只做本地前置過濾（指令／連結／權限／防連發／
    自傷字眼），值不值得回應由模型判斷，平淡的日常、事務性訊息、吵架它都會
    回 react: false。同一則訊息只檢測一次，判斷來源看是哪條路：
      - 沒在跟奈奈講話 → 情緒偵測那一次順便回 react/emojis
        （config.EMOTION_REACTION_ADDENDUM），main.py 拿到後叫 apply()
      - 有在跟奈奈講話 → 那條路不跑情緒偵測，由 maybe_react() 自己問一次
        （llm_client.decide_reaction）

  • **跟著別人按**（config.REACTION_FOLLOW_ENABLED，見 follow()）——
    有人在某則訊息上按了表情，奈奈就跟著按同一個。這條完全不花模型：
    別人都按了就表示這則訊息值得回應，直接附和就好。

三個設計原則：
  1. **絕對不能影響回話** —— 一律在背景 task 跑，任何例外都吞掉只留 log
  2. **不能變成刷表情機器人** —— 除了模型自己會拒絕，還有每人冷卻當保險
  3. **模型挑的表情一定要驗證** —— 只接受 config.REACTION_EMOJIS 白名單，
     或這個伺服器真的有的自訂表情；亂送 Discord 會回 400 Unknown Emoji
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque

import discord

import config
import reaction_memory
import stats
import llm_client

logger = logging.getLogger("nana.react")

# ── 上下文紀錄 ─────────────────────────────────────────
# 模型只看得到「這一則訊息」，看不到她剛剛已經按過什麼，所以「不要一直刷同類的、
# 不要一串訊息每則都按」這件事沒辦法交給它判斷，得由這邊記帳。
# 全放記憶體，重啟後重新開始算（頂多剛開機時多按幾個）。
_last_ask: dict[tuple[int, int], float] = {}     # (人, 頻道) → 上次問模型的時間
_last_press: dict[tuple[int, int], float] = {}   # (人, 頻道) → 上次真的按下去的時間
_channel_hits: dict[int, deque[float]] = {}      # 頻道 → 最近按下去的時間戳
_recent_emoji: dict[int, deque[str]] = {}        # 人 → 最近收到過哪些表情
# 模型判斷與 Discord API 都是 await；兩則訊息可能在同一個 event loop tick 裡都
# 通過 _context_ok。這份「暫訂」帳本在真正送 API 前先佔位，避免兩個背景 task
# 一起穿透冷卻；API 失敗時會立刻釋放，不能把失敗也算成已按。
_pending_press: dict[tuple[int, int], float] = {}
_pending_channel_hits: dict[int, deque[float]] = {}
_pending_reservations: dict[int, tuple[tuple[int, int], int, float]] = {}
_PRUNE_AT = 2000        # 表長超過這個數就清一次過期的

# 這些開頭的訊息不值得按：指令、程式碼、純連結
_SKIP_PREFIXES = ("!", "/", "```", "http://", "https://")

# 自訂表情：模型可能回 :name: 或 <:name:123>
_CUSTOM_RE = re.compile(r"^<a?:([A-Za-z0-9_]+):\d+>$|^:([A-Za-z0-9_]+):$")

# 模型常把多個表情塞在同一個字串裡，用這些字元分隔
_SEP_RE = re.compile(r"[\s,，、;；|/]+")

# 比對用：長的排前面，☀️（帶 VS16）才不會被 ☀ 先吃掉
_SORTED_EMOJIS = tuple(sorted(config.REACTION_EMOJIS, key=len, reverse=True))

_VS16 = "\ufe0f"    # emoji variation selector（☀️ 這種帶不帶差在這個看不見的字）

# 「同類」不只是同一個字元：剛按過 😊，下一則再按 😄 仍然會顯得像在刷。
# 自訂表情沒有可靠的語意資訊，所以只做完全相同的去重；Unicode 則分成常見語氣類。
_EMOJI_GROUPS: dict[str, frozenset[str]] = {
    "開心": frozenset(("😊", "😄", "😆", "🤣", "😂", "🥰", "😍", "🤗", "😌", "😎", "🙂", "😅", "🤭")),
    "難過": frozenset(("🥺", "😢", "😭", "😔", "😩", "😴", "🫠", "🥲")),
    "驚訝": frozenset(("😮", "🤯", "🤔", "👀", "😳")),
    "支持": frozenset(("💛", "💜", "💖", "🫶", "🙏", "👍", "👏", "🙌", "💪", "🤝", "✅", "💯", "🫡")),
    "慶祝": frozenset(("🎉", "🎊", "🥳", "🎂", "🍰", "✨", "🌟", "⭐", "🔥")),
    "療癒": frozenset(("🌸", "🌈", "☀️", "🌙", "☕", "🍵", "🐱", "🐰", "🍀", "🫂")),
}
_EMOJI_CATEGORY = {
    emoji.rstrip(_VS16): category
    for category, emojis in _EMOJI_GROUPS.items()
    for emoji in emojis
}


# ── 前置過濾（都是本地判斷，不花模型）──────────────────

def _key(message: discord.Message) -> tuple[int, int]:
    """記帳用的 key：同一個人在不同頻道各算各的。"""
    return (message.author.id, getattr(message.channel, "id", 0))


def _prune() -> None:
    """紀錄表長太大時清掉過期的，避免長期跑下來一直長。"""
    now = time.time()
    for table, window in (
        (_last_ask, config.REACTION_ASK_COOLDOWN),
        (_last_press, config.REACTION_BURST_WINDOW_S),
        (_pending_press, config.REACTION_BURST_WINDOW_S),
    ):
        if len(table) <= _PRUNE_AT:
            continue
        cutoff = now - max(window, 60)
        for key in [k for k, ts in table.items() if ts < cutoff]:
            table.pop(key, None)
    # 正常情況會在 API 成功／失敗後立即釋放；這是防止取消 task 或未知例外留下殘值。
    pending_cutoff = now - max(config.REACTION_BURST_WINDOW_S, 60)
    for message_id, (key, channel_id, ts) in list(_pending_reservations.items()):
        if ts < pending_cutoff:
            _pending_reservations.pop(message_id, None)
            if _pending_press.get(key) == ts:
                _pending_press.pop(key, None)
            hits = _pending_channel_hits.get(channel_id)
            if hits:
                try:
                    hits.remove(ts)
                except ValueError:
                    pass


def _trim_hits(hits: deque[float] | None, *, now: float) -> int:
    """移除頻道滑動視窗外的紀錄，回傳仍在窗內的數量。"""
    if not hits:
        return 0
    cutoff = now - config.REACTION_CHANNEL_WINDOW_S
    while hits and hits[0] < cutoff:
        hits.popleft()
    return len(hits)


def _blocked_content(message: discord.Message) -> bool:
    """這則訊息一律不按表情 —— 提到自傷／想死的訊息要用文字好好回應。

    模型那邊已經交代過不要按，這裡是本地的第二層保險：主動判斷和跟著別人按
    兩條路都會先過這一關。
    """
    text = (message.content or "").lower()
    return any(hint in text for hint in config.REACTION_BLOCK_HINTS)


def _is_reactable(message: discord.Message) -> bool:
    """這則訊息本身值不值得考慮。"""
    if config.REACTION_CHANNELS and getattr(message.channel, "id", 0) not in config.REACTION_CHANNELS:
        return False

    text = (message.content or "").strip()
    # 只有附件沒有文字 → 不按。這條線看不到圖，硬猜可能在別人的壞消息上按 🎉
    if len(text) < config.REACTION_MIN_CHARS:
        return False
    if text.startswith(_SKIP_PREFIXES):
        return False
    if _blocked_content(message):
        return False
    return True


def _can_react(message: discord.Message) -> bool:
    """檢查權限 —— 沒權限就別浪費一次模型判斷。"""
    if message.guild is None:
        return True                      # 私訊一定可以
    me = getattr(message.guild, "me", None)
    if me is None:
        return True
    try:
        perms = message.channel.permissions_for(me)
    except (AttributeError, TypeError):
        return True
    return bool(perms.add_reactions and perms.read_message_history)


def _context_ok(message: discord.Message) -> bool:
    """上下文判斷：這則訊息**現在**按下去會不會變成刷表情。

    兩條規則都拿「她實際按過什麼」在算，不是拿「問過模型幾次」；正在送出
    的 reaction 會暫訂一個名額，避免背景 task 的競態：
      1. 同一個人在同一頻道剛被按過 → 整串就到這裡（連發五則只按一次）
      2. 整個頻道最近按太多則 → 先停手（不要整片都是她的表情）
    """
    now = time.time()

    key = _key(message)
    if (
        now - _last_press.get(key, 0.0) < config.REACTION_BURST_WINDOW_S
        or now - _pending_press.get(key, 0.0) < config.REACTION_BURST_WINDOW_S
    ):
        return False

    channel_id = getattr(message.channel, "id", 0)
    hit_count = _trim_hits(_channel_hits.get(channel_id), now=now)
    pending_count = _trim_hits(_pending_channel_hits.get(channel_id), now=now)
    if hit_count + pending_count >= config.REACTION_CHANNEL_MAX_IN_WINDOW:
        return False

    return True


def _claim_context(message: discord.Message) -> bool:
    """原子地佔住一次按表情的名額，避免並行背景 task 同時通過檢查。"""
    message_id = getattr(message, "id", 0)
    if message_id in _pending_reservations or not _context_ok(message):
        return False

    now = time.time()
    key = _key(message)
    channel_id = getattr(message.channel, "id", 0)
    _pending_press[key] = now
    _pending_channel_hits.setdefault(channel_id, deque()).append(now)
    _pending_reservations[message_id] = (key, channel_id, now)
    return True


def _release_context(message: discord.Message) -> None:
    """釋放尚未成功的暫訂名額。"""
    reservation = _pending_reservations.pop(getattr(message, "id", 0), None)
    if reservation is None:
        return
    key, channel_id, ts = reservation
    if _pending_press.get(key) == ts:
        _pending_press.pop(key, None)
    hits = _pending_channel_hits.get(channel_id)
    if hits:
        try:
            hits.remove(ts)
        except ValueError:
            pass


def _passes_gate(message: discord.Message, *, is_direct: bool,
                 already_decided: bool) -> bool:
    """上下文 + 問模型的節流 + 抽籤。"""
    if not _context_ok(message):
        return False

    # 要另外花一次模型呼叫的路徑才需要節流；搭情緒偵測便車的不用 ——
    # 那邊模型已經答完了，這時候擋掉只是白白丟掉一個它想按的表情。
    if not already_decided:
        if time.time() - _last_ask.get(_key(message), 0.0) < config.REACTION_ASK_COOLDOWN:
            return False

    # 恭喜／生日／好累這類幾乎一定值得回應的，不抽籤直接進判斷
    text = (message.content or "").lower()
    if any(hint in text for hint in config.REACTION_PRIORITY_HINTS):
        return True

    chance = config.REACTION_DIRECT_PROBABILITY if is_direct else config.REACTION_PROBABILITY
    return random.random() < chance


def _note_pressed(message: discord.Message, emojis: list[str]) -> None:
    """記下「她真的按下去了」—— 上下文判斷全靠這份紀錄。"""
    now = time.time()
    _last_press[_key(message)] = now
    _channel_hits.setdefault(getattr(message.channel, "id", 0), deque()).append(now)

    mem = _recent_emoji.get(message.author.id)
    if mem is None:
        mem = deque(maxlen=max(config.REACTION_RECENT_MEMORY, 1))
        _recent_emoji[message.author.id] = mem
    for e in emojis:
        mem.append(e)
    # 也寫進持久記憶 —— 上面那份 deque 只在記憶體裡，重開就忘了，
    # 於是她重啟後又貼一模一樣的表情
    reaction_memory.note(message.author.id, emojis)
    _release_context(message)
    _prune()


def _repeat_blocked(user_id: int) -> set[str]:
    """這次不准用的表情：最近剛收過的，加上**他明確說過不要用的**。

    「不要用」這件事一定要在程式層擋掉，不能只寫進 prompt。prompt 是提示，
    模型偶爾不聽；而使用者已經開口拜託過一次了，再按一次就是言而無信。
    """
    mem = list(_recent_emoji.get(user_id) or [])
    n = max(config.REACTION_AVOID_REPEAT_LAST, 0)
    raw = set(mem[-n:]) if n else set()
    raw |= set(reaction_memory.avoided(user_id))
    # 一律去掉 VS16（U+FE0F）再比。同一個表情有帶不帶 VS16 兩種寫法，
    # 直接字串比對會當成不同的東西 —— 那樣「不要用 ❤️」就擋不掉她按的 ❤。
    return {e.rstrip(_VS16) for e in raw if e}


def _repeat_categories(user_id: int) -> set[str]:
    """最近用過的 emoji 類別；未分類的自訂表情只會走精確比對。"""
    mem = list(_recent_emoji.get(user_id) or [])
    n = max(config.REACTION_AVOID_REPEAT_LAST, 0)
    return {
        category
        for emoji in mem[-n:]
        if (category := _EMOJI_CATEGORY.get(emoji.rstrip(_VS16))) is not None
    }


def _without_same_category(
    targets: list[str | discord.Emoji],
) -> list[str | discord.Emoji]:
    """同一次回應最多留一個已知類別的 emoji。"""
    seen_categories: set[str] = set()
    kept: list[str | discord.Emoji] = []
    for target in targets:
        category = _EMOJI_CATEGORY.get(str(target).rstrip(_VS16))
        if category and category in seen_categories:
            continue
        if category:
            seen_categories.add(category)
        kept.append(target)
    return kept


def avoid_hint(user_id: int) -> str:
    """提示模型「這個人最近收過這些，換一個」。

    硬擋（_repeat_blocked）只擋最近幾個，會擋掉的就整個丟掉；這條提示是讓模型
    一開始就別挑重複的，比事後丟掉好 —— 丟掉等於這次沒表情。
    """
    # 持久記憶那份已經包含「最近按過什麼」＋「他說過不要用什麼」＋「他喜歡什麼」，
    # 而且重開機還在（記憶體那份 deque 不會）。
    persisted = reaction_memory.hint(user_id)
    if persisted:
        return persisted

    used = list(_recent_emoji.get(user_id) or [])
    if not used:
        return ""
    return (
        "\n\n（這個人最近已經收到過這些表情："
        + " ".join(dict.fromkeys(reversed(used)))
        + "。這次請挑不同類型的表情；如果挑不出更貼切的就不要按。）"
    )


# ── 把模型回的字串變成真的能按的表情 ───────────────────

def _guild_emoji_hint(guild: discord.Guild | None) -> str:
    """告訴模型這個伺服器有哪些自訂表情可以用。"""
    if not (config.REACTION_ALLOW_GUILD_EMOJI and guild and guild.emojis):
        return ""
    names = [f":{e.name}:" for e in guild.emojis if e.is_usable()]
    if not names:
        return ""
    names = names[:config.REACTION_GUILD_EMOJI_SAMPLE]
    return (
        "\n\n這個伺服器另外還有這些自訂表情，覺得比清單裡的更貼切就用它（連冒號一起寫）：\n"
        + " ".join(names)
    )


def _resolve(raw: str, guild: discord.Guild | None) -> str | discord.Emoji | None:
    """把模型回的一個表情字串轉成 add_reaction 吃得下的東西。不合法回 None。"""
    token = (raw or "").strip()
    if not token:
        return None

    # 伺服器自訂表情
    m = _CUSTOM_RE.match(token)
    if m:
        if not (config.REACTION_ALLOW_GUILD_EMOJI and guild):
            return None
        name = (m.group(1) or m.group(2) or "").lower()
        return next(
            (e for e in guild.emojis if e.name.lower() == name and e.is_usable()),
            None,
        )

    # Unicode：只收白名單。VS16 有帶沒帶都算同一個。
    for candidate in (token, token + _VS16, token.rstrip(_VS16)):
        if candidate in config.REACTION_EMOJIS:
            return candidate
    return None


def _extract(token: str) -> list[str]:
    """從一段字串裡把白名單認得的表情逐個挑出來。

    模型很常把兩個表情塞在同一個字串裡（`["🥺 🫂"]`、`["🎉🥰"]`），整串比對
    一定不中，白丟掉太浪費 —— 這裡改成掃過去一個一個撿。長的先試，
    ☀️（帶 VS16）才不會被 ☀ 先吃掉。
    """
    found: list[str] = []
    i = 0
    while i < len(token):
        for cand in _SORTED_EMOJIS:
            if token.startswith(cand, i):
                found.append(cand)
                i += len(cand)
                break
        else:
            i += 1          # 不是表情的字（空白、逗號、說明文字）跳過
    return found


def _resolve_many(raw: str, guild: discord.Guild | None) -> list[str | discord.Emoji]:
    """把模型回的一個元素拆成「真的能按的表情」清單（可能 0～多個）。"""
    out: list[str | discord.Emoji] = []
    for piece in _SEP_RE.split((raw or "").strip()):
        if not piece:
            continue
        one = _resolve(piece, guild)
        if one is not None:                 # 剛好就是一個（含 :name: 自訂表情）
            out.append(one)
            continue
        out.extend(_extract(piece))         # 黏在一起的多個 unicode 表情

    seen: set[str] = set()
    uniq: list[str | discord.Emoji] = []
    for e in out:
        key = str(e)
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


# ── 跟著別人按（附和）──────────────────────────────────

def _followable_emoji(
    emoji: discord.PartialEmoji,
    guild: discord.Guild | None,
) -> str | discord.Emoji | discord.PartialEmoji | None:
    """別人按的這個表情，奈奈按不按得出來？按不出來回 None。

    unicode 一律可以（Discord 已經收下了，不必再對白名單）。
    自訂表情只有「這個伺服器自己有、而且奈奈用得到」才行 —— 別人用 Nitro 從
    別的伺服器帶進來的表情，機器人按會直接 400。
    """
    if emoji is None:
        return None
    if emoji.id is None:                       # unicode
        return emoji.name or None
    if guild is None:
        return None
    own = guild.get_emoji(emoji.id)
    return own if (own and own.is_usable()) else None


def _already_followed(message: discord.Message) -> int:
    """奈奈在這則訊息上已經按了幾個表情。"""
    return sum(1 for r in message.reactions if r.me)


async def follow(
    bot_user: discord.ClientUser | None,
    message: discord.Message,
    emoji: discord.PartialEmoji,
) -> None:
    """別人按了表情 → 奈奈也跟著按同一個。

    跟主動判斷那條線完全獨立（兩邊可以分別開關）。這裡不打模型：有人按了就
    表示這則訊息值得回應，跟著附和就好。

    整支包在 try 裡 —— 跟不跟得上都不能影響任何其他功能。
    """
    try:
        if not config.REACTION_FOLLOW_ENABLED:
            return
        if config.REACTION_CHANNELS and getattr(message.channel, "id", 0) not in config.REACTION_CHANNELS:
            return
        if not _can_react(message):
            return

        # 別人按在奈奈自己的訊息上 → 預設不跟（自己按自己有點怪）
        if bot_user is not None and message.author.id == bot_user.id \
                and not config.REACTION_FOLLOW_OWN_MESSAGES:
            return

        if _blocked_content(message):
            logger.debug("這則訊息不附和（含自傷字眼）")
            return

        if _already_followed(message) >= config.REACTION_FOLLOW_MAX_PER_MESSAGE:
            return

        # 附和也要看上下文：有人一路往下按五則訊息，她全部跟一遍也是刷表情
        if not _context_ok(message):
            logger.debug("這串剛剛已經按過了，不再跟")
            return

        target = _followable_emoji(emoji, message.guild)
        if target is None:
            return

        # 他親口說過不要用的表情，**跟著別人按也不行**。
        # 這條路本來完全沒經過硬擋（它不打模型、也不走 apply），所以只要有別人
        # 按了那個表情，她就會跟著按下去 —— 正是使用者拜託她不要做的那件事。
        if str(target).rstrip(_VS16) in reaction_memory.avoided(message.author.id):
            logger.info("😊 不跟著按 %s —— 他說過不要用這個 │ user=%d",
                        target, message.author.id)
            return

        # 已經按過同一個就不用再按（Discord 會忽略，但省一次 API）
        key = str(emoji)
        for r in message.reactions:
            if str(r.emoji) == key:
                if r.me:
                    return
                if r.count < config.REACTION_FOLLOW_MIN_COUNT:
                    return
                break

        # 等一下再按：看起來自然，也順便避開 reaction 的 rate limit
        if config.REACTION_FOLLOW_DELAY > 0:
            await asyncio.sleep(config.REACTION_FOLLOW_DELAY)

        # sleep 期間可能已經有另一條背景流程按過；真正送出前再原子檢查一次。
        if not _claim_context(message):
            return
        try:
            await message.add_reaction(target)
            stats.bump(stats.REACTIONS)
            # 跟著按也算「她給過這個人這個表情」。不記的話「最近給過什麼」會漏掉
            # 一半（實測 log 裡有不少是走這條路按上去的），下次就又挑到同一個。
            reaction_memory.note(message.author.id, [str(target)])
        except discord.HTTPException as e:
            logger.debug("跟著按失敗（%s）：%s", target, e)
            _release_context(message)
            return
        except Exception:
            _release_context(message)
            raise

        _note_pressed(message, [str(target)])
        logger.info(
            "%s 跟著按 │ %s 的訊息：%s",
            target,
            message.author.display_name,
            (message.content or "")[:40],
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("跟著按的流程出錯（忽略）：%s", e)


# ── 主流程 ─────────────────────────────────────────────

def wants_reaction(message: discord.Message, *, is_direct: bool = False,
                   already_decided: bool = False) -> bool:
    """本地前置檢查：這則訊息現在該不該按表情。

    只判斷「輪不輪得到它」（開關／頻道／權限／上下文／節流），**內容值不值得回應
    是模型決定的**，不在這裡。

    is_direct: 是不是在跟奈奈講話（@／回覆／私訊／固定頻道），只影響抽籤機率。
    already_decided: 模型已經答完了（搭情緒偵測那次便車），這時候跳過「問模型的
        節流」那一關，也不記 _last_ask —— 我們根本沒有多問。
    """
    if not config.REACTION_ENABLED:
        return False
    if not _is_reactable(message) or not _can_react(message):
        return False
    if not _passes_gate(message, is_direct=is_direct, already_decided=already_decided):
        return False
    if not already_decided:
        _last_ask[_key(message)] = time.time()
    return True


async def apply(message: discord.Message, picks: list[str] | None) -> None:
    """把模型挑好的表情按上去。picks 是空的就什麼都不做。

    給「已經有判斷結果」的路徑用 —— 被動訊息的表情是情緒偵測那一次順便判斷的
    （見 config.EMOTION_REACTION_ADDENDUM），不需要再打一次模型。
    """
    try:
        # 這裡也可能收到情緒偵測 JSON 裡沒整理過的值，統一先洗一遍
        if isinstance(picks, str):
            picks = [picks]
        if not isinstance(picks, (list, tuple)):
            return
        picks = [str(p).strip() for p in picks if str(p).strip()][:config.REACTION_MAX_EMOJIS]
        if not picks:
            return

        # 一個元素可能拆出好幾個表情（"🥺 🫂"），攤平後再重新算上限
        targets: list[str | discord.Emoji] = []
        for raw in picks:
            resolved = _resolve_many(raw, message.guild)
            if not resolved:
                logger.debug("跳過認不出來的表情：%r", raw)
            targets.extend(e for e in resolved if str(e) not in {str(t) for t in targets})

        # 剛剛才給過這個人的表情不要再給 —— 一直收到同一個 emoji 看起來就是機器人。
        # 提示詞已經先叫模型換一個了，這裡是它沒聽話時的硬擋。
        blocked = _repeat_blocked(message.author.id)
        blocked_categories = _repeat_categories(message.author.id)
        refused = set(reaction_memory.avoided(message.author.id))
        if blocked or blocked_categories:
            kept = [
                t for t in targets
                if str(t).rstrip(_VS16) not in blocked
                and _EMOJI_CATEGORY.get(str(t).rstrip(_VS16)) not in blocked_categories
            ]
            dropped = [str(t) for t in targets if t not in kept]
            if dropped:
                # 他親口拒絕過的表情被擋下來 → 用 info。這件事值得看得到：
                # 代表模型又想按那個表情，而硬擋確實有在工作。
                banned = [d for d in dropped if d.rstrip(_VS16) in refused]
                if banned:
                    logger.info("😊 擋掉他說過不要用的表情：%s │ user=%d",
                                " ".join(banned), message.author.id)
                logger.debug("擋掉剛剛用過的同類表情：%s", dropped)
            targets = kept

        # 同一則訊息也不需要兩個同類表情（例如 😊😄）；不同語氣的組合如 🥺🫂
        # 仍可保留。自訂表情沒有類別資訊，因此不在這裡額外刪除。
        targets = _without_same_category(targets)

        targets = targets[:config.REACTION_MAX_EMOJIS]
        if not targets or not _claim_context(message):
            return

        added: list[str] = []
        try:
            for emoji in targets:
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException as e:
                    # Forbidden（沒權限）／NotFound（訊息被刪）都是這個的子類
                    logger.debug("按表情失敗（%s）：%s", emoji, e)
                    continue
                added.append(str(emoji))
                stats.bump(stats.REACTIONS)

            if added:
                _note_pressed(message, added)
                logger.info(
                    "%s 表情回應 │ %s: %s",
                    "".join(added),
                    message.author.display_name,
                    (message.content or "")[:40],
                )
        finally:
            # 全部 add_reaction 都失敗時，不能留下暫訂名額。
            if not added:
                _release_context(message)
    except Exception as e:  # noqa: BLE001
        logger.debug("按表情時出錯（忽略）：%s", e)


async def maybe_react(message: discord.Message, *, is_direct: bool = False) -> None:
    """獨立判斷一則訊息要不要按表情，要的話就按上去。

    給「沒有跑情緒偵測」的路徑用（有在跟奈奈講話的訊息、不在監聽名單的頻道）。
    沒搭話又在監聽範圍內的訊息不走這裡 —— 那些是情緒偵測順便判斷完再叫 apply()，
    同一則訊息只檢測一次。

    整支包在 try 裡：按表情永遠不能影響真正的回覆，壞掉就安靜地不按。
    """
    try:
        if not wants_reaction(message, is_direct=is_direct):
            return

        text = (message.content or "").strip()[:config.REACTION_MAX_CHARS]
        picks = await llm_client.decide_reaction(
            f"[{message.author.display_name}] 說：{text}",
            emoji_catalog=" ".join(config.REACTION_EMOJIS),
            # 把「最近給過他什麼」一起告訴模型，讓它自己換一個，
            # 比事後硬擋好 —— 硬擋掉就等於這次沒表情。
            extra_prompt=_guild_emoji_hint(message.guild) + avoid_hint(message.author.id),
        )
        await apply(message, picks)
    except Exception as e:  # noqa: BLE001
        logger.debug("表情回應流程出錯（忽略）：%s", e)
