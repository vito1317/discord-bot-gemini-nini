"""
奈奈 — Discord 情緒支持機器人 🌸
主程式入口（使用 py-cord）

功能：
  1. 自動情緒偵測：監聽所有訊息，偵測到負面情緒時主動關心
  2. 危險訊息警告：偵測到自傷等危險訊號時，立即發送警告到指定頻道
  3. 關鍵字觸發：訊息中包含「奈奈」等關鍵字時，用 AI 回應
  4. 對話模式：使用者 @奈奈、回覆奈奈的訊息、私訊均可直接對話
     （用 Discord 的「回覆」指著某則訊息時，那則訊息的內容也會一起給奈奈看）
  5. 自動表情回應：不管有沒有在跟奈奈講話，都會挑貼合語氣的 emoji 按上去；
     別人按了表情她也會跟著按（危險訊息偵測和表情判斷共用同一次模型呼叫）
  6. 語音頻道：透過 /join 加入語音頻道，橋接 MiniCPM-o 4.5 語音 AI
  7. 斜線指令：/chat、/mood、/reset、/help、/support、/join、/leave、/status
  8. 前綴指令：!nana、!reset、!help、!mood、!support
"""

from __future__ import annotations

import asyncio
import io
import os
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import discord
import httpx
from discord.ext import commands, tasks

import activity
import audio_sink
import attachments
import browser
import config
import llm_client
import agent
import memory
import reactions
import reaction_memory
import reminders
import focus_review
import numpy as np

import stats
import webfetch
import websearch
from conversation import ConversationManager
from voice_bridge import (bridge, OmniSink, AIAudioSource, MixedAudioSource,
                          recording_done)
from review import ReviewManager, detect_form, parse_form, ai_review, ai_followup_review, approve_member

# ── 日誌設定 ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-12s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nana")

# ── Opus ───────────────────────────────────────────────
if not discord.opus.is_loaded():
    for lib in ("libopus.so.0", "libopus.so", "libopus.dylib"):
        try:
            discord.opus.load_opus(lib)
            logger.info("Opus loaded: %s", lib)
            break
        except OSError:
            continue

# ── Discord Bot（py-cord）──────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

# auto_sync_commands=False：py-cord 的自動同步是 bulk 覆寫，會連 Discord 為
# Activity 自動建立的 Entry Point 指令（type 4，語音頻道裡「啟動活動」那個）
# 一起刪掉，Discord 就回 50240 並**整批拒絕** —— 結果所有指令都同步不了。
# 改成自己同步，把 Entry Point 一起送回去（見 sync_commands_keep_entry_point）。
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    auto_sync_commands=False,
)

conv_manager = ConversationManager()
review_manager = ReviewManager()

# user_id → channel_id：瀏覽任務是在哪個頻道交代的。
# 從 Activity 操作時（那裡沒有頻道概念）要把結果送回原本的對話裡。
_last_channel_of: dict[int, int] = {}

# 瀏覽器 session 的回收巡邏（on_ready 起，見 browser.reaper）
_browser_reaper: asyncio.Task | None = None
_persistent_views_registered = False


# ═══════════════════════════════════════════════════════
# 事件處理
# ═══════════════════════════════════════════════════════


@bot.event
async def on_ready():
    """機器人上線"""
    logger.info("🌸 %s 已上線！(%s)", config.BOT_NAME, bot.user)
    global _persistent_views_registered
    if not _persistent_views_registered:
        bot.add_view(focus_review.FocusReviewView())
        _persistent_views_registered = True
    logger.info("   已連接 %d 個伺服器", len(bot.guilds))
    logger.info("   觸發關鍵字: %s", ", ".join(config.TRIGGER_KEYWORDS))
    if config.ALERT_CHANNEL_ID:
        logger.info("   ⚠️ 危險訊息警告頻道: %d", config.ALERT_CHANNEL_ID)
    else:
        logger.warning("   ⚠️ 未設定 ALERT_CHANNEL_ID，危險訊息警告功能停用")

    # 變數名不能叫 activity —— 會遮蔽 activity 模組（Activity 操作台）
    presence = discord.Activity(
        type=discord.ActivityType.listening,
        name="你的心聲 💛 | /help",
    )
    await bot.change_presence(status=discord.Status.online, activity=presence)

    # 清理殘留的語音連線（避免重啟後 4017 錯誤）
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=True)
            logger.info("🔇 已清理殘留語音連線: %s", vc.channel)
        except Exception:
            pass

    # 強制離開所有語音頻道（即使 voice_clients 為空）
    for guild in bot.guilds:
        me = guild.me
        if me and me.voice and me.voice.channel:
            logger.info("🔇 偵測到殘留語音狀態: %s，正在斷開...", me.voice.channel)
            try:
                await guild.change_voice_state(channel=None)
            except Exception as e:
                logger.warning("斷開語音失敗: %s", e)

    # 重啟前可能有殘留的瀏覽器（session 是記憶體內的，程序沒了就找不回來）
    await browser.shutdown()
    await audio_sink.stop()

    # ── Discord Activity（語音頻道裡的操作台）──
    # client_id 就是 application id，等連上線才拿得到，所以在這裡補
    if not config.DISCORD_CLIENT_ID and bot.user:
        config.DISCORD_CLIENT_ID = str(bot.user.id)
    await sync_commands_keep_entry_point()

    activity.register("resume", _activity_resume)
    activity.register("start_task", _activity_start_task)
    activity.register("sound", set_browser_sound)
    activity.register("sound_state",
                      lambda uid: bool((vc := _vc_for_user(uid)) and browser_sound_on(vc)))
    if await activity.start():
        logger.info("   🎛️ Activity 操作台：%s", config.ACTIVITY_PUBLIC_URL)

    # 瀏覽器 session 的回收巡邏。任務做完之後畫面會留著，而「沒人在看就收」
    # 這個判斷需要有人定期去檢查 —— 沒有這隻的話留著的 Chromium 會活到
    # 下一次有人開瀏覽任務為止。
    global _browser_reaper
    if _browser_reaper is None or _browser_reaper.done():
        _browser_reaper = asyncio.create_task(browser.reaper())

    if not cleanup_sessions.is_running():
        cleanup_sessions.start()
    if not deliver_reminders.is_running():
        deliver_reminders.start()
    if config.PROFILE_STATS_ENABLED and not update_profile_stats.is_running():
        update_profile_stats.start()


async def _activity_resume(user_id: int, **kw) -> None:
    """Activity 上按了確認／送了資料 → 接續那個任務。

    結果照樣送回原本那個頻道 —— Activity 只是另一個操作介面，
    紀錄和截圖還是要留在對話裡。
    """
    s = browser.pending_for(user_id)
    task = s.task if s else ""
    owner = s.user_id if s else user_id
    delegate = s.delegate_id if s else None
    cid = _last_channel_of.get(owner)
    dest = bot.get_channel(cid) if cid else None
    try:
        result = await browser.resume(user_id, **kw)
    except Exception as e:  # noqa: BLE001
        logger.warning("Activity 接續任務失敗：%s", e)
        return
    if dest is not None:
        await deliver_browse(dest, owner, result, task=task, delegate_id=delegate)
        await send_browse_video(dest, owner, result)


async def _activity_start_task(user_id: int, task: str) -> None:
    """Activity 上直接交代一件新任務。

    結果送到他最近一次用奈奈的頻道；找不到就私訊他。
    """
    dest = _last_channel_of.get(user_id)
    dest = bot.get_channel(dest) if dest else None
    if dest is None:
        try:
            u = bot.get_user(user_id) or await bot.fetch_user(user_id)
            dest = u.dm_channel or await u.create_dm()
        except discord.HTTPException:
            logger.warning("Activity 交代的任務找不到可以回報的頻道")
            return
    await run_browse_task(dest, user_id, task)


async def sync_commands_keep_entry_point() -> None:
    """同步斜線指令，但保留 Activity 的 Entry Point 指令。

    Discord 在啟用 Activities 時會自動建立一個 type=4（PRIMARY_ENTRY_POINT）的
    指令。bulk 覆寫沒把它帶上就會被視為「要刪掉它」→ 400 error 50240，而且是
    整批失敗，所有指令都不會更新。py-cord 不知道這種指令的存在，所以自己來。
    """
    app_id = bot.user.id
    url = f"https://discord.com/api/v10/applications/{app_id}/commands"
    headers = {"Authorization": f"Bot {config.DISCORD_TOKEN}",
               "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            existing = (await c.get(url, headers=headers)).json()
            keep = [cmd for cmd in existing
                    if isinstance(cmd, dict) and cmd.get("type") == 4]

            payload = [cmd.to_dict() for cmd in bot.pending_application_commands]
            payload += keep

            r = await c.put(url, headers=headers, json=payload)
            if r.status_code == 200:
                logger.info("   ⌨️ 已同步 %d 個指令（保留 %d 個 Entry Point）",
                            len(payload) - len(keep), len(keep))
            else:
                logger.error("同步指令失敗：%s %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.error("同步指令時出錯：%s", e)


@bot.event
async def on_application_command_error(
    ctx: discord.ApplicationContext, error: discord.DiscordException):
    """斜線指令出錯時給使用者一句話，並把原因記成一行 log。

    預設行為是把整個 traceback 印到 stderr、使用者只看到「應用程式沒有回應」，
    很難查。特別常見的是 10062 Unknown interaction —— 互動權杖只有 3 秒有效，
    重啟期間按下的指令一定過期，那不是指令本身壞掉。
    """
    err = getattr(error, "original", error)

    if isinstance(err, discord.NotFound) and getattr(err, "code", 0) == 10062:
        logger.warning("⌛ /%s 的互動已過期（多半是在重啟期間按的）", ctx.command.qualified_name)
        return

    logger.error("❌ /%s 出錯：%s: %s", ctx.command.qualified_name,
                 type(err).__name__, err)
    try:
        await ctx.respond(f"這個指令出錯了 😵‍💫（{type(err).__name__}）\n"
                          f"-# 我已經記錄下來了，稍後再試一次看看。", ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    """處理所有收到的訊息"""
    if message.author == bot.user or message.author.bot:
        return

    # 重點關注必須早於指令與任何 AI 處理；名單成員的指令也要先經人工審核。
    if await focus_review.intercept(message, bot):
        return

    # 先處理前綴指令
    await bot.process_commands(message)

    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    # 記錄活躍時段 —— 主動關心要挑他平常會在線的時間才有意義
    if config.AGENT_ENABLED:
        try:
            await reminders.note_activity(message.author.id)
        except Exception as e:  # noqa: BLE001
            logger.debug("記錄活躍時段失敗（忽略）：%s", e)

    # 「以後不要用 😅 這個表情」這種話要真的被記下來。
    #
    # 放在這裡（每一則訊息都會經過）而不是放在表情判斷裡：抱怨通常是直接對她說的，
    # 那條路走的是對話流程，不會經過表情判斷。之前就是這樣 —— 她當場答應「我會學著
    # 調整」，但沒有任何地方記下來，下一則訊息又照樣按下去。
    try:
        reaction_memory.learn_from_text(message.author.id, message.content or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("學表情偏好失敗（忽略）：%s", e)

    # ── 新成員審核：表單偵測或追蹤回覆 ──
    if await handle_review(message):
        return  # 已處理審核，不再做其他處理

    # ── 判斷互動模式 ──
    is_mentioned = getattr(config, "REPLY_ON_MENTION", True) and bot.user in message.mentions
    # 被回覆的那則訊息只解析一次，回覆偵測和「讓奈奈看到內容」共用同一個結果
    replied = await resolve_reference(message)
    is_reply_to_bot = bool(replied and replied.author == bot.user)
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_fixed_channel = config.FIXED_REPLY_CHANNEL_ID and message.channel.id == config.FIXED_REPLY_CHANNEL_ID
    # 私人聊天室：裡面每一句她都會回，不用 @
    is_private_room = message.channel.id in config.PRIVATE_ROOMS

    # 關鍵字偵測（文字訊息中包含「奈奈」等關鍵字）
    content_lower = message.content.lower()
    has_keyword = getattr(config, "REPLY_ON_KEYWORD", True) and any(kw in content_lower for kw in config.TRIGGER_KEYWORDS)

    # 附件只在下面這些「有搭話」的情境才會被讀（見 handle_direct_conversation），
    # 一般頻道裡別人隨手貼的檔案奈奈不會去翻。

    talking_to_nana = bool(
        is_fixed_channel or is_private_room or is_mentioned or is_reply_to_bot
        or is_dm or has_keyword
    )

    if talking_to_nana:
        author_id = message.author.id

        # ── 任務進行中被問「進度？」→ 直接回報，不要丟給聊天模型 ──
        # 只有真的有任務在跑時才比對關鍵字，所以平常聊天不受影響，
        # 也不必為了這件事多打一次模型。
        if config.BROWSER_ENABLED:
            snap = browser.progress_for(author_id)
            if snap and any(k in content_lower for k in _PROGRESS_ASKS):
                logger.info("🖥️ 回報進度 │ user=%d │ 第 %d 步", author_id, snap["step_no"])
                await send_browse_progress(message, snap)
                return

        # ── 有瀏覽任務卡在「等你補資料」→ 這句話就是答案 ──
        # 放在最前面：她剛剛才問「身分證幾號」，那這句就是要拿去填的，
        # 不該再被當成一般閒聊丟給聊天模型。
        if config.BROWSER_ENABLED and browser.awaiting_input(author_id):
            answer = message.content
            for mention in message.mentions:
                answer = answer.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
            answer = answer.strip()
            if answer:
                logger.info("🖥️ 收到補充資料，接續瀏覽任務 │ user=%d │ %d 字",
                            author_id, len(answer))
                await message.reply("收到，我接著弄 🖥️", mention_author=False)
                asyncio.create_task(continue_browse_task(message, answer))
                return

        # ── 表情回應（這條路不做情緒偵測，所以表情要自己判斷一次）──
        # 丟背景跑：按表情要等一次模型判斷，不能讓它拖到下面的回覆。
        asyncio.create_task(reactions.maybe_react(message, is_direct=True))
        # 直接對話模式
        await handle_direct_conversation(message, replied=replied)
    else:
        # 被動情緒偵測（所有訊息）—— 表情由這一次判斷順便決定，不另外打模型
        await handle_emotion_detection(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """有人按了表情 → 奈奈跟著按同一個（附和）。

    用 raw 版本而不是 on_reaction_add：raw 連「不在快取裡的舊訊息」也收得到，
    重啟後別人去按舊訊息一樣跟得上。
    """
    if not config.REACTION_FOLLOW_ENABLED:
        return
    # 奈奈自己按的不能再跟，不然會自己觸發自己
    if bot.user and payload.user_id == bot.user.id:
        return
    if payload.member and payload.member.bot:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.HTTPException:
            return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.HTTPException, AttributeError) as e:
        logger.debug("取不到被按表情的訊息 %s：%s", payload.message_id, e)
        return

    await reactions.follow(bot.user, message, payload.emoji)


async def resolve_reference(message: discord.Message) -> discord.Message | None:
    """取得使用者「回覆」的那則訊息。沒有回覆、或抓不到就回 None。

    多數情況 Discord 會直接把 referenced_message 塞在 gateway payload 裡
    （= reference.resolved），只有它沒給時才多打一次 API 去 fetch —— 這樣
    連「回覆一則很久以前的奈奈訊息」也認得出來，不會因為不在快取就被忽略。
    """
    ref = message.reference
    if ref is None:
        return None

    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        return resolved
    if isinstance(resolved, discord.DeletedReferencedMessage):
        return None      # 被回覆的訊息已經刪了
    if ref.message_id is None:
        return None

    try:
        return await message.channel.fetch_message(ref.message_id)
    except (discord.HTTPException, AttributeError) as e:
        logger.debug("取不到被回覆的訊息 %s：%s", ref.message_id, e)
        return None


# ═══════════════════════════════════════════════════════
# 新成員審核
# ═══════════════════════════════════════════════════════


async def handle_review(message: discord.Message) -> bool:
    """
    處理新成員審核。回傳 True 表示已攔截處理。
    1. 偵測審核頻道的表單
    2. 追蹤待確認用戶的後續訊息
    """
    user_id = message.author.id

    # ── 追蹤中的用戶回覆（在審核頻道內） ──
    if (
        review_manager.is_pending(user_id)
        and config.REVIEW_CHANNEL_ID
        and message.channel.id == config.REVIEW_CHANNEL_ID
    ):
        pending = review_manager.get_pending(user_id)
        if pending and pending.follow_up_count < pending.max_follow_ups:
            pending.conversation.append(
                {"role": "user", "content": message.content}
            )
            pending.last_activity = __import__("time").time()
            pending.follow_up_count += 1

            logger.info(
                "📝 審核追問回覆 │ %s: %s",
                message.author.display_name,
                message.content[:80],
            )

            async with message.channel.typing():
                result = await ai_followup_review(pending)

            if result:
                await process_review_result(
                    message, result, is_followup=True
                )
            else:
                await message.reply(
                    "⚠️ 審核系統暫時無法處理，請稍後重試或等待管理員人工審核。",
                    mention_author=False,
                )
            return True

    # ── 偵測新表單 ──
    if (
        config.REVIEW_CHANNEL_ID
        and message.channel.id == config.REVIEW_CHANNEL_ID
        and detect_form(message.content)
    ):
        logger.info(
            "📋 偵測到審核表單 │ %s",
            message.author.display_name,
        )

        parsed = parse_form(message.content)
        logger.info("   解析欄位: %s", list(parsed.keys()))

        async with message.channel.typing():
            result = await ai_review(message.content)

        if result:
            # 建立追蹤記錄（即使通過也先記錄）
            from review import PendingReview

            pending = PendingReview(
                user_id=user_id,
                channel_id=message.channel.id,
                form_content=message.content,
                form_message_id=message.id,
            )
            review_manager.add_pending(pending)

            await process_review_result(message, result, is_followup=False)
        else:
            await message.reply(
                "⚠️ 審核系統暫時無法處理你的申請，管理員會盡快人工審核。\n"
                "請耐心等候 🌸",
                mention_author=False,
            )
        return True

    return False


async def process_review_result(
    message: discord.Message,
    result: dict,
    is_followup: bool = False,
):
    """處理 AI 審核結果"""
    decision = result.get("decision", "follow_up")
    reason = result.get("reason", "")
    follow_up_q = result.get("follow_up_question", "")
    risk_level = result.get("risk_level", 1)

    logger.info(
        "🔍 審核結果 │ %s: %s (風險 %d) │ %s",
        message.author.display_name,
        decision,
        risk_level,
        reason,
    )

    if decision == "approve":
        # ── 通過 ──
        review_manager.remove_pending(message.author.id)
        if isinstance(message.author, discord.Member):
            await approve_member(message.author, message.channel, reason)
        else:
            embed = discord.Embed(
                title="✅ 審核通過",
                description=f"{message.author.mention} 審核通過！冒險團歡迎你！",
                color=0x4CAF50,
            )
            embed.add_field(
                name="👋 接下來",
                value="可以先到 [新人報到](https://canary.discord.com/channels/1212105433943117844/1348893805268697129) 和大家打招呼，\n"
                      "再到 [新手教學及頻道指南](https://canary.discord.com/channels/1212105433943117844/1324269864201752586) 查看頻道使用方法。",
                inline=False,
            )
            embed.set_footer(text=f"— {config.BOT_NAME}")
            await message.channel.send(f"{message.author.mention}", embed=embed)

    elif decision == "reject":
        # ── 拒絕 ──
        review_manager.remove_pending(message.author.id)
        embed = discord.Embed(
            title="❌ 審核未通過",
            description=reason,
            color=0xFF6B6B,
        )
        if risk_level >= 4:
            embed.add_field(
                name="📞 需要幫助嗎？",
                value=(
                    "• **安心專線** 1925（24小時免費）\n"
                    "• **生命線** 1995\n"
                    "• **張老師專線** 1980"
                ),
                inline=False,
            )
        embed.add_field(
            name="💡 提醒",
            value="建議先就醫或尋求專業諮商，穩定後歡迎再來申請加入 🌸",
            inline=False,
        )
        embed.set_footer(text=f"— {config.BOT_NAME}")
        await message.reply(embed=embed, mention_author=False)

    elif decision == "redirect_admin":
        # ── 轉交管理員 ──
        review_manager.remove_pending(message.author.id)
        embed = discord.Embed(
            title="📋 需要人工審核",
            description="你的申請需要管理員人工審核（陪同者申請），請耐心等候！\n管理員會盡快處理你的申請 🌸",
            color=0xFFA500,
        )
        embed.set_footer(text=f"— {config.BOT_NAME}")
        await message.reply(embed=embed, mention_author=False)
        # 如果有警告頻道，通知管理員
        if config.ALERT_CHANNEL_ID:
            alert_ch = bot.get_channel(config.ALERT_CHANNEL_ID)
            if alert_ch:
                embed = discord.Embed(
                    title="📋 需要人工審核",
                    description=f"{message.author.mention} 提交的表單需要管理員手動審核（疑似陪同者）。",
                    color=0xFFA500,
                )
                embed.add_field(
                    name="原因", value=reason, inline=False
                )
                if message.guild:
                    embed.add_field(
                        name="🔗 表單連結",
                        value=f"[點擊查看]({message.jump_url})",
                        inline=False,
                    )
                await alert_ch.send(embed=embed)

    elif decision == "follow_up":
        # ── 追問 ──
        pending = review_manager.get_pending(message.author.id)
        if pending:
            pending.conversation.append(
                {"role": "assistant", "content": follow_up_q}
            )

        question = follow_up_q or "可以再告訴我更多關於你的狀況嗎？這樣我才能更好地完成審核 🌸"
        await message.reply(
            f"📝 **需要更多資訊**\n\n{question}",
            mention_author=False,
        )


# ═══════════════════════════════════════════════════════
# 直接對話處理
# ═══════════════════════════════════════════════════════


def _quote_text(msg: discord.Message) -> str:
    """把一則訊息壓成一段可讀的文字，給「被回覆的訊息」引用用。

    content 之外也看 embed 和附件名 —— 奈奈自己的回覆常常整則都是 embed
    （/help、警告卡片），只讀 content 會拿到空字串。
    """
    parts: list[str] = []

    body = (msg.content or "").strip()
    if body:
        parts.append(body)

    for em in msg.embeds[:2]:
        bits = [t for t in (em.title, em.description) if t]
        if bits:
            parts.append("（卡片）" + "：".join(bits))

    names = [a.filename for a in msg.attachments]
    if names:
        parts.append(f"（附件：{'、'.join(names[:4])}）")

    if msg.stickers:
        parts.append(f"（貼圖：{msg.stickers[0].name}）")

    return "\n".join(parts).strip()


def _recall_query(content: str, reply_block: str) -> str:
    """撈長期記憶要用的查詢字串。

    很短又全是代名詞的追問（「哪些說法」「什麼意思」「然後呢」）拿去比對記憶
    什麼都撈不到 —— 那幾個字本身沒有內容。這種時候要用「他在回覆的那則訊息」
    當查詢，指的東西才找得回來。
    實例：她主動關心說「之前聽你提到那些說法」，對方回「哪些說法」，
    用四個字去撈 → 空的 → 她只能說「抱歉我沒對上訊號」。
    """
    text = (content or "").strip()
    if not reply_block:
        return text
    if len(text) <= config.RECALL_ANAPHORA_MAX_CHARS or _ANAPHORA.search(text):
        # 把被回覆的內容接在後面（原文照樣保留 —— 他問的還是他問的）
        return f"{text}\n{reply_block[:600]}"
    return text


# 純指代、自己沒有內容的追問
_ANAPHORA = re.compile(
    r"(哪些|哪個|哪一|什麼意思|什麼啊|是什麼|怎麼說|指的是|然後呢|所以呢"
    r"|真的嗎|為什麼|怎樣|如何|誰啊|在說什麼|沒聽懂|聽不懂)")


async def build_reply_context(
    replied: discord.Message | None,
) -> tuple[str, str, list[tuple[str, str]]]:
    """使用者用 Discord 的「回覆」指著某則訊息說話時，把那則訊息整理給奈奈看。

    回傳 (接在 prompt 後面的區塊, 存進對話歷史的短摘要, 要一起送的圖片)。
    引用的全文不進歷史 —— 跟搜尋結果一樣，每輪重送 context 會爆掉，
    所以歷史裡只留一行「他在回覆某人的某句話」。
    """
    if not (replied and getattr(config, "REPLY_CONTEXT_ENABLED", True)):
        return "", "", []

    quoted = _quote_text(replied)
    if not quoted:
        return "", "", []

    limit = config.REPLY_CONTEXT_MAX_CHARS
    if len(quoted) > limit:
        quoted = quoted[:limit] + "…（後面省略）"

    is_self = replied.author == bot.user
    who = "奈奈（你自己）" if is_self else replied.author.display_name

    block = (
        f"\n\n## 他正在回覆這則訊息\n"
        f"{who} 說：\n{quoted}\n"
        f"（他接下來那句話是針對這則訊息講的，要接著這個脈絡回答，"
        f"不要問他在說什麼。）"
    )

    # 他回覆的是「奈奈主動關心他」的那則訊息時，把當初的來源接回去。
    #
    # 這一段是必要的：主動關心刻意不複述對方的原話（見 AUTO_CHECKIN_PROMPT），
    # 所以她講出來的是「之前聽你提到那些說法…」。對方回一句「哪些說法」的時候，
    # 引用的全文就是她自己那句含糊的話，而「哪些說法」四個字拿去撈長期記憶
    # 什麼都撈不到 —— 她只能回「抱歉我沒對上訊號」。實際發生過（2026-08-13）。
    if is_self:
        try:
            trigger = await reminders.proactive_trigger(replied.id)
        except Exception as e:  # noqa: BLE001
            logger.debug("查主動關心來源失敗（忽略）：%s", e)
            trigger = ""
        if trigger:
            block += (
                f"\n\n## 你當初為什麼主動找他\n"
                f"你會講那句話，是因為他先前說過這件事：\n「{trigger[:1200]}」\n"
                f"（所以他問「哪些」「什麼意思」的時候，指的就是這件事 ——"
                f"直接順著它接下去講，不要說你不記得或沒對上訊號。）"
            )

    # 被回覆的訊息裡的圖片也一起送給 vision —— 別人貼圖、他回覆那張圖問
    # 「這是什麼」的時候，看不到圖就答不出來。
    images: list[tuple[str, str]] = []
    if (
        getattr(config, "REPLY_CONTEXT_IMAGES", True)
        and config.ATTACHMENT_ENABLED
        and not is_self          # 自己貼過的圖不用再看一次
    ):
        images = await attachments.collect_images(
            replied, config.REPLY_CONTEXT_MAX_IMAGES
        )

    short = quoted.replace("\n", " ")
    short = short[:40] + "…" if len(short) > 40 else short
    hint = f"（他回覆了 {'奈奈' if is_self else who} 的訊息「{short}」）"

    return block, hint, images


class Progress:
    """對話處理中的階段提示（讀檔案／搜尋／思考）。

    只用一則訊息，一路 edit 更新，最後在真正的回覆送出前刪掉。
    每個階段都包 try —— 進度提示壞掉絕對不能影響真正的回覆。
    """

    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel
        self.msg: discord.Message | None = None

    async def set(self, text: str) -> None:
        if not config.PROGRESS_ENABLED:
            return
        try:
            if self.msg is None:
                self.msg = await self.channel.send(text)
            else:
                await self.msg.edit(content=text)
        except Exception as e:  # noqa: BLE001
            logger.debug("更新進度訊息失敗（忽略）：%s", e)

    async def clear(self) -> None:
        if self.msg is None:
            return
        try:
            await self.msg.delete()
        except Exception as e:  # noqa: BLE001
            logger.debug("刪除進度訊息失敗（忽略）：%s", e)
        finally:
            self.msg = None


async def schedule_auto_checkin(user_id: int, user_name: str, channel_id: int,
                                intensity: int, trigger_text: str) -> None:
    """情緒訊號 → 排一次事後追蹤關心，時間挑他平常會在線的時段。

    這裡只負責「排」，真正要不要送由投遞時的治理閘門再判斷一次 ——
    因為從現在到那個時間之間，他可能已經自己回來聊了。
    """
    if not (config.AGENT_ENABLED and config.AUTO_CHECKIN_ENABLED):
        return
    if intensity < config.AUTO_CHECKIN_MIN_INTENSITY:
        return

    enabled, last_proactive, _seen = await reminders.get_prefs(user_id)
    if not enabled:
        return
    if last_proactive and (datetime.now() - last_proactive) < timedelta(
            days=config.AUTO_CHECKIN_MIN_GAP_DAYS):
        return
    if await reminders.has_pending_auto_checkin(user_id):
        return   # 已經排了一次，不要疊

    # 只主動關心奈奈真的認識的人，避免對整個伺服器發訊息
    if config.AUTO_CHECKIN_REQUIRE_MEMORY:
        try:
            if not await memory.list_memories(user_id):
                return
        except Exception:  # noqa: BLE001
            return

    not_before = datetime.now() + timedelta(hours=config.AUTO_CHECKIN_DELAY_HOURS)
    due = await reminders.next_active_time(user_id, not_before)
    rid, err = await reminders.add(
        user_id, user_name, channel_id,
        trigger_text[:150] or "關心", due, "none", kind="auto_checkin",
    )
    if err:
        logger.debug("自動關心未排入：%s", err)
        return
    logger.info("💛 已排自動關心 #%s │ %s │ %s（情緒強度 %d）",
                rid, user_name, due.strftime("%m-%d %H:%M"), intensity)


_ECHO_HEAD_RE = re.compile(r"^\s*\[[^\]\n]{1,80}\]\s*說\s*[:：]\s*")
_SEP_LINE_RE = re.compile(r"^\s*(?:-{3,}|—{2,}|\*{3,}|={3,}|─{3,})\s*$")


def strip_echo(text: str, content: str) -> str:
    """砍掉模型偶爾複製在回覆開頭的 `[暱稱] 說：原訊息`。

    Gemma 有時會把 `[暱稱] 說：` 當成要照抄的文件標頭 —— 先把使用者那句話
    重述一遍、加一條 `---`，才開始回答。實測同一句話三次有兩次會這樣。
    system prompt 已經明確交代不要這麼做，這裡是它沒聽話時的保險。

    抓不準邊界時一律原樣送出 —— 寧可讓他看到複述，也不要把真正的回覆剪掉。
    """
    if not text:
        return text
    m = _ECHO_HEAD_RE.match(text)
    if not m:
        return text          # 快路徑：正常回覆不會以這個開頭

    rest = text[m.end():]

    # 用原訊息的尾巴定位複述到哪裡結束。忽略空白比對 —— 模型常把換行併成空格，
    # 而使用者的訊息本身可能有空行，單純切「第一個空行」會切不乾淨。
    flat: list[str] = []
    pos_map: list[int] = []
    for i, ch in enumerate(rest):
        if not ch.isspace():
            flat.append(ch)
            pos_map.append(i)
    tail = re.sub(r"\s+", "", content or "")[-12:]

    cut = -1
    if tail:
        found = "".join(flat).find(tail)
        if found >= 0:
            cut = pos_map[found + len(tail) - 1] + 1
    if cut < 0:
        blank = rest.find("\n\n")     # 定位不到 → 退回切第一個空行
        cut = blank if blank >= 0 else -1
    if cut < 0:
        return text

    lines = rest[cut:].lstrip().split("\n")
    while lines and (not lines[0].strip() or _SEP_LINE_RE.match(lines[0])):
        lines.pop(0)          # 複述後面常跟一條分隔線
    body = "\n".join(lines).strip()

    if not body:
        return text           # 整則都只是複述 → 原樣送出，別送空訊息
    logger.info("✂️ 砍掉回覆開頭的複述（%d → %d 字）", len(text), len(body))
    return body


async def recall_memory(user_id: int, content: str) -> str:
    """撈出跟這句話相關的長期記憶，組成要附在 system prompt 後面的區塊。"""
    if not config.MEMORY_ENABLED or not content:
        return ""
    try:
        hits = await memory.recall(user_id, content)
        # 跨用戶（共享）記憶：關於群組/大家的事，所有人聊天時都帶上
        if config.MEMORY_SHARED_ENABLED and user_id != config.MEMORY_SHARED_ID:
            shared = await memory.recall(config.MEMORY_SHARED_ID, content)
            have = {h.id for h in hits}
            hits = hits + [h for h in shared if h.id not in have]
    except Exception as e:  # noqa: BLE001
        logger.warning("讀取長期記憶失敗：%s", e)
        return ""
    if hits:
        logger.info("🧠 想起 %d 則（含共享）關於 %s 的事", len(hits), user_id)
    return memory.format_for_prompt(hits)


async def learn_from(user_id: int, user_name: str, user_text: str, reply: str) -> None:
    """背景任務：從這一輪對話抽出值得長期記住的事。失敗只記 log，不影響聊天。"""
    if not config.MEMORY_ENABLED:
        return
    if len(user_text.strip()) < config.MEMORY_MIN_CHARS:
        return   # 太短（「你好」「嗯」）不值得跑一次 LLM
    try:
        items = await llm_client.extract_memories(f"[{user_name}] 說：{user_text}")
        if not items:
            return
        # 分流：關於群組/大家的存共享名下，個人私事存個人名下
        personal = [i for i in items if not i.get("shared")]
        shared = [i for i in items if i.get("shared")]
        if personal:
            n = await memory.remember(user_id, user_name, personal)
            if n:
                logger.info("🧠 記住 %d 則（個人）│ %s │ %s", n, user_name,
                            "；".join(i.get("content", "")[:40] for i in personal))
        if shared and config.MEMORY_SHARED_ENABLED:
            n = await memory.remember(config.MEMORY_SHARED_ID, "大家", shared)
            if n:
                logger.info("🌐 記住 %d 則（共享）│ %s", n,
                            "；".join(i.get("content", "")[:40] for i in shared))
    except Exception as e:  # noqa: BLE001
        logger.warning("記憶抽取失敗：%s", e)


async def _noop_pair() -> tuple[str, str]:
    """給 asyncio.gather 用的空佔位（已經讀了連結就不再搜尋）。"""
    return "", ""


async def maybe_fetch_urls(content: str, progress: "Progress | None" = None) -> tuple[str, list[str]]:
    """使用者訊息裡有網址就抓回來讀。回傳 (要塞進 prompt 的區塊, 圖片 data URI)。

    SSRF 防護在 webfetch 裡（擋私有位址 + 逐跳驗證轉址）—— 這台機器上跑著
    一堆內部服務，沒擋的話貼 http://127.0.0.1:10003 就能叫奈奈幫忙偵察內網。
    """
    if not config.FETCH_URL_ENABLED or not content:
        return "", []

    urls = webfetch.find_urls(content)
    if not urls:
        return "", []

    if progress is not None:
        shown = urls[0].split("//", 1)[-1][:45]
        more = f" 等 {len(urls)} 個連結" if len(urls) > 1 else ""
        await progress.set(f"🔗 正在讀取 {shown}{more}…")

    pages = await webfetch.fetch_all(urls)
    ok = [p for p in pages if not p.error]
    logger.info("🔗 讀取連結 │ %d 個成功 / %d 個", len(ok), len(pages))

    images: list[str] = []
    for p in pages:
        for uri in (p.images or []):
            if len(images) < config.MAX_IMAGES_PER_MESSAGE:
                images.append(uri)
    return webfetch.format_for_prompt(pages), images


async def _run_search(query: str, progress: "Progress | None") -> str:
    """實際執行搜尋，並更新階段提示。"""
    if progress is not None:
        await progress.set(f"🔎 正在上網查「{query[:40]}」…")
    results = await websearch.search(query)
    if progress is not None:
        await progress.set(
            f"🔎 找到 {len(results)} 筆資料，正在讀…" if results
            else f"🔎 沒查到「{query[:30]}」的結果…"
        )
    return websearch.format_for_prompt(query, results)


async def maybe_search(content: str, progress: "Progress | None" = None) -> tuple[str, str]:
    """判斷這句話要不要上網。回傳 (搜尋結果區塊, 要交給瀏覽器做的事)。

    兩段式，為了不讓「陪聊」這條主線白白多花一次 LLM 呼叫：
      1. 明確講「查一下 / 搜尋 / google」→ 直接搜，關鍵字就是去掉觸發詞的剩餘部分
      2. 看起來像在問資訊（有問號、什麼、最新…）→ 才問模型要不要上網
      3. 其餘（訴苦、打招呼、閒聊）→ 完全不碰網路

    第 2 步那次呼叫**同時**判斷「這是查資料還是要動手操作網站」——
    搭同一次便車，不為了「要不要開瀏覽器」另外打一次模型。
    """
    if not config.WEB_SEARCH_ENABLED or not content:
        return "", ""

    lowered = content.lower()

    # ① 明確要求
    for kw in config.SEARCH_TRIGGERS:
        if kw in lowered:
            query = re.sub(re.escape(kw), " ", content, flags=re.IGNORECASE)
            for extra in ("奈奈", "nana", "幫我", "一下", "好嗎", "好不好", "謝謝"):
                query = re.sub(re.escape(extra), " ", query, flags=re.IGNORECASE)
            query = re.sub(r"\s+", " ", query).strip(" ，。,.?？!！")
            if not query:
                return "", ""
            logger.info("🔎 明確要求搜尋 │ %s", query[:60])
            return await _run_search(query, progress), ""

    # ② 像在問資訊 → 交給模型判斷（順便判斷要不要開瀏覽器）
    if not any(h in lowered for h in config.QUESTION_HINTS):
        return "", ""

    need, query, want_browser, browse_task = await llm_client.decide_search(content)

    if want_browser and config.BROWSER_ENABLED:
        logger.info("🖥️ 模型判定要動手操作網站 │ %s%s", browse_task[:60],
                    f" │ 搜「{query}」" if query else "")
        # 用 \x00 把「任務」和「搜尋關鍵字」串在一起帶回去（呼叫端會拆開）
        return "", f"{browse_task}\x00{query}"
    if not need:
        return "", ""

    logger.info("🔎 模型判定需要搜尋 │ %s", query[:60])
    return await _run_search(query, progress), ""


async def handle_direct_conversation(
    message: discord.Message,
    replied: discord.Message | None = None,
):
    """處理使用者主動發起的對話（@提及、回覆、私訊、關鍵字）

    replied 是使用者用「回覆」指著的那則訊息（on_message 已經解析過就直接傳進來，
    省一次 API）。沒帶的話這裡自己補 —— !nana 前綴指令那條路不會帶。
    """
    content = message.content
    for mention in message.mentions:
        if mention == bot.user:
            # 奈奈自己的 @提及直接移除（使用者 @奈奈 只是為了觸發對話）
            content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        else:
            # 其他人的 @提及換成暱稱，讓 LLM 知道在講誰
            name = mention.display_name
            content = content.replace(f"<@{mention.id}>", name).replace(f"<@!{mention.id}>", name)
    content = content.strip()

    if replied is None and message.reference is not None:
        replied = await resolve_reference(message)

    progress = Progress(message.channel)

    # ── 讀取附件（文字檔 / 圖片 / PDF）──
    if config.ATTACHMENT_ENABLED and message.attachments:
        names = "、".join(a.filename for a in message.attachments[:3])
        if len(message.attachments) > 3:
            names += f" 等 {len(message.attachments)} 個檔案"
        await progress.set(f"📎 正在讀取 {names}…")
    bundle = await attachments.collect(message)

    # ── 被回覆的那則訊息（內容 + 裡面的圖片）──
    reply_block, reply_hint, reply_images = await build_reply_context(replied)

    if not content:
        if bundle.has_content:
            content = "（傳了附件過來）"
        elif reply_block:
            content = "（他回覆了那則訊息，但沒有多說什麼）"
        else:
            content = "你好"

    user_id = message.author.id
    channel_id = getattr(message.channel, 'id', 0)
    session = conv_manager.get_session(user_id, channel_id)

    logger.info(
        "💬 對話 │ %s (%d): %s%s%s",
        message.author.display_name,
        user_id,
        content[:80],
        f" │ 附件 {bundle.summary()}" if bundle.has_any else "",
        f" │ ↩ 回覆 {replied.author.display_name}" if reply_block and replied else "",
    )

    user_name = message.author.display_name
    chat_content = f"[{user_name}] 說：{content}"

    # ── Agent 工具（提醒等）──
    # 放在讀連結／搜尋之前：如果這句話是要設提醒，就不必再去抓網頁或搜尋了。
    # 管理員 @某人 → 可幫那個人設提醒
    target_user = next(
        (m for m in message.mentions if m != bot.user and not getattr(m, "bot", False)),
        None,
    )
    # ── 這件事是幫誰辦的 ──
    # 兩種講法都認：「幫 @虫合 掛號」（@提及）、或是回覆虫合的訊息說「幫他掛號」。
    # 要資料／要確認時會 tag 這個人，他回的話也能接回任務（見 browser.Session）。
    delegate = target_user
    if delegate is None and replied is not None:
        author = replied.author
        if author != bot.user and not getattr(author, "bot", False) \
                and author.id != message.author.id:
            delegate = author

    is_admin = bool(message.guild and message.author.guild_permissions.manage_guild)
    agent_text = content
    if target_user:   # 把 @提及換成名字，免得 <@id> 干擾 LLM 解析
        for tag in (f"<@{target_user.id}>", f"<@!{target_user.id}>"):
            agent_text = agent_text.replace(tag, target_user.display_name)
    # 最近做過的瀏覽任務：「換一間」「再試一次」這種跟進的話本身沒有資訊，
    # 要把上一個任務給模型看它才知道在講什麼（也才會重新開一個任務而不是純聊天）。
    last_browse = ""
    if config.BROWSER_ENABLED:
        recent = browser.recent_task(user_id)
        if recent:
            last_browse = (f"{recent['task']}（結果：{recent['status']}"
                           f"{'／' + recent['summary'][:80] if recent['summary'] else ''}）")

    agent_result = await agent.handle(
        agent_text, user_id=user_id, user_name=user_name, channel_id=channel_id,
        target_user_id=(target_user.id if target_user else None),
        target_user_name=(target_user.display_name if target_user else None),
        is_admin=is_admin, last_browse=last_browse,
    )
    # agent 接下了瀏覽任務 → 背景開瀏覽器，這條路先讓奈奈回一句「我去看看」。
    # 權限不足時不啟動，但仍讓她用自己的話講（context 已經交代她要說什麼）。
    browse_started = False
    if agent_result.browse_task:
        allowed = True
        if config.BROWSER_ADMIN_ONLY:
            perms = getattr(message.author, "guild_permissions", None)
            allowed = bool(perms and perms.manage_guild)
        if allowed:
            browse_started = True
            # 模型有時會漏掉 url 欄位 —— 使用者訊息裡有網址就直接用，
            # 不然會退回「先去搜」，變成把他貼的網址拿去搜尋
            browse_url = agent_result.browse_url
            if not browse_url:
                found = webfetch.find_urls(content)
                if found:
                    browse_url = found[0]
                    logger.info("🖥️ 模型沒給網址，改用訊息裡的：%s", browse_url)
            asyncio.create_task(run_browse_task(
                message.channel, user_id,
                agent_result.browse_task, browse_url,
                delegate_id=(delegate.id if delegate else None),
                delegate_name=(delegate.display_name if delegate else ""),
                search_hint=agent_result.browse_query))
        else:
            agent_result = agent.AgentResult(
                handled=True,
                context="（瀏覽器操作只開放給管理員，跟他說你這件事沒辦法幫他做）")

    if agent_result.reply:
        await progress.clear()
        session.add_message("user", f"[{user_name}] 說：{content}")
        session.add_message("assistant", agent_result.reply)
        await send_long_message(message.channel, agent_result.reply, reference=message)
        return

    # ── 撈長期記憶 + 讀連結 + 需要的話上網查 ──
    # 有貼連結就以連結為準，不再另外搜尋 —— 對方已經指定要看哪一頁了。
    async with message.channel.typing():
        fetch_block, fetch_images = await maybe_fetch_urls(content, progress)
        memory_context, (search_block, auto_browse) = await asyncio.gather(
            recall_memory(user_id, _recall_query(content, reply_block)),
            maybe_search(content, progress)
            if not fetch_block and not browse_started else _noop_pair(),
        )

    # ── 模型判定這件事要「動手操作網站」才辦得到 → 開瀏覽器 ──
    # agent 那條路已經接下任務時就不重複開（browse_started）。
    if auto_browse and not browse_started and config.BROWSER_ENABLED:
        allowed = True
        if config.BROWSER_ADMIN_ONLY:
            perms = getattr(message.author, "guild_permissions", None)
            allowed = bool(perms and perms.manage_guild)
        if allowed:
            auto_task, _, auto_query = auto_browse.partition("\x00")
            asyncio.create_task(run_browse_task(
                message.channel, user_id, auto_task,
                delegate_id=(delegate.id if delegate else None),
                delegate_name=(delegate.display_name if delegate else ""),
                search_hint=auto_query))
            extra_browse_note = (
                f"\n\n## 你正在幫他做的事\n你判斷這件事要真的上網站操作才辦得到，"
                f"已經開瀏覽器去做了：{auto_browse}\n"
                f"先回一句簡短的「我去幫你看看」讓他知道，**不要說你已經做完**，"
                f"也不要編造結果 —— 做完會另外傳截圖給他。")
        else:
            extra_browse_note = ("\n\n## 說明\n他這件事需要你上網站操作，"
                                 "但這個功能只開放給管理員，跟他說你沒辦法幫他做。")
    else:
        extra_browse_note = ""

    # 網頁裡的圖片（PDF 掃描頁、圖片連結）併進附件的圖片清單一起送
    for uri in fetch_images:
        if len(bundle.images) < config.MAX_IMAGES_PER_MESSAGE:
            bundle.images.append(("網頁圖片", uri))

    # 被回覆訊息裡的圖片也一起送
    for name, uri in reply_images:
        if len(bundle.images) < config.MAX_IMAGES_PER_MESSAGE:
            bundle.images.append((f"被回覆的圖片：{name}", uri))

    # 送給模型的 content：純文字，或含圖片的 content blocks
    payload = attachments.build_content(
        chat_content + reply_block + fetch_block + search_block, bundle
    )

    # 存進歷史的是乾淨的原句 —— 圖片 base64 和搜尋結果都不進 history，
    # 否則每一輪都會重送，context 很快就爆。
    history_text = chat_content
    if bundle.has_content:
        history_text = f"{history_text} {bundle.placeholder()}".strip()
    if reply_hint:
        # 引用全文不進歷史，只留一行提示，後面幾輪才知道剛剛在講哪則訊息
        history_text = f"{reply_hint}{history_text}"
    session.add_message("user", history_text)

    # 檔案指引只在真的有附件時才加 —— 常駐在 system prompt 會讓模型一直惦記著
    # 檔案，連暱稱裡長得像檔名的字（例如「【窮鬼】vito.ipynb」）都會被當成附件。
    extra_prompt = memory_context
    if bundle.has_any:
        extra_prompt += config.ATTACHMENT_PROMPT
    # agent 已經把事情做完了，把結果交給奈奈用自己的語氣講出來
    if agent_result.context:
        extra_prompt += f"\n\n## 你剛剛幫他做的事\n{agent_result.context}"
    # 自動判定要開瀏覽器時，讓她知道自己已經去做了（別讓她編造結果）
    extra_prompt += extra_browse_note

    # 剛剛才上網看過的內容 —— 帶著它，追問才答得出來。
    # 少了這段的話：她捲完整個網站、截圖也傳了，使用者追問「那個演算法有什麼特點」
    # 她卻回「我還沒看到你的演算法內容」。
    if config.BROWSER_ENABLED:
        seen = browser.recent_task(user_id)
        if seen and seen.get("text"):
            extra_prompt += (
                f"\n\n## 你剛剛上網看到的內容\n"
                f"（你去做的事：{seen['task']}）\n"
                f"（網址：{seen.get('url', '')}）\n"
                f"{seen['text'][:2500]}\n\n"
                f"他如果追問這一頁上的東西（上面某個項目、某段內容的細節），"
                f"**就用上面這些內容回答，不要說你沒看到** —— 你剛剛才看完。\n"
                f"上面真的找不到答案時，才說要再去那一頁看一次。")

    async with message.channel.typing():
        if progress.msg is not None:
            await progress.set("💭 正在想怎麼回你…")
        response = await llm_client.generate_support_response(
            user_message=payload,
            conversation_history=session.history[:-1],
            memory_context=extra_prompt,
        )

        # 圖片送失敗（例如 webp 這種 stb_image 不吃的格式）→ 退成純文字再試一次
        if not response and bundle.images:
            logger.warning("含圖片的請求失敗，改用純文字重試（%d 張圖）", len(bundle.images))
            fallback = attachments.strip_images(payload)
            fallback += f"\n\n（附帶了 {len(bundle.images)} 張圖片，但這次沒能看到）"
            response = await llm_client.generate_support_response(
                user_message=fallback,
                conversation_history=session.history[:-1],
                memory_context=extra_prompt,
            )

    await progress.clear()

    # 模型有時會先把他那句話抄一遍才開始回答，砍掉再送出
    response = strip_echo(response, content)

    if response:
        session.add_message("assistant", response)
        await send_long_message(message.channel, response, reference=message)
        # 記憶抽取放背景跑 —— 回覆已經送出去了，不讓它影響回話速度
        asyncio.create_task(
            learn_from(user_id, user_name, content, response)
        )
    else:
        await message.reply(
            f"抱歉，{config.BOT_NAME}現在頭有點暈，等一下再試試好嗎？😵‍💫",
            mention_author=False,
        )


# ═══════════════════════════════════════════════════════
# 被動情緒偵測（所有訊息）
# ═══════════════════════════════════════════════════════


async def handle_emotion_detection(message: discord.Message):
    """偵測所有訊息中的情緒（同一次判斷順便決定要不要按表情）

    危險訊息偵測和表情判斷共用這一次模型呼叫 —— 同一則訊息只檢測一次，
    不會為了表情再打一次（見 config.EMOTION_REACTION_ADDENDUM）。
    """
    if config.MONITORED_CHANNELS and message.channel.id not in config.MONITORED_CHANNELS:
        # 這個頻道不做情緒偵測 → 沒有可以搭便車的判斷，表情走自己那條路
        asyncio.create_task(reactions.maybe_react(message))
        return

    content = message.content.strip()
    if not content:
        return

    if content.startswith(("!", "/", "http", "```")):
        return

    user_name = message.author.display_name
    emotion_result = await llm_client.detect_emotion(
        f"[{user_name}] 說：{content}",
        # 讓它知道最近已經給過這個人哪些表情，別一直重複同一個
        extra_system=reactions.avoid_hint(message.author.id),
    )
    if not emotion_result:
        return

    needs_support = emotion_result.get("needs_support", False)
    intensity = emotion_result.get("intensity", 1)
    emotion = emotion_result.get("emotion", "中性")
    is_danger = emotion_result.get("is_danger", False)
    reason = emotion_result.get("reason", "")

    # ── 表情回應 ──
    # 用剛剛那次判斷的結果，模型說不用按就不按。放背景跑，不擋下面的關懷回覆。
    # already_decided=True：這次判斷是搭便車來的，不必再過「問模型的節流」那關，
    # 但同串／同頻道的上下文規則照樣要過（那才是防刷表情的部分）。
    # is_danger 時無論模型說什麼都不按：prompt 已經交代過，這裡再擋一次，
    # 萬一模型出錯也絕不會在「想死」的訊息上按表情。
    picks = emotion_result.get("emojis") if emotion_result.get("react") else None
    if picks and not is_danger and reactions.wants_reaction(message, already_decided=True):
        asyncio.create_task(reactions.apply(message, picks))

    logger.info(
        "🔍 情緒偵測 │ %s: %s (強度 %d, 支持: %s, 危險: %s) │ %s",
        message.author.display_name,
        emotion,
        intensity,
        needs_support,
        is_danger,
        content[:60],
    )

    # ── 危險訊息：立即警告（不受冷卻限制）──
    if is_danger:
        await send_danger_alert(message, emotion, intensity, reason)

    # ── 排一次事後追蹤關心 ──
    # 跟下面的「立即回覆」是兩回事，所以放在 EMOTION_AUTO_REPLY 檢查之前：
    # 立即回覆可能被關掉，但過幾小時回頭關心一下仍然有價值。
    if needs_support:
        await schedule_auto_checkin(
            message.author.id, message.author.display_name,
            message.channel.id, intensity, content,
        )

    # ── 情緒支持回覆 ──
    if not needs_support or intensity < getattr(config, "EMOTION_THRESHOLD", 2) or not getattr(config, "EMOTION_AUTO_REPLY", True):
        return

    session = conv_manager.get_session(message.author.id, message.channel.id)
    if not session.can_auto_reply():
        return

    session.mark_auto_replied()

    user_name = message.author.display_name
    emotion_context = (
        f"（系統提示：偵測到使用者的情緒為「{emotion}」，強度 {intensity}/5。"
        f"請溫柔地主動關心對方，不要提到你是透過偵測得知的。"
        f"自然地加入對話，就像一個朋友剛好看到訊息一樣。）"
    )

    async with message.channel.typing():
        response = await llm_client.generate_support_response(
            user_message=f"[{user_name}] 說：{content}\n\n{emotion_context}",
        )

    response = strip_echo(response, content)

    if response:
        if intensity >= 4:
            await send_long_message(message.channel, response, reference=message)
        else:
            await send_long_message(message.channel, response)
        logger.info("💛 已主動關懷 %s", message.author.display_name)


# ═══════════════════════════════════════════════════════
# 危險訊息警告
# ═══════════════════════════════════════════════════════


async def send_danger_alert(
    message: discord.Message,
    emotion: str,
    intensity: int,
    reason: str,
):
    """發送危險訊息警告到指定頻道"""
    if not config.ALERT_CHANNEL_ID:
        logger.warning("⚠️ 偵測到危險訊息但未設定 ALERT_CHANNEL_ID")
        return

    alert_channel = bot.get_channel(config.ALERT_CHANNEL_ID)
    if not alert_channel:
        logger.error("❌ 找不到警告頻道 ID: %d", config.ALERT_CHANNEL_ID)
        return
    stats.bump(stats.ALERTS)

    embed = discord.Embed(
        title="🚨 危險訊息警告",
        description="偵測到可能包含自傷或危險訊號的訊息，請管理員關注。",
        color=0xFF0000,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(
        name="👤 使用者",
        value=f"{message.author.mention} (`{message.author.display_name}`)",
        inline=True,
    )
    embed.add_field(
        name="📍 頻道",
        value=f"{message.channel.mention}" if hasattr(message.channel, "mention") else "私訊",
        inline=True,
    )
    embed.add_field(
        name="😢 偵測到的情緒",
        value=f"{emotion}（強度 {intensity}/5）",
        inline=True,
    )

    msg_preview = message.content[:500]
    if len(message.content) > 500:
        msg_preview += "..."
    embed.add_field(name="💬 訊息內容", value=f"```{msg_preview}```", inline=False)
    embed.add_field(name="🔍 判斷原因", value=reason or "無", inline=False)

    if message.guild:
        embed.add_field(
            name="🔗 訊息連結",
            value=f"[點擊跳轉]({message.jump_url})",
            inline=False,
        )

    embed.set_footer(text="⚠️ 請管理員盡速確認 │ 安心專線 1925 ｜ 生命線 1995")

    try:
        await alert_channel.send(embed=embed)
        logger.warning("🚨 已發送危險訊息警告 │ %s", message.author.display_name)
    except Exception as e:
        logger.error("❌ 發送警告失敗: %s", e)

    # 在原頻道溫柔回覆
    try:
        await message.reply(
            f"嘿，我看到你的訊息了。不管你現在經歷什麼，你都不是一個人 💛\n\n"
            f"如果你正在很痛苦的狀態，請撥打以下專線：\n"
            f"📞 **安心專線 1925**（24小時）\n"
            f"📞 **生命線 1995**\n"
            f"📞 **張老師專線 1980**\n\n"
            f"我在這裡陪你，隨時都可以找我說話 🌸",
            mention_author=False,
        )
    except Exception as e:
        logger.error("❌ 無法回覆危險訊息: %s", e)


# ═══════════════════════════════════════════════════════
# 斜線指令（py-cord 風格）
# ═══════════════════════════════════════════════════════


@bot.slash_command(name="chat", description=f"和{config.BOT_NAME}聊天 💬")
@discord.option("message", type=str, description="你想對奈奈說的話")
async def slash_chat(
    ctx: discord.ApplicationContext,
    message: str,
):
    """透過斜線指令和奈奈聊天"""
    user_id = ctx.author.id
    channel_id = ctx.channel_id or 0
    session = conv_manager.get_session(user_id, channel_id)

    logger.info("💬 /chat │ %s: %s", ctx.author.display_name, message[:80])

    session.add_message("user", message)
    await ctx.defer()

    response = await llm_client.generate_support_response(
        user_message=message,
        conversation_history=session.history[:-1],
    )

    if response:
        session.add_message("assistant", response)
        await ctx.followup.send(response)
    else:
        await ctx.followup.send(
            f"抱歉，{config.BOT_NAME}現在頭有點暈，等一下再試試好嗎？😵‍💫"
        )


@bot.slash_command(name="mood", description="取得一則心情小提醒 🌈")
async def slash_mood(ctx: discord.ApplicationContext):
    import random

    tips = [
        "🌿 試著深呼吸 3 次，慢慢吸氣、慢慢吐氣",
        "☕ 給自己泡一杯溫暖的飲料吧",
        "🎵 聽一首你喜歡的歌",
        "🚶 起身走動一下，看看窗外的風景",
        "📝 把現在的心情寫下來，不管寫什麼都好",
        "💤 如果累了，允許自己休息一下",
        "🌸 跟一個你信任的人聊聊天",
        "🎨 做一件讓你感到快樂的小事",
        "🌙 今晚早點睡，明天又是新的一天",
        "🤗 給自己一個擁抱，你已經很努力了",
    ]

    embed = discord.Embed(title="🌈 心情小提醒", description=random.choice(tips), color=0x87CEEB)
    embed.set_footer(text=f"— {config.BOT_NAME} 💛")
    await ctx.respond(embed=embed)


@bot.slash_command(name="reset", description="重置和奈奈的對話記錄 🔄")
async def slash_reset(ctx: discord.ApplicationContext):
    conv_manager.clear_session(ctx.author.id, ctx.channel_id or 0)
    note = ""
    if config.MEMORY_ENABLED:
        note = "\n（長期記憶還留著，要一起清請用 `/forget`）"
    await ctx.respond(
        f"好的，{config.BOT_NAME}把這次的對話忘掉了～🌱\n讓我們重新開始吧！{note}",
        ephemeral=True,
    )


@bot.slash_command(name="reminders", description="查看你設定的提醒 ⏰")
async def slash_reminders(ctx: discord.ApplicationContext):
    if not config.AGENT_ENABLED:
        await ctx.respond("提醒功能目前是關閉的喔。", ephemeral=True)
        return

    items = await reminders.list_for(ctx.author.id)
    if not items:
        await ctx.respond(
            "你目前沒有任何提醒喔～\n直接跟我說「**30分鐘後提醒我倒垃圾**」就可以設定 ⏰",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="⏰ 你的提醒",
        description=f"共 {len(items)} 個。想取消的話跟我說「取消○○的提醒」就好。",
        color=0xFFB7C5,
    )
    for r in items[:25]:
        rep = f"　🔁 {r.repeat_str()}" if r.repeat_str() else ""
        label = "💛 主動關心" if r.kind == "checkin" else r.text
        icon = "💛" if r.kind == "checkin" else "⏰"
        embed.add_field(name=f"{icon} #{r.id}　{label}",
                        value=f"{r.when_str()}{rep}", inline=False)
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(name="voice_wake", description="開關語音喚醒模式（要不要先叫「奈奈」）🌸")
async def slash_voice_wake(
    ctx: discord.ApplicationContext,
    mode: str = "查看",
):
    mode = (mode or "查看").strip()
    if mode not in ("開啟", "關閉", "查看", "on", "off"):
        mode = "查看"
    if mode in ("on",):
        mode = "開啟"
    elif mode in ("off",):
        mode = "關閉"
    if mode == "查看":
        cur = "🌸 開啟中（要先叫「奈奈」）" if config.VOICE_WAKE_ENABLED else "🗣️ 關閉中（直接說話就回應）"
        await ctx.respond(
            f"語音喚醒目前：**{cur}**\n"
            f"喚醒詞：{'、'.join(config.VOICE_WAKE_WORDS[:6])}…\n"
            f"用 `/voice_wake 開啟` 或 `/voice_wake 關閉` 切換。",
            ephemeral=True)
        return

    on = (mode == "開啟")
    config.VOICE_WAKE_ENABLED = on
    bridge.awake_until = 0.0   # 切換時清掉喚醒視窗
    config.save_settings({"VOICE_WAKE_ENABLED": on})
    logger.info("🌸 語音喚醒模式 → %s", "開啟" if on else "關閉")
    if on:
        msg = ("🌸 **語音喚醒已開啟**\n"
               "現在要先叫「**奈奈**」(或奶奶/娜娜) 才會回應，喚醒後 60 秒內可連續對話、"
               "還能用語音設提醒／記事／總結。\n"
               "-# 提醒：辨識受環境影響，安靜、靠近麥克風、清楚說效果較好。")
    else:
        msg = ("🗣️ **語音喚醒已關閉**\n"
               "現在直接說話奈奈就會回應，不用先叫名字（一般聊天用這個比較順）。")
    await ctx.respond(msg, ephemeral=True)


@bot.slash_command(name="checkin_status", description="查看主動關心的狀態 💛")
# 一定要用 decorator 宣告選項，不能寫成 `user: discord.Option(discord.Member, …)`：
# 這個檔案開頭有 from __future__ import annotations，所有註解都變成字串，py-cord
# 讀不到裡面的 discord.Member，只好退成 string 選項 —— 結果 Discord 給的是文字框
# 而不是成員選單（tag 不到人），而且傳進來的是 str，target.id 直接 AttributeError。
@discord.option("user", type=discord.Member, description="要查誰（留空看自己）", required=False)
async def slash_checkin_status(
    ctx: discord.ApplicationContext,
    user: discord.Member = None,
):
    target = user or ctx.author
    # 只有管理員能查別人的，否則等於洩漏他人的作息。
    # guild_permissions 用 getattr 取 —— 私訊裡 ctx.author 是 User，沒有這個屬性，
    # 直接取會噴 AttributeError，使用者只會看到「該申請未受回應」。
    perms = getattr(ctx.author, "guild_permissions", None)
    if target.id != ctx.author.id and not (perms and perms.manage_guild):
        await ctx.respond("只能查自己的喔～", ephemeral=True)
        return

    enabled, last_pro, last_seen = await reminders.get_prefs(target.id)
    hours = await reminders.active_hours(target.id)
    mems = len(await memory.list_memories(target.id))
    pending = [r for r in await reminders.list_for(target.id)
               if r.kind in ("checkin", "auto_checkin")]

    if hours:
        hrs_txt = "、".join(f"{h:02d}點" for h in sorted(hours))
    else:
        hrs_txt = (f"樣本不足，會用預設 "
                   f"{config.AUTO_CHECKIN_FALLBACK_HOURS[0]:02d}–"
                   f"{config.AUTO_CHECKIN_FALLBACK_HOURS[-1]:02d}點")

    nxt = await reminders.next_active_time(
        target.id, datetime.now() + timedelta(hours=config.AUTO_CHECKIN_DELAY_HOURS))

    embed = discord.Embed(title=f"💛 主動關心狀態 — {target.display_name}",
                          color=0xFFB7C5)
    embed.add_field(name="開關", value="✅ 開啟" if enabled else "🚫 已關閉", inline=True)
    embed.add_field(name="奈奈記得的事", value=f"{mems} 則", inline=True)
    embed.add_field(name="待送的關心", value=f"{len(pending)} 個", inline=True)
    embed.add_field(name="學到的活躍時段", value=hrs_txt, inline=False)
    embed.add_field(name="若現在觸發，會挑這個時間",
                    value=f"{nxt:%Y-%m-%d %H:%M}", inline=False)
    embed.add_field(
        name="上次主動關心",
        value=f"{last_pro:%Y-%m-%d %H:%M}" if last_pro else "還沒有過", inline=True)
    embed.add_field(
        name="最後出現", value=f"{last_seen:%Y-%m-%d %H:%M}" if last_seen else "無紀錄",
        inline=True)

    blockers = []
    if not enabled:
        blockers.append("已關閉主動關心")
    if config.AUTO_CHECKIN_REQUIRE_MEMORY and not mems:
        blockers.append("奈奈還不認識他（沒有長期記憶）")
    if last_pro and (datetime.now() - last_pro) < timedelta(
            days=config.AUTO_CHECKIN_MIN_GAP_DAYS):
        blockers.append(f"距上次不足 {config.AUTO_CHECKIN_MIN_GAP_DAYS} 天")
    if blockers:
        embed.add_field(name="⚠️ 目前不會送出的原因",
                        value="\n".join(f"• {b}" for b in blockers), inline=False)

    await ctx.respond(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════
# 私人聊天室
# ═══════════════════════════════════════════════════════

room_group = bot.create_group("room", "和奈奈的私人聊天室 🌸")


def _register_room(channel_id: int) -> None:
    if channel_id not in config.PRIVATE_ROOMS:
        config.PRIVATE_ROOMS.append(channel_id)
        config.save_settings()


def _unregister_room(channel_id: int) -> None:
    if channel_id in config.PRIVATE_ROOMS:
        config.PRIVATE_ROOMS.remove(channel_id)
        config.save_settings()


def _my_open_rooms(user_id: int) -> list[int]:
    """這個人名下還活著的房間 id。順手把已經被刪掉的從名單清掉。

    靠討論串名字結尾的 `-{user_id}` 認人 —— 重啟後記憶體是空的，
    但名字還在，這樣才認得出哪一間是誰的。
    """
    alive: list[int] = []
    for cid in list(config.PRIVATE_ROOMS):
        ch = bot.get_channel(cid)
        if ch is None:
            _unregister_room(cid)          # 頻道已刪 → 名單也清掉
            continue
        if isinstance(ch, discord.Thread) and ch.archived:
            continue
        if f"-{user_id}" in (getattr(ch, "name", "") or ""):
            alive.append(cid)
    return alive


@room_group.command(name="open", description="開一間只有你和奈奈的私人聊天室 🌸")
@discord.option("topic", type=str, description="想聊什麼（選填，會寫在開場）", required=False)
async def room_open(ctx: discord.ApplicationContext, topic: str = ""):
    """在目前頻道底下開一個私密討論串，裡面奈奈會回應每一句話。"""
    if ctx.guild is None:
        await ctx.respond(
            f"你現在就是在私訊我了，這裡本來就只有我們兩個 🌸\n直接說吧，我在聽。",
            ephemeral=True)
        return

    parent = ctx.channel
    # 討論串裡不能再開討論串
    if isinstance(parent, discord.Thread):
        parent = parent.parent
    if not isinstance(parent, discord.TextChannel):
        await ctx.respond("這個頻道沒辦法開討論串，換一個文字頻道試試 🌸", ephemeral=True)
        return

    mine = _my_open_rooms(ctx.author.id)
    if len(mine) >= config.PRIVATE_ROOM_MAX_PER_USER:
        existing = bot.get_channel(mine[0])
        await ctx.respond(
            f"你已經有一間了：{existing.mention}\n聊完想關掉的話在裡面用 `/room close` 🌸",
            ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    # 名字裡帶 id，重啟後才認得出哪一間是誰的
    name = f"🌸 奈奈與{ctx.author.display_name}-{ctx.author.id}"[:100]
    try:
        thread = await parent.create_thread(
            name=name,
            type=discord.ChannelType.private_thread,
            auto_archive_duration=config.PRIVATE_ROOM_ARCHIVE_MINUTES,
            invitable=False,
        )
    except discord.Forbidden:
        await ctx.respond(
            "我沒有在這個頻道開私密討論串的權限 😢\n"
            "請管理員給我「建立私人討論串」和「在討論串中發送訊息」兩個權限，"
            "或是直接**私訊我**也一樣可以聊。", ephemeral=True)
        return
    except discord.HTTPException as e:
        logger.warning("開私人聊天室失敗：%s", e)
        await ctx.respond(f"開不起來 😵‍💫（{str(e)[:100]}）\n直接私訊我也可以喔。",
                          ephemeral=True)
        return

    _register_room(thread.id)
    logger.info("🌸 開了私人聊天室 #%d │ %s（%d）", thread.id,
                ctx.author.display_name, ctx.author.id)

    try:
        await thread.add_user(ctx.author)
    except discord.HTTPException:
        pass

    embed = discord.Embed(
        title="🌸 這裡只有我們兩個",
        description=(
            f"{ctx.author.mention} 這是你的私人聊天室。\n"
            f"**在這裡你不用 @我**，講什麼我都會回你。"
        ),
        color=0xFFB7C5,
    )
    embed.add_field(
        name="可以做的事",
        value=("• 直接說話就好，想聊什麼都可以\n"
               "• 傳檔案、圖片、貼網址給我看\n"
               "• 「30分鐘後提醒我吃藥」這類也照樣有用\n"
               "• 要我幫你上網辦事（掛號、查詢）也可以，"
               "**身分證這類資料在這裡給比較安全**"),
        inline=False,
    )
    embed.add_field(
        name="聊完了",
        value="用 `/room close` 關掉，或就放著 —— "
              f"{config.PRIVATE_ROOM_ARCHIVE_MINUTES // 60} 小時沒講話會自動封存。",
        inline=False,
    )
    embed.set_footer(text=f"— {config.BOT_NAME}")

    opener = f"{ctx.author.mention}"
    await thread.send(content=opener, embed=embed)

    if topic:
        # 有講主題就讓她直接接話，不要讓人再打一次
        session = conv_manager.get_session(ctx.author.id, thread.id)
        session.add_message("user", f"[{ctx.author.display_name}] 說：{topic}")
        async with thread.typing():
            reply = await llm_client.generate_support_response(
                user_message=f"[{ctx.author.display_name}] 說：{topic}",
                memory_context=await recall_memory(ctx.author.id, topic),
            )
        reply = strip_echo(reply, topic)
        if reply:
            session.add_message("assistant", reply)
            await send_long_message(thread, reply)

    await ctx.respond(f"開好了 → {thread.mention} 🌸", ephemeral=True)


@room_group.command(name="close", description="關掉這間私人聊天室 👋")
@discord.option("delete", type=bool, required=False,
                description="連整個房間一起刪掉（預設只封存，紀錄還留著）")
async def room_close(ctx: discord.ApplicationContext, delete: bool = False):
    ch = ctx.channel
    if not isinstance(ch, discord.Thread) or ch.id not in config.PRIVATE_ROOMS:
        await ctx.respond("這裡不是私人聊天室喔。要在房間裡面用這個指令 🌸", ephemeral=True)
        return

    _unregister_room(ch.id)
    conv_manager.clear_session(ctx.author.id, ch.id)
    logger.info("🌸 關閉私人聊天室 #%d │ %s │ delete=%s",
                ch.id, ctx.author.display_name, delete)

    if delete:
        await ctx.respond("好，那我把這間整個收掉 👋 想聊隨時再開一間 💛")
        try:
            await ch.delete()
            return
        except discord.HTTPException as e:
            logger.warning("刪除討論串失敗：%s", e)
            await ctx.followup.send(
                "我刪不掉這個討論串（少了「管理討論串」權限）😢\n"
                "不過我已經不會在這裡回話了，你可以手動刪除它。", ephemeral=True)
            return

    await ctx.respond("好，那我把這裡收起來 👋 想聊隨時再開一間就好 💛")

    # 一定要 lock：只封存的話任何人再講一句話就會自動解除封存，
    # 但那時我已經不在這裡回話了 —— 會變成一間沒人應答的空房間。
    try:
        await ch.edit(archived=True, locked=True)
    except discord.HTTPException as e:
        # 之前這裡是 logger.debug，失敗完全看不出來，使用者只會覺得「指令沒用」
        logger.warning("封存討論串失敗：%s", e)
        try:
            await ctx.followup.send(
                f"我沒辦法把這個討論串封存起來（{str(e)[:80]}）—— "
                f"少了「管理討論串」權限。\n"
                f"**但我已經不會在這裡回話了**，你可以自己把它封存或刪掉，"
                f"或用 `/room close delete:true` 讓我直接刪。",
                ephemeral=True)
        except discord.HTTPException:
            pass


# ═══════════════════════════════════════════════════════
# 瀏覽器操作
# ═══════════════════════════════════════════════════════


_URL_IN_TEXT = re.compile(r"(https?://\S+)")


def _safe_links(text: str) -> str:
    """把網址用 <> 包起來。

    Discord 的自動連結會把網址後面緊接的中文一起吃進去（「開了 https://…?q=在嘉義找診所
    （這件事是幫…）」整段變成一個藍色連結）。<> 同時也不會產生預覽卡片。
    """
    return _URL_IN_TEXT.sub(lambda m: f"<{m.group(1)}>", text or "")


def _browse_embed(result: browser.Result, task: str) -> discord.Embed:
    """把瀏覽結果做成 embed。截圖另外用 discord.File 附上。"""
    look = {
        "done": ("✅ 幫你弄好了", 0x4CAF50),
        "need_confirm": ("✋ 送出前先問你一下", 0xFFA500),
        "need_input": ("❓ 我需要一點資料", 0xFFA500),
        "blocked": ("🚧 這裡我沒辦法幫你", 0xFF6B6B),
        "max_steps": ("⏳ 還沒做完就先停下來了", 0xFFA500),
        "error": ("😵‍💫 中途出錯了", 0xFF6B6B),
    }
    title, color = look.get(result.status, ("🖥️ 瀏覽結果", 0xFFB7C5))

    desc = result.question or result.summary or "（沒有補充說明）"
    if result.status == "need_input":
        desc += ("\n\n-# 🔒 不想讓別人看到的話按下面的「私密輸入」，"
                 "打的內容不會出現在頻道裡；直接在頻道打也可以。")
    embed = discord.Embed(title=title, description=desc[:3800], color=color)
    embed.add_field(name="你交代的事", value=task[:1000] or "—", inline=False)

    if result.steps:
        lines = [f"{s.n}. {_safe_links(s.detail)}" for s in result.steps[-6:]]
        embed.add_field(name=f"我做了什麼（共 {len(result.steps)} 步）",
                        value="\n".join(lines)[:1000], inline=False)
    if result.url:
        embed.add_field(name="現在停在", value=f"<{result.url[:900]}>", inline=False)
    if result.linger:
        # 做完不等於畫面沒了 —— 不講的話沒人知道還可以接著自己用
        embed.add_field(
            name="🖥️ 畫面我先留著",
            value=("到語音頻道的「活動」→ 奈奈的操作台，可以繼續看這一頁，"
                   "也可以自己點、自己打字（她已經停手了，不會跟你搶）。\n"
                   f"-# 沒人在看就會自動收掉，最多留 "
                   f"{config.BROWSER_LINGER_MAX_S // 60} 分鐘。"),
            inline=False)
    if result.shot:
        embed.set_image(url="attachment://nana-browse.png")
        embed.set_footer(text=f"— {config.BOT_NAME}｜畫面截圖如上")
    else:
        # 不要在沒有圖的時候還寫「畫面截圖如上」—— 會讓人以為圖掉了
        embed.set_footer(text=f"— {config.BOT_NAME}｜這次沒能拍到畫面")
    return embed


class ConfirmBrowse(discord.ui.View):
    """送出前的確認按鈕。

    交代任務的人、以及「這件事是幫誰辦的」那個人，兩邊都按得動 ——
    幫別人掛號時，讓當事人自己點頭比較合理。其他人一律按不動。
    """

    def __init__(self, user_id: int, task: str, delegate_id: int | None = None):
        super().__init__(timeout=config.BROWSER_SESSION_IDLE_S)
        self.user_id = user_id
        self.task = task
        self.delegate_id = delegate_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = {self.user_id} | ({self.delegate_id} if self.delegate_id else set())
        if interaction.user and interaction.user.id in allowed:
            return True
        await interaction.response.send_message(
            "這是別人的事情，只有當事人能決定要不要送出喔 🌸", ephemeral=True)
        return False

    async def _finish(self, interaction: discord.Interaction, *,
                      confirmed: bool, always: bool = False):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        self.stop()
        if always:
            try:
                await interaction.channel.send(
                    f"<@{self.user_id}> 好，這個任務接下來我就不再一個一個問了 🏃\n"
                    f"-# 但**需要你給資料的時候還是會停下來問你**（身分證、生日這類我不會自己填）。")
            except discord.HTTPException:
                pass
        result = await browser.resume(self.user_id, confirmed=confirmed, always=always)
        await deliver_browse(interaction.channel, self.user_id, result, task=self.task,
                             delegate_id=self.delegate_id)

    @discord.ui.button(label="確認送出", style=discord.ButtonStyle.danger, emoji="✅")
    async def yes(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await self._finish(interaction, confirmed=True)

    @discord.ui.button(label="一律允許", style=discord.ButtonStyle.primary, emoji="🏃")
    async def always(self, _button: discord.ui.Button, interaction: discord.Interaction):
        """這個任務剩下的送出都不再問。

        只放行「要不要按下去」；缺資料一樣會停下來問本人，
        個資不能自己編、內網不能去這些紅線也都還在。
        """
        await self._finish(interaction, confirmed=True, always=True)

    @discord.ui.button(label="不要送出", style=discord.ButtonStyle.secondary, emoji="✋")
    async def no(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await self._finish(interaction, confirmed=False)


class BrowseInputModal(discord.ui.Modal):
    """私密輸入視窗：打進去的字只有他自己看得到，不會出現在頻道裡。

    身分證、生日、驗證碼這種東西不該叫人在公開頻道打出來 ——
    modal 送出後直接進瀏覽器，訊息紀錄裡什麼都不留。
    """

    def __init__(self, owner_id: int, delegate_id: int | None,
                 task: str, question: str):
        super().__init__(title="🔒 私密輸入（只有你看得到）")
        self.owner_id = owner_id
        self.delegate_id = delegate_id
        self.task = task
        self.add_item(discord.ui.InputText(
            label=(question[:44] or "請輸入奈奈需要的資料"),
            placeholder="打在這裡，別人看不到",
            style=discord.InputTextStyle.short,
            max_length=200,
            required=True,
        ))

    async def callback(self, interaction: discord.Interaction):
        value = (self.children[0].value or "").strip()
        if not value:
            await interaction.response.send_message("沒收到內容，再試一次好嗎？",
                                                    ephemeral=True)
            return
        logger.info("🔒 收到私密輸入 │ user=%d │ %d 字", interaction.user.id, len(value))
        await interaction.response.send_message(
            "收到了，我繼續弄 🔒（你剛剛打的東西沒有出現在頻道裡）", ephemeral=True)

        try:
            result = await browser.resume(interaction.user.id, confirmed=True,
                                          extra=value)
        except Exception as e:  # noqa: BLE001
            logger.warning("私密輸入接續失敗：%s", e)
            result = browser.Result(status="error",
                                    summary=f"中途出錯了：{str(e)[:150]}")
        await deliver_browse(interaction.channel, self.owner_id, result,
                             task=self.task, delegate_id=self.delegate_id)


class PrivateInput(discord.ui.View):
    """「🔒 私密輸入」按鈕。只有當事人（或交代的人）按得動。"""

    def __init__(self, owner_id: int, task: str, question: str,
                 delegate_id: int | None = None):
        super().__init__(timeout=config.BROWSER_SESSION_IDLE_S)
        self.owner_id = owner_id
        self.delegate_id = delegate_id
        self.task = task
        self.question = question

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = {self.owner_id} | ({self.delegate_id} if self.delegate_id else set())
        if interaction.user and interaction.user.id in allowed:
            return True
        await interaction.response.send_message(
            "這是別人的事情，只有當事人能填喔 🌸", ephemeral=True)
        return False

    @discord.ui.button(label="私密輸入", style=discord.ButtonStyle.primary, emoji="🔒")
    async def private(self, _button: discord.ui.Button,
                      interaction: discord.Interaction):
        await interaction.response.send_modal(
            BrowseInputModal(self.owner_id, self.delegate_id,
                             self.task, self.question))


async def deliver_browse(dest, user_id: int, result: browser.Result, *, task: str,
                         delegate_id: int | None = None):
    """把結果（含截圖）送到頻道。停在確認點時附上確認按鈕。

    delegate_id：這件事是幫誰辦的。需要資料或要確認送出時，**tag 的是他**
    —— 別人的身分證只有本人給得出來，也只有本人該點頭。
    """
    if dest is None:
        return
    embed = _browse_embed(result, task)

    # 畫面上已經有個資（身分證／生日填進去了）→ 公開頻道就不貼那張圖
    private_here = isinstance(dest, discord.DMChannel) or (
        getattr(dest, "id", 0) in config.PRIVATE_ROOMS)
    shot = result.shot
    if shot and result.sensitive and not private_here:
        shot = None
        embed.set_image(url=None)
        embed.add_field(
            name="🔒 截圖沒有貼出來",
            value=("畫面上有你的個人資料，這裡是公開頻道所以我不貼。\n"
                   "想看畫面的話用 `/room open` 開個私人聊天室，或直接私訊我 🌸"),
            inline=False)

    files = []
    if shot:
        files.append(discord.File(io.BytesIO(shot), filename="nana-browse.png"))

    view = None
    if result.status == "need_confirm":
        view = ConfirmBrowse(user_id, task, delegate_id=delegate_id)
    elif result.status == "need_input":
        # 給一個私密輸入的入口 —— 身分證這種不該叫人在頻道裡打出來
        view = PrivateInput(user_id, task, result.question, delegate_id=delegate_id)

    # 要資料／要確認 → 找當事人；純粹報告結果 → 回報給交代的人
    needs_person = result.status in ("need_input", "need_confirm")
    if needs_person and delegate_id:
        who = f"<@{delegate_id}>"
        note = f"\n-# 這是 <@{user_id}> 請我幫你處理的，需要你本人回覆才能繼續 🌸"
    else:
        who = f"<@{user_id}>"
        note = ""

    try:
        await dest.send(content=who + note, embed=embed, files=files, view=view)
    except discord.HTTPException as e:
        logger.warning("送出瀏覽結果失敗：%s", e)
        try:
            await dest.send(f"{who} {result.summary or result.question}"[:1900])
        except discord.HTTPException:
            pass


class BrowseProgress(discord.ui.View):
    """任務進度：**整個過程只用一則訊息**，用 ◀ ▶ 翻頁看每一步。

    以前是每一步發一則帶截圖的訊息 —— 看得到過程，但十幾步就把頻道洗掉了。
    現在所有步驟收在同一則裡：跑的時候自動停在最新一步，想回頭看就按方向鍵，
    紀錄全部留著（任務結束後按鈕還能繼續翻）。
    """

    def __init__(self, dest, user_id: int, task: str) -> None:
        super().__init__(timeout=None)      # 任務結束後還要能翻，不設逾時
        self.dest = dest
        self.user_id = user_id
        self.task = task
        self.msg: discord.Message | None = None
        self.pages: list[tuple[browser.Step, bytes | None]] = []
        self.cursor = -1          # -1 = 跟著最新一步跑
        self.done = False
        self._last_live = 0.0     # 即時畫面的節流時間戳

    # ── 內部：組出目前那一頁 ──

    def _at(self) -> int:
        return len(self.pages) - 1 if self.cursor < 0 else min(
            self.cursor, len(self.pages) - 1)

    def _render(self) -> tuple[str, discord.File | None]:
        if not self.pages:
            return (f"<@{self.user_id}> 🖥️ 開始了…\n"
                    f"-# 「{self.task[:110]}」｜想知道進度隨時問我「進度？」"), None

        i = self._at()
        step, shot = self.pages[i]
        head = "✅ **做完了**" if self.done else "🖥️ **進行中**"
        pos = f"第 {step.n} 步" + (f"（共 {len(self.pages)} 步，看第 {i + 1} 頁）"
                                   if len(self.pages) > 1 else "")
        follow = "" if self.cursor < 0 else "　·　⏭ 回到最新"
        body = (f"<@{self.user_id}> {head}　{pos}{follow}\n"
                f"{_safe_links(step.detail[:250])}\n"
                f"-# 「{self.task[:110]}」")
        f = (discord.File(io.BytesIO(shot), filename=f"step-{step.n}.png")
             if shot else None)
        return body, f

    def _sync_buttons(self) -> None:
        i = self._at()
        many = len(self.pages) > 1
        self.prev.disabled = not many or i <= 0
        self.next.disabled = not many or i >= len(self.pages) - 1
        self.latest.disabled = self.cursor < 0

    async def _paint(self, interaction: discord.Interaction | None = None) -> None:
        content, f = self._render()
        self._sync_buttons()

        # 附件的用法很容易寫錯（我第一版就錯了，訊息永遠停在「開始了…」）：
        #   • attachments= 只吃 discord.Attachment（會呼叫 a.to_dict()），
        #     塞 discord.File 進去會噴 AttributeError —— 而且不是 HTTPException，
        #     很容易被漏接、然後靜靜地什麼都不做
        #   • 要換成新圖：file=<File> 再配 attachments=[] 清掉舊的
        #     （只給 file 的話 py-cord 會「保留舊附件再加一張」，會越疊越多）
        kw: dict = {"content": content, "view": self, "attachments": []}
        if f is not None:
            kw["file"] = f

        try:
            if interaction is not None:
                await interaction.response.edit_message(**kw)
            elif self.msg is not None:
                await self.msg.edit(**kw)
        except Exception as e:  # noqa: BLE001
            # 這裡刻意用 warning 不用 debug —— 上一版壓成 debug，
            # 結果進度不動卻完全查不到原因
            logger.warning("更新進度訊息失敗：%s: %s", type(e).__name__, e)

    # ── 對外 ──

    async def start(self) -> None:
        content, _ = self._render()
        self._sync_buttons()
        try:
            self.msg = await self.dest.send(content=content, view=self)
        except discord.HTTPException:
            self.msg = None

    async def update(self, step: browser.Step, shot: bytes | None = None) -> None:
        """做完一步就更新那一則訊息（不發新訊息，所以不會洗頻）。"""
        self.pages.append((step, shot))
        if len(self.pages) > config.BROWSER_MAX_KEPT_SHOTS:
            self.pages.pop(0)      # 太舊的丟掉，避免無限長
        if self.cursor >= 0:
            return                 # 使用者正在回頭翻，不要把畫面搶走
        await self._paint()

    async def live(self, shot: bytes) -> None:
        """等模型想下一步的空檔送來的即時畫面 —— 就地換圖，不發新訊息。

        使用者正在回頭翻頁時不要動（cursor >= 0），否則畫面會被搶走。
        也做節流：Discord 的訊息編輯是 5 次/5 秒。
        """
        if self.cursor >= 0 or not self.pages or self.msg is None:
            return
        now = time.time()
        if now - self._last_live < max(config.BROWSER_LIVE_INTERVAL, 2.0):
            return
        self._last_live = now
        step, _old = self.pages[-1]
        self.pages[-1] = (step, shot)      # 把最後一頁的畫面換成最新的
        await self._paint()

    async def finish(self) -> None:
        """任務結束 —— 訊息留著當紀錄，按鈕還能翻。"""
        self.done = True
        if self.pages:
            await self._paint()
        elif self.msg is not None:
            try:
                await self.msg.delete()   # 一步都沒跑就沒有紀錄價值
            except discord.HTTPException:
                pass

    # ── 按鈕 ──

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "這是別人的任務紀錄喔 🌸", ephemeral=True)
        return False

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, _b: discord.ui.Button, interaction: discord.Interaction):
        self.cursor = max(0, self._at() - 1)
        await self._paint(interaction)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, _b: discord.ui.Button, interaction: discord.Interaction):
        self.cursor = min(len(self.pages) - 1, self._at() + 1)
        await self._paint(interaction)

    @discord.ui.button(emoji="⏭", label="最新", style=discord.ButtonStyle.primary)
    async def latest(self, _b: discord.ui.Button, interaction: discord.Interaction):
        self.cursor = -1
        await self._paint(interaction)


def _voice_for(dest) -> "discord.VoiceClient | None":
    """這個頻道所在的伺服器裡，奈奈有沒有在語音頻道。"""
    guild = getattr(dest, "guild", None)
    if guild is None:
        return None
    vc = guild.voice_client
    return vc if vc and vc.is_connected() else None


async def narrate_step(dest, step: browser.Step) -> None:
    """在語音頻道用聲音講一句她正在做什麼。

    只有奈奈已經在語音頻道（有人 /join 過）才會念，而且句子要短 ——
    TTS 一句 2~4 秒，太長會排隊排到下一步都做完了。
    """
    if not config.BROWSER_NARRATE:
        return
    if _voice_for(dest) is None:
        return
    line = re.sub(r"https?://\S+", "網址", step.detail)      # 別把網址念出來
    line = re.sub(r"（[^）]*）", "", line).strip()             # 去掉括號註記
    line = line[:60]
    if not line:
        return
    try:
        await asyncio.to_thread(bridge.speak, f"第{step.n}步，{line}")
    except Exception as e:  # noqa: BLE001
        logger.debug("旁白失敗（忽略）：%s", e)


async def run_browse_task(dest, user_id: int, task: str, url: str = "",
                          delegate_id: int | None = None,
                          delegate_name: str = "", search_hint: str = ""):
    """背景執行一次瀏覽任務，做完把結果和截圖送到頻道。

    delegate_id：這件事是幫誰辦的（「幫 @虫合 掛號」時就是虫合）。
    需要資料或要確認送出時會 tag 他，他回的話也能接回這個任務。
    """
    if getattr(dest, "id", None):
        _last_channel_of[user_id] = dest.id
    logger.info("🖥️ 開始瀏覽任務 │ user=%d │ %s%s%s", user_id, task[:60],
                f" │ {url}" if url else "",
                f" │ 幫 {delegate_name}({delegate_id}) 辦" if delegate_id else "")
    stats.bump(stats.BROWSE)
    prog = BrowseProgress(dest, user_id, task)
    await prog.start()
    # on_closed 有可能在任務結束「之後」才被呼叫（畫面留著的情況），
    # 那時候 result 這個區域變數已經不在它的閉包範圍裡了，所以用一個小盒子帶過去
    result_holder: dict = {}
    try:
        private_here = isinstance(dest, discord.DMChannel) or (
            getattr(dest, "id", 0) in config.PRIVATE_ROOMS)
        async def on_step(step, shot=None):
            await prog.update(step, shot)
            await narrate_step(dest, step)

        # 做完之後畫面會留著（見 browser._finish），所以錄影不是在任務結束時
        # 拿得到 —— Playwright 要等 context 關掉才把 webm 寫完。真的收掉的時候
        # 再把影片補送過來。
        async def on_closed(_uid: int, video: str | None):
            # 瀏覽器沒了就不該再往語音頻道送它的聲音
            try:
                await set_browser_sound(user_id, False)
            except Exception as e:  # noqa: BLE001
                logger.debug("關瀏覽器聲音失敗（忽略）：%s", e)
            if not video:
                return
            await send_browse_video(
                dest, user_id,
                browser.Result(status="done", video=video,
                               sensitive=result_holder.get("sensitive", False)))

        result = await browser.start(user_id, task, start_url=url or None,
                                     delegate_id=delegate_id,
                                     delegate_name=delegate_name,
                                     private_channel=private_here,
                                     search_hint=search_hint,
                                     on_step=on_step,
                                     on_frame=prog.live,
                                     on_closed=on_closed)
        # 影片是任務結束後才收掉的，那時候要知道畫面上有沒有個資才決定能不能傳
        result_holder["sensitive"] = result.sensitive
    except Exception as e:  # noqa: BLE001
        logger.warning("瀏覽任務失敗：%s", e)
        result = browser.Result(status="error", summary=f"中途出錯了：{str(e)[:150]}")
    await prog.finish()
    # 把「她停下來要問什麼」一起記下來 —— 只記 need_input 這個狀態的話，
    # 事後根本查不出她為什麼會在「看yt」這種任務上要人給資料。
    logger.info("🖥️ 瀏覽任務結束 │ user=%d │ %s%s%s%s", user_id, result.status,
                f" │ 停在 {result.url[:80]}" if result.url else "",
                f" │ 問：{result.question[:120]}" if result.question else "",
                f" │ 錄影 {result.video}" if result.video else "")
    await deliver_browse(dest, user_id, result, task=task, delegate_id=delegate_id)
    await send_browse_video(dest, user_id, result)

    # 把「她剛剛去看了什麼、看到什麼」寫進對話歷史 ——
    # 沒有這個的話使用者追問「那個 XX 有什麼特點」時她會說「我還沒看到」，
    # 明明十二步前才剛捲完整頁。
    try:
        u = bot.get_user(user_id)
        name = u.display_name if u else "他"
        session = conv_manager.get_session(user_id, getattr(dest, "id", 0))
        session.add_message("user", f"[{name}] 說：（請你上網幫我：{task}）")
        session.add_message("assistant", result.summary or result.question
                            or "（我去看過了，把畫面傳給你了）")
    except Exception as e:  # noqa: BLE001
        logger.debug("寫入瀏覽對話歷史失敗（忽略）：%s", e)

    asyncio.create_task(learn_from_browse(user_id, task, result))


async def send_browse_video(dest, user_id: int, result: browser.Result) -> None:
    """把操作錄影傳上去（太大就只說有錄但傳不了）。

    Discord 不讓機器人開螢幕分享，所以「事後可以倒回去看」的錄影是最接近的東西。
    畫面上有個資又在公開頻道時不傳 —— 影片裡什麼都看得到。
    """
    path = result.video
    if not path:
        return
    try:
        if result.sensitive and not (
                isinstance(dest, discord.DMChannel)
                or getattr(dest, "id", 0) in config.PRIVATE_ROOMS):
            logger.info("🎥 錄影含個資且在公開頻道，不傳")
            return
        size_mb = os.path.getsize(path) / 1024 / 1024
        if size_mb > config.BROWSER_MAX_VIDEO_MB:
            await dest.send(f"-# 🎥 這次的操作錄影 {size_mb:.1f} MB，超過上限傳不上來。")
            return
        await dest.send(
            content=f"-# 🎥 剛剛的操作錄影（{size_mb:.1f} MB）—— 可以倒回去看每一步",
            file=discord.File(path, filename="nana-browse.webm"))
    except discord.HTTPException as e:
        logger.warning("傳錄影失敗：%s", e)
    except OSError as e:
        logger.warning("讀錄影檔失敗：%s", e)
    finally:
        browser.cleanup_video(path)


async def learn_from_browse(user_id: int, task: str, result: browser.Result) -> None:
    """把「奈奈幫他上網做了什麼」記進長期記憶。

    只記真的做完的（done）—— 半途卡住的紀錄下來只會變雜訊。
    **個資一定先遮掉**：使用者交代的話裡很常直接帶著身分證和生日
    （「幫我掛號，身分證 A1234…」），那種東西不該寫進資料庫。
    """
    if not (config.MEMORY_ENABLED and result.status == "done"):
        return
    user = bot.get_user(user_id)
    name = user.display_name if user else str(user_id)
    desc = (f"（他請你用瀏覽器幫他辦事：{browser.redact(task)}）"
            f"結果：{browser.redact(result.summary)}")
    # 沿用一般聊天那條記憶抽取的路（含去重、個人/共享分流）
    await learn_from(user_id, name, desc, result.summary)


# 任務進行中被問「進度？」時的關鍵字。只有真的有任務在跑才會比對，
# 所以不會影響平常聊天（也不必為此多打一次模型）。
_PROGRESS_ASKS = (
    "進度", "怎樣了", "怎麼樣了", "好了嗎", "弄好了嗎", "做完了嗎", "完成了嗎",
    "到哪了", "弄到哪", "還要多久", "還沒好", "在做什麼", "在幹嘛", "現在如何",
    "status", "progress",
)


async def send_browse_progress(message: discord.Message, snap: dict) -> None:
    """把目前進度和最後看到的畫面回給使用者。"""
    step_no, mx = snap["step_no"], snap["max_steps"]
    elapsed = snap["elapsed"]
    waiting = snap.get("awaiting")

    if waiting == "confirm":
        head = "我停在**等你確認送出**那一步了，上面那則訊息按一下就會繼續 🌸"
    elif waiting == "input":
        head = "我在**等你給我資料**，你直接打給我就好 🌸"
    else:
        head = f"還在弄，已經做了 **{step_no}** 步（上限 {mx}），花了 {elapsed} 秒。"

    embed = discord.Embed(title="🖥️ 目前進度", description=head, color=0xFFB7C5)
    embed.add_field(name="你交代的事", value=snap["task"][:1000] or "—", inline=False)
    if snap["steps"]:
        lines = [f"{s.n}. {_safe_links(s.detail)}" for s in snap["steps"][-6:]]
        embed.add_field(name="做過的步驟", value="\n".join(lines)[:1000], inline=False)
    if snap["url"]:
        embed.add_field(name="現在這一頁",
                        value=f"{snap['title'][:80]}\n<{snap['url'][:400]}>", inline=False)

    files = []
    if snap["shot"]:
        files.append(discord.File(io.BytesIO(snap["shot"]), filename="nana-progress.png"))
        embed.set_image(url="attachment://nana-progress.png")
        embed.set_footer(text=f"— {config.BOT_NAME}｜這是我最後看到的畫面")
    else:
        embed.set_footer(text=f"— {config.BOT_NAME}")

    try:
        await message.reply(embed=embed, files=files, mention_author=False)
    except discord.HTTPException as e:
        logger.warning("回報進度失敗：%s", e)


async def continue_browse_task(message: discord.Message, answer: str):
    """有人補了資料 → 接續那個停住的瀏覽任務。

    回話的可能是交代的人，也可能是「被代辦的當事人」（他來給自己的身分證），
    所以 session 是用參與者找的，結果也要回報給原本交代的人。
    """
    s = browser.awaiting_input(message.author.id)
    task = s.task if s else ""
    owner_id = s.user_id if s else message.author.id
    delegate_id = s.delegate_id if s else None
    try:
        result = await browser.resume(message.author.id, confirmed=True, extra=answer)
    except Exception as e:  # noqa: BLE001
        logger.warning("接續瀏覽任務失敗：%s", e)
        result = browser.Result(status="error", summary=f"中途出錯了：{str(e)[:150]}")
    await deliver_browse(message.channel, owner_id, result, task=task,
                         delegate_id=delegate_id)


@bot.slash_command(name="browse", description="請奈奈用瀏覽器幫你操作或查詢（例如掛號）🖥️")
@discord.option("task", type=str, description="你要她做什麼，例如「掛台大心臟科下週三下午」")
@discord.option("url", type=str, description="知道網址就給她（選填）", required=False)
@discord.option("for_user", type=discord.Member, required=False,
                description="這是幫誰辦的？需要資料時會直接問他（選填）")
async def slash_browse(
    ctx: discord.ApplicationContext,
    task: str,
    url: str = "",
    for_user: discord.Member = None,
):
    if not config.BROWSER_ENABLED:
        await ctx.respond("瀏覽器操作功能目前是關閉的，請管理員用 `/settings browser` 打開 🌸",
                          ephemeral=True)
        return
    if not browser.AVAILABLE:
        await ctx.respond("這台機器還沒裝好 Playwright，我沒辦法開瀏覽器 😵‍💫", ephemeral=True)
        return

    perms = getattr(ctx.author, "guild_permissions", None)
    if config.BROWSER_ADMIN_ONLY and not (perms and perms.manage_guild):
        await ctx.respond("這個功能目前只開放給管理員使用喔 🌸", ephemeral=True)
        return

    who = f"幫 {for_user.display_name} " if for_user else "幫你"
    await ctx.respond(
        f"好，我去{who}看看 🖥️\n-# 「{task[:120]}」— 操作網站要一點時間，"
        f"做完（或需要問資料）我會把畫面截圖傳上來。"
        + (f"\n-# 需要 {for_user.mention} 的資料時我會直接問他。" if for_user else "")
    )
    # 不要 await 在 interaction 上 —— 瀏覽動輒好幾分鐘，早就逾時了
    asyncio.create_task(run_browse_task(
        ctx.channel, ctx.author.id, task, url,
        delegate_id=(for_user.id if for_user else None),
        delegate_name=(for_user.display_name if for_user else "")))


@bot.slash_command(name="todos", description="查看你的待辦清單 📝")
async def slash_todos(ctx: discord.ApplicationContext):
    if not config.AGENT_ENABLED:
        await ctx.respond("待辦功能目前是關閉的喔。", ephemeral=True)
        return

    items = await reminders.todo_list(ctx.author.id)
    if not items:
        await ctx.respond(
            "你的待辦清單是空的～\n跟我說「**幫我記一下要買牛奶**」就會記下來 📝",
            ephemeral=True,
        )
        return

    lines = "\n".join(f"`#{i}`　{t}" for i, t in items[:40])
    embed = discord.Embed(
        title="📝 你的待辦清單",
        description=f"{lines}\n\n完成的話跟我說「**○○做完了**」就會劃掉。",
        color=0xFFB7C5,
    )
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(name="memories", description=f"看看{config.BOT_NAME}記得你哪些事 🧠")
async def slash_memories(ctx: discord.ApplicationContext):
    if not config.MEMORY_ENABLED:
        await ctx.respond("長期記憶目前是關閉的喔。", ephemeral=True)
        return

    items = await memory.list_memories(ctx.author.id)
    if not items:
        await ctx.respond(
            f"{config.BOT_NAME}還沒記住關於你的事呢～多跟我聊聊吧 💛", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"🧠 {config.BOT_NAME}記得關於你的事",
        description=f"共 {len(items)} 則。想全部刪掉的話用 `/forget`。",
        color=0xFFB7C5,
    )
    grouped: dict[str, list[str]] = {}
    for m in items:
        grouped.setdefault(memory._KIND_LABEL.get(m.kind, "其他"), []).append(m.content)
    for label, contents in grouped.items():
        # Discord 單一欄位上限 1024 字
        text = "\n".join(f"• {c}" for c in contents)
        embed.add_field(name=label, value=text[:1020], inline=False)

    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(name="forget", description=f"讓{config.BOT_NAME}忘掉關於你的所有記憶 🗑️")
async def slash_forget(ctx: discord.ApplicationContext):
    if not config.MEMORY_ENABLED:
        await ctx.respond("長期記憶目前是關閉的喔。", ephemeral=True)
        return

    n = await memory.forget(ctx.author.id)
    conv_manager.clear_all_user_sessions(ctx.author.id)
    logger.info("🧠 清除記憶 │ %s │ %d 則", ctx.author.display_name, n)
    await ctx.respond(
        f"好，{config.BOT_NAME}把關於你的 {n} 則記憶都刪掉了，對話也重新開始 🌱"
        if n else f"{config.BOT_NAME}本來就沒記住關於你的事喔～",
        ephemeral=True,
    )


@bot.slash_command(name="help", description=f"了解{config.BOT_NAME}的所有功能 📖")
async def slash_help(ctx: discord.ApplicationContext):
    await ctx.respond(embed=build_help_embed())


@bot.slash_command(name="support", description="取得心理健康支持資源 📞")
async def slash_support(ctx: discord.ApplicationContext):
    embed = discord.Embed(
        title="📞 心理健康支持資源",
        description="如果你或身邊的人正在經歷困難，以下是可以提供幫助的專業資源：",
        color=0xFF8C00,
    )
    embed.add_field(
        name="🇹🇼 台灣",
        value=(
            "• **安心專線** 1925（24小時免費）\n"
            "• **生命線** 1995\n"
            "• **張老師專線** 1980\n"
            "• **家暴專線** 113\n"
            "• **男性關懷專線** 0800-013-999"
        ),
        inline=False,
    )
    embed.add_field(
        name="🌐 線上資源",
        value=(
            "• [台灣自殺防治安心專線](https://www.tsos.org.tw/)\n"
            "• [心理衛生中心](https://mental-health.gov.tw/)"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 提醒",
        value="尋求幫助是勇敢的表現，不是軟弱。\n記得，你不是一個人 💛",
        inline=False,
    )
    embed.set_footer(text=f"— {config.BOT_NAME} 🌸 | 你值得被好好對待")
    await ctx.respond(embed=embed)


# ── 重點關注用戶與審核控制台 ───────────────────────────


focus_group = bot.create_group("focus", "重點關注用戶訊息審核 🔎")


@focus_group.command(name="channel", description="設定重點關注訊息的審核頻道")
@discord.default_permissions(administrator=True)
@discord.option("channel", type=discord.TextChannel, description="管理員接收審核卡片的頻道")
async def focus_channel(ctx: discord.ApplicationContext, channel: discord.TextChannel):
    config.FOCUS_REVIEW_CHANNEL_ID = channel.id
    config.save_settings()
    await ctx.respond(f"✅ 重點關注審核頻道已設為 {channel.mention}。", ephemeral=True)


@focus_group.command(name="add", description="加入重點關注用戶")
@discord.default_permissions(administrator=True)
@discord.option("member", type=discord.Member, description="所有發言都必須經過審核的成員")
async def focus_add(ctx: discord.ApplicationContext, member: discord.Member):
    if not config.FOCUS_REVIEW_CHANNEL_ID:
        await ctx.respond("請先用 `/focus channel` 設定審核頻道，再加入重點關注用戶。",
                          ephemeral=True)
        return
    if member.bot:
        await ctx.respond("不能把機器人加入重點關注名單。", ephemeral=True)
        return
    if member.id not in config.FOCUS_WATCHED_USERS:
        config.FOCUS_WATCHED_USERS.append(member.id)
        config.save_settings()
    await ctx.respond(
        f"🔎 已將 {member.mention} 加入重點關注名單；之後的發言都會先進入審核。",
        ephemeral=True,
    )


@focus_group.command(name="remove", description="移除重點關注用戶")
@discord.default_permissions(administrator=True)
@discord.option("member", type=discord.Member, description="要解除重點關注的成員")
async def focus_remove(ctx: discord.ApplicationContext, member: discord.Member):
    if member.id in config.FOCUS_WATCHED_USERS:
        config.FOCUS_WATCHED_USERS.remove(member.id)
        config.save_settings()
        text = f"✅ 已將 {member.mention} 移出重點關注名單。"
    else:
        text = f"{member.mention} 不在重點關注名單中。"
    await ctx.respond(text, ephemeral=True)


@focus_group.command(name="mode", description="切換 AI 或人工訊息審核")
@discord.default_permissions(administrator=True)
@discord.option("mode", type=str, description="選擇審核方式", choices=["人工審核", "AI 審核"])
async def focus_mode(ctx: discord.ApplicationContext, mode: str):
    config.FOCUS_REVIEW_MODE = "ai" if mode == "AI 審核" else "manual"
    config.save_settings()
    note = "（含附件的訊息仍會轉人工審核）" if config.FOCUS_REVIEW_MODE == "ai" else ""
    await ctx.respond(f"✅ 已切換為 **{mode}** {note}", ephemeral=True)


@focus_group.command(name="list", description="查看重點關注用戶名單")
@discord.default_permissions(administrator=True)
async def focus_list(ctx: discord.ApplicationContext):
    users = config.FOCUS_WATCHED_USERS
    text = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in users) or "目前沒有重點關注用戶。"
    await ctx.respond(embed=discord.Embed(title="🔎 重點關注名單", description=text,
                                          color=0x5865F2), ephemeral=True)


@focus_group.command(name="pending", description="查看待人工審核訊息")
@discord.default_permissions(administrator=True)
async def focus_pending(ctx: discord.ApplicationContext):
    items = focus_review.pending_summary()
    text = "\n".join(
        f"• `#{item.id}` <@{item.author_id}> → <#{item.channel_id}>" for item in items
    ) or "目前沒有待審訊息。"
    await ctx.respond(embed=discord.Embed(title="📨 待審佇列", description=text,
                                          color=0xF0A500), ephemeral=True)


@focus_group.command(name="panel", description="開啟重點關注管理控制台")
@discord.default_permissions(administrator=True)
async def focus_panel(ctx: discord.ApplicationContext):
    await ctx.respond(embed=focus_review.control_embed(),
                      view=focus_review.FocusControlView(), ephemeral=True)


# ── 設定指令 ───────────────────────────────────────────


settings_group = bot.create_group("settings", "奈奈機器人設定 ⚙️")


@settings_group.command(name="alert_channel", description="設定危險訊息警告頻道 🚨")
@discord.default_permissions(administrator=True)
@discord.option("channel", type=discord.TextChannel, description="選擇要接收危險訊息警告的頻道")
async def settings_alert_channel(
    ctx: discord.ApplicationContext,
    channel: discord.TextChannel,
):
    """設定危險訊息警告頻道（僅管理員）"""
    config.ALERT_CHANNEL_ID = channel.id
    config.save_settings()
    logger.info("⚙️ 警告頻道已更新: #%s (%d) │ 設定者: %s", channel.name, channel.id, ctx.author.display_name)

    embed = discord.Embed(
        title="⚙️ 設定已更新",
        description=f"危險訊息警告頻道已設定為 {channel.mention}",
        color=0x00CC66,
    )
    embed.add_field(
        name="📋 效果",
        value="當偵測到包含自傷或危險訊號的訊息時，\n警告將自動發送到此頻道。",
        inline=False,
    )
    embed.set_footer(text=f"設定者: {ctx.author.display_name}")
    await ctx.respond(embed=embed, ephemeral=True)


@settings_group.command(name="disable_alert", description="停用危險訊息警告 ⚠️")
@discord.default_permissions(administrator=True)
async def settings_disable_alert(ctx: discord.ApplicationContext):
    """停用危險訊息警告（僅管理員）"""
    config.ALERT_CHANNEL_ID = None
    config.save_settings()
    logger.info("⚙️ 警告頻道已停用 │ 設定者: %s", ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 警告已停用",
            description="危險訊息警告功能已停用。\n使用 `/settings alert_channel` 重新啟用。",
            color=0xFF8C00,
        ),
        ephemeral=True,
    )


@settings_group.command(name="view", description="查看目前的設定 📋")
@discord.default_permissions(administrator=True)
async def settings_view(ctx: discord.ApplicationContext):
    """查看目前的機器人設定（僅管理員）"""
    alert_ch = bot.get_channel(config.ALERT_CHANNEL_ID) if config.ALERT_CHANNEL_ID else None
    alert_text = alert_ch.mention if alert_ch else "❌ 未設定"

    embed = discord.Embed(
        title=f"⚙️ {config.BOT_NAME} 目前設定",
        color=0xFFB7C5,
    )
    embed.add_field(name="🚨 警告頻道", value=alert_text, inline=True)
    embed.add_field(name="⏱️ 冷卻時間", value=f"{config.COOLDOWN_SECONDS} 秒", inline=True)
    embed.add_field(name="🔑 觸發關鍵字", value=", ".join(config.TRIGGER_KEYWORDS), inline=True)
    embed.add_field(name="🤖 文字模型", value=f"`{config.LM_STUDIO_MODEL}`", inline=True)
    embed.add_field(name="🎙️ 語音 AI", value=f"`{config.VOICE_SERVER_URL}`", inline=True)
    embed.add_field(name="🎚️ 情緒門檻", value=f"強度 >= {getattr(config, 'EMOTION_THRESHOLD', 2)}", inline=True)

    # 審核設定
    review_ch = bot.get_channel(config.REVIEW_CHANNEL_ID) if config.REVIEW_CHANNEL_ID else None
    review_ch_text = review_ch.mention if review_ch else "❌ 未設定"
    remove_role = ctx.guild.get_role(config.REVIEW_REMOVE_ROLE_ID) if config.REVIEW_REMOVE_ROLE_ID and ctx.guild else None
    add_role = ctx.guild.get_role(config.REVIEW_ADD_ROLE_ID) if config.REVIEW_ADD_ROLE_ID and ctx.guild else None

    embed.add_field(
        name="📋 審核系統",
        value=(
            f"• 審核頻道: {review_ch_text}\n"
            f"• 移除角色: {remove_role.mention if remove_role else '❌ 未設定'}\n"
            f"• 加上角色: {add_role.mention if add_role else '❌ 未設定'}"
        ),
        inline=False,
    )

    focus_ch = bot.get_channel(config.FOCUS_REVIEW_CHANNEL_ID) if config.FOCUS_REVIEW_CHANNEL_ID else None
    embed.add_field(
        name="🔎 重點關注審核",
        value=(
            f"• 審核頻道: {focus_ch.mention if focus_ch else '❌ 未設定'}\n"
            f"• 審核模式: {'🤖 AI' if config.FOCUS_REVIEW_MODE == 'ai' else '👤 人工'}\n"
            f"• 關注人數: {len(config.FOCUS_WATCHED_USERS)}\n"
            f"• 待審訊息: {focus_review.pending_count()}"
        ),
        inline=False,
    )
    
    # 聊天與回覆設定
    fixed_ch = bot.get_channel(config.FIXED_REPLY_CHANNEL_ID) if config.FIXED_REPLY_CHANNEL_ID else None
    embed.add_field(
        name="💬 聊天與偏好",
        value=(
            f"• 專屬回覆頻道: {fixed_ch.mention if fixed_ch else '❌ 未設定'}\n"
            f"• @提及時回覆: {'✅ 啟用' if getattr(config, 'REPLY_ON_MENTION', True) else '❌ 停用'}\n"
            f"• 關鍵字時回覆: {'✅ 啟用' if getattr(config, 'REPLY_ON_KEYWORD', True) else '❌ 停用'}\n"
            f"• 自動情緒安慰: {'✅ 啟用' if getattr(config, 'EMOTION_AUTO_REPLY', True) else '❌ 僅警告'}\n"
            f"• 主動按表情: "
            f"{'✅ 啟用（AI 判斷該不該按）' if getattr(config, 'REACTION_ENABLED', True) else '❌ 停用'}"
            + (
                f"，節流 {int(getattr(config, 'REACTION_PROBABILITY', 1.0) * 100)}%"
                if getattr(config, "REACTION_PROBABILITY", 1.0) < 1.0 else ""
            )
            + "\n"
            f"• 跟著別人按: "
            f"{'✅ 啟用' if getattr(config, 'REACTION_FOLLOW_ENABLED', True) else '❌ 停用'}\n"
            f"• 看得到被回覆的訊息: "
            f"{'✅ 啟用' if getattr(config, 'REPLY_CONTEXT_ENABLED', True) else '❌ 停用'}"
        ),
        inline=False,
    )

    if config.MONITORED_CHANNELS:
        channels = [f"<#{c}>" for c in config.MONITORED_CHANNELS]
        embed.add_field(name="📡 監聽頻道", value=", ".join(channels), inline=False)
    else:
        embed.add_field(name="📡 監聽頻道", value="全部頻道", inline=True)

    await ctx.respond(embed=embed, ephemeral=True)


# ── 審核設定 ───────────────────────────────────────────


@settings_group.command(name="review_channel", description="設定新成員審核頻道 📋")
@discord.default_permissions(administrator=True)
@discord.option("channel", type=discord.TextChannel, description="選擇審核表單的頻道")
async def settings_review_channel(
    ctx: discord.ApplicationContext,
    channel: discord.TextChannel,
):
    """設定新成員審核頻道（僅管理員）"""
    config.REVIEW_CHANNEL_ID = channel.id
    config.save_settings()
    logger.info("⚙️ 審核頻道已更新: #%s (%d) │ 設定者: %s", channel.name, channel.id, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 審核頻道已設定",
            description=f"新成員表單將在 {channel.mention} 自動審核",
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="review_remove_role", description="設定審核通過後要移除的角色 🏷️")
@discord.default_permissions(administrator=True)
@discord.option("role", type=discord.Role, description="審核通過後要移除的角色")
async def settings_review_remove_role(
    ctx: discord.ApplicationContext,
    role: discord.Role,
):
    """設定審核通過後要移除的角色（僅管理員）"""
    config.REVIEW_REMOVE_ROLE_ID = role.id
    config.save_settings()
    logger.info("⚙️ 審核移除角色: %s (%d) │ 設定者: %s", role.name, role.id, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 移除角色已設定",
            description=f"審核通過後將移除 {role.mention}",
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="review_add_role", description="設定審核通過後要加上的角色 🏷️")
@discord.default_permissions(administrator=True)
@discord.option("role", type=discord.Role, description="審核通過後要加上的角色")
async def settings_review_add_role(
    ctx: discord.ApplicationContext,
    role: discord.Role,
):
    """設定審核通過後要加上的角色（僅管理員）"""
    config.REVIEW_ADD_ROLE_ID = role.id
    config.save_settings()
    logger.info("⚙️ 審核加上角色: %s (%d) │ 設定者: %s", role.name, role.id, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 加上角色已設定",
            description=f"審核通過後將加上 {role.mention}",
            color=0x00CC66,
        ),
        ephemeral=True,
    )


# ── 進階對話與情緒設定 ────────────────────────────────────


@settings_group.command(name="fixed_channel", description="設定專屬對話頻道（任何發言強制作答）💬")
@discord.default_permissions(administrator=True)
@discord.option("channel", type=discord.TextChannel, description="選擇要作為專屬聊天的頻道（留空則移除）", required=False)
async def settings_fixed_channel(
    ctx: discord.ApplicationContext,
    channel: discord.TextChannel = None,
):
    """設定專屬對話頻道（僅管理員）"""
    config.FIXED_REPLY_CHANNEL_ID = channel.id if channel else None
    config.save_settings()
    logger.info("⚙️ 專屬回覆頻道已更新: %s │ 設定者: %s", channel.name if channel else "無", ctx.author.display_name)

    desc = f"未來在 {channel.mention} 裡的任何發言，都會直接觸發奈奈回覆！" if channel else "已取消專屬對話頻道設定。"
    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 專屬對話頻道設定",
            description=desc,
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="reply_on_mention", description="設定是否在被提及(@)時自動回覆 🔔")
@discord.default_permissions(administrator=True)
@discord.option("enable", type=bool, description="True (是) 或 False (否)")
async def settings_reply_on_mention(
    ctx: discord.ApplicationContext,
    enable: bool,
):
    """設定是否在被提及時回覆（僅管理員）"""
    config.REPLY_ON_MENTION = enable
    config.save_settings()
    logger.info("⚙️ @提及回覆功能: %s │ 設定者: %s", enable, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 提及回覆設定",
            description=f"已{'開啟' if enable else '關閉'}被 @ 提及時的回覆功能。",
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="reply_on_keyword", description="設定是否在提到關鍵字(例如:奈奈)時自動回覆 🔔")
@discord.default_permissions(administrator=True)
@discord.option("enable", type=bool, description="True (是) 或 False (否)")
async def settings_reply_on_keyword(
    ctx: discord.ApplicationContext,
    enable: bool,
):
    """設定是否在被提及關鍵字時回覆（僅管理員）"""
    config.REPLY_ON_KEYWORD = enable
    config.save_settings()
    logger.info("⚙️ 關鍵字回覆功能: %s │ 設定者: %s", enable, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 關鍵字回覆設定",
            description=f"已{'開啟' if enable else '關閉'}文字內含關鍵字（{', '.join(config.TRIGGER_KEYWORDS)}）時的自動回覆功能。",
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="emotion_support", description="設定是否開啟自動情緒安慰功能 💛")
@discord.default_permissions(administrator=True)
@discord.option("enable", type=bool, description="True (開啟主動安慰) 或 False (只偵測危險不主動閒聊)")
async def settings_emotion_support(
    ctx: discord.ApplicationContext,
    enable: bool,
):
    """設定主動情緒安慰功能（僅管理員）"""
    config.EMOTION_AUTO_REPLY = enable
    config.save_settings()
    logger.info("⚙️ 情緒主動安慰: %s │ 設定者: %s", enable, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 自動情緒安慰設定",
            description=f"已{'開啟' if enable else '關閉'}自動情緒安慰功能。\n（注意：即使關閉，嚴重危險訊息仍會發送至警告頻道）",
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="emotion_threshold", description="設定自動安慰的情緒強度門檻 🎚️")
@discord.default_permissions(administrator=True)
@discord.option("threshold", type=int, choices=[1, 2, 3, 4, 5], description="情緒觸發門檻 (1最敏感~5最嚴重)")
async def settings_emotion_threshold(
    ctx: discord.ApplicationContext,
    threshold: int,
):
    """設定情緒偵測觸發門檻（僅管理員）"""
    config.EMOTION_THRESHOLD = threshold
    config.save_settings()
    logger.info("⚙️ 情緒觸發門檻: %d │ 設定者: %s", threshold, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 情緒觸發門檻設定",
            description=f"已將自動安慰的情緒強度門檻調整為 **{threshold}**\n\n*(1: 微小情緒即可觸發, 5: 僅極度崩潰才觸發)*",
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="reactions", description="設定奈奈要不要自己按表情回應訊息 🌸")
@discord.default_permissions(administrator=True)
@discord.option("enable", type=bool, description="True (幫訊息按表情) 或 False (完全不按)")
async def settings_reactions(
    ctx: discord.ApplicationContext,
    enable: bool,
):
    """設定自動表情回應（僅管理員）"""
    config.REACTION_ENABLED = enable
    config.save_settings()
    logger.info("⚙️ 自動表情回應: %s │ 設定者: %s", enable, ctx.author.display_name)

    passive = int(config.REACTION_PROBABILITY * 100)
    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 自動表情回應設定",
            description=(
                (
                    "已**開啟**自動表情回應。\n\n"
                    "不管有沒有在跟奈奈講話，她都會看過訊息內容再決定要不要按表情：\n"
                    "• **不是每則都按** —— 由 AI 判斷值不值得回應，平淡的日常對話不會按\n"
                    "• 明確的自傷／危險訊息不會按表情（改用文字好好回應）\n"
                    "• 吵架、貼連結、指令、事務性訊息也不會按\n"
                    f"• 同一個人 {config.REACTION_BURST_WINDOW_S} 秒內不會被連按\n"
                    f"• 同一頻道 {config.REACTION_CHANNEL_WINDOW_S} 秒內最多回應 "
                    f"{config.REACTION_CHANNEL_MAX_IN_WINDOW} 則，最近的同類表情也會避開\n"
                    + (
                        f"• 目前另外設了 {passive}% 節流（`/settings reaction_rate` 可調）\n"
                        if passive < 100 else ""
                    )
                )
                if enable else
                "已**關閉**自動表情回應，奈奈不會再幫訊息按表情。"
            ),
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="browser", description="開關瀏覽器操作功能 🖥️")
@discord.default_permissions(administrator=True)
@discord.option("enable", type=bool, description="True (可以幫人操作網站) 或 False (完全關閉)")
@discord.option("admin_only", type=bool,
                description="只有管理員能用（選填，預設維持現狀）", required=False)
async def settings_browser(
    ctx: discord.ApplicationContext,
    enable: bool,
    admin_only: bool = None,
):
    """開關瀏覽器操作（僅管理員）"""
    config.BROWSER_ENABLED = enable
    if admin_only is not None:
        config.BROWSER_ADMIN_ONLY = admin_only
    config.save_settings()
    logger.info("⚙️ 瀏覽器操作: %s（僅管理員=%s）│ 設定者: %s",
                enable, config.BROWSER_ADMIN_ONLY, ctx.author.display_name)

    if not enable:
        await browser.shutdown()

    allow = config.BROWSER_ALLOW_DOMAINS
    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 瀏覽器操作設定",
            description=(
                (
                    "已**開啟**。奈奈可以用 `/browse` 或直接被交代（「幫我掛…」）"
                    "去操作網站，做完傳截圖回來。\n\n"
                    f"• 使用權限：{'🔒 只有管理員' if config.BROWSER_ADMIN_ONLY else '👥 所有人'}\n"
                    f"• 可去的網站：{'、'.join(allow) if allow else '除內網以外都可以'}\n"
                    f"• 一個任務上限 {config.BROWSER_MAX_STEPS} 步 / "
                    f"{config.BROWSER_MAX_SECONDS} 秒，同時最多 "
                    f"{config.BROWSER_MAX_SESSIONS} 個人在用\n"
                    "• **送出／付款／掛號這類按鈕一定先問本人**才會按\n"
                    "• 身分證、生日這種資料只會用本人給的，不會自己編\n"
                    "• 內網網址一律不去；遇到驗證碼或要登入會停下來交還給本人"
                )
                if enable else
                "已**關閉**瀏覽器操作，並收掉所有還開著的瀏覽器。"
            ),
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="reaction_follow", description="設定奈奈要不要跟著別人按表情 🫱")
@discord.default_permissions(administrator=True)
@discord.option("enable", type=bool, description="True (別人按了就跟著按同一個) 或 False (不跟)")
async def settings_reaction_follow(
    ctx: discord.ApplicationContext,
    enable: bool,
):
    """設定要不要跟著別人按表情（僅管理員）"""
    config.REACTION_FOLLOW_ENABLED = enable
    config.save_settings()
    logger.info("⚙️ 跟著按表情: %s │ 設定者: %s", enable, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 跟著按表情設定",
            description=(
                (
                    "已**開啟**附和。別人在某則訊息上按了表情，奈奈會跟著按同一個：\n"
                    f"• 要 **{config.REACTION_FOLLOW_MIN_COUNT}** 個人按了才跟\n"
                    f"• 同一則訊息最多跟 **{config.REACTION_FOLLOW_MAX_PER_MESSAGE}** 種表情\n"
                    f"• 跟之前會等 {config.REACTION_FOLLOW_DELAY:g} 秒，比較自然\n"
                    "• 提到自傷／想死的訊息不會跟（那種要用文字好好回應）\n"
                    "• 這條不花模型算力，和「主動判斷要不要按」是各自獨立的功能"
                )
                if enable else
                "已**關閉**附和，別人按表情奈奈不會再跟著按。\n"
                "（主動判斷要不要按表情的功能不受影響，用 `/settings reactions` 調）"
            ),
            color=0x00CC66,
        ),
        ephemeral=True,
    )


@settings_group.command(name="reaction_rate", description="限制多少比例的訊息會送去讓 AI 判斷 🎚️")
@discord.default_permissions(administrator=True)
@discord.option("percent", type=int, description="0~100，預設 100（每則都讓 AI 判斷）。調低=先隨機跳過一些，省算力")
async def settings_reaction_rate(
    ctx: discord.ApplicationContext,
    percent: int,
):
    """設定多少比例的訊息會送去讓模型判斷要不要按表情（僅管理員）"""
    percent = max(0, min(100, percent))
    config.REACTION_PROBABILITY = percent / 100
    config.save_settings()
    logger.info("⚙️ 表情判斷節流: %d%% │ 設定者: %s", percent, ctx.author.display_name)

    await ctx.respond(
        embed=discord.Embed(
            title="⚙️ 表情回應節流設定",
            description=(
                f"一般訊息有 **{percent}%** 會送去讓 AI 判斷要不要按表情。\n\n"
                + (
                    "*100% = 每則都判斷。要不要按仍然是 AI 決定的，"
                    "它覺得不值得就不會按 —— 這個數字只是節流閥，"
                    "算力吃緊的大群才需要調低。*"
                    if percent == 100 else
                    f"*剩下的 {100 - percent}% 會直接跳過（連判斷都不做）。*"
                )
            ),
            color=0x00CC66,
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════
# 手動審核指令
# ═══════════════════════════════════════════════════════


@bot.slash_command(name="approve", description="手動通過新成員審核 ✅")
@discord.default_permissions(administrator=True)
@discord.option("member", type=discord.Member, description="要通過審核的成員")
@discord.option("reason", type=str, description="通過原因（選填）", required=False)
async def slash_approve(
    ctx: discord.ApplicationContext,
    member: discord.Member,
    reason: str = "管理員手動審核通過",
):
    """手動通過新成員審核（僅管理員）"""
    await ctx.defer(ephemeral=True)

    # 移除追蹤記錄
    review_manager.remove_pending(member.id)

    # 執行角色變更和通過訊息
    await approve_member(member, ctx.channel, reason)

    await ctx.followup.send(
        embed=discord.Embed(
            title="✅ 手動審核完成",
            description=f"已通過 {member.mention} 的審核\n原因: {reason}",
            color=0x00CC66,
        ),
        ephemeral=True,
    )
    logger.info("✅ 手動審核通過 │ %s │ 設定者: %s │ 原因: %s", member.display_name, ctx.author.display_name, reason)


# ═══════════════════════════════════════════════════════


async def _flash_speaking(vc, times: int = 2) -> None:
    """加入語音後閃 speaking 燈幾下，當「已就緒」提示。"""
    try:
        from discord.enums import SpeakingState
        for _ in range(times):
            await vc.ws.speak(SpeakingState.voice)
            await asyncio.sleep(0.15)
            await vc.ws.speak(SpeakingState.none)
            await asyncio.sleep(0.13)
    except Exception as e:  # noqa: BLE001
        logger.debug("閃燈失敗（忽略）：%s", e)


def _vc_for_user(user_id: int):
    """找出「這個人在、奈奈也在」的那個語音頻道。"""
    for vc in bot.voice_clients:
        ch = getattr(vc, "channel", None)
        if ch and any(m.id == user_id for m in getattr(ch, "members", [])):
            return vc
    return None


def browser_sound_on(vc) -> bool:
    return isinstance(getattr(vc, "source", None), MixedAudioSource)


async def set_browser_sound(user_id: int, on: bool) -> str:
    """把瀏覽器的聲音接進語音頻道／收掉。回傳給人看的一句話。"""
    vc = _vc_for_user(user_id)
    if vc is None:
        return "你要先跟我在同一個語音頻道裡（用 `/join` 叫我進去）才聽得到 🎙️"

    if not on:
        if browser_sound_on(vc):
            vc.stop()          # 停掉混音；她要說話時 play monitor 會自己重新 play
        return "瀏覽器的聲音關掉了"

    if browser_sound_on(vc):
        return "已經在放了 🔊"
    if not await audio_sink.ensure():
        return "這台機器的音效環境起不來，沒辦法給聲音（要 pipewire）"

    try:
        # 從假裝置的 monitor 錄 —— 沒有聲音在播的時候它就是一片靜音，不會出錯
        src = discord.FFmpegPCMAudio(
            audio_sink.monitor_source(),
            before_options="-f pulse -fflags nobuffer -flags low_delay",
            options=None)
    except Exception as e:  # noqa: BLE001
        logger.warning("🔊 開不了 ffmpeg：%s", e)
        return f"開不了音訊來源：{str(e)[:100]}"

    vc.stop()
    vc.play(MixedAudioSource(src, AIAudioSource(bridge)))
    logger.info("🔊 瀏覽器聲音已接進語音頻道 │ user=%d │ 頻道=%s",
                user_id, getattr(vc.channel, "name", "?"))
    return "接上了 —— 現在語音頻道聽得到瀏覽器的聲音 🔊"


async def _remember_proactive(sent, user_id: int, trigger: str,
                              msg: str, target) -> None:
    """主動關心送出去之後的收尾：記住來源，並寫進對話歷史。

    兩件事都是為了「對方回過來的時候她接得上」：
      • proactive_sent：對方**回覆**那則訊息時，把當初的來源接回去
        （session 可能早就過期了，所以這筆一定要落地）
      • 對話歷史：對方在同一個頻道直接接話時，至少知道自己剛講過什麼
    兩個都失敗也不能讓這則關心變成錯誤 —— 訊息已經送出去了。
    """
    try:
        if sent is not None and trigger:
            await reminders.remember_proactive(sent.id, user_id, trigger)
    except Exception as e:  # noqa: BLE001
        logger.debug("記主動關心來源失敗（忽略）：%s", e)
    try:
        session = conv_manager.get_session(user_id, getattr(target, "id", 0))
        session.add_message("assistant", msg)
    except Exception as e:  # noqa: BLE001
        logger.debug("主動關心寫入對話歷史失敗（忽略）：%s", e)


async def _ai_play_monitor(vc) -> None:
    """按需播放：只有奈奈真的有話要說時才 play，播完 source 回 b"" 自動停止。

    這樣 Discord 的 speaking 指示燈就只在奈奈說話時亮，不會恆亮
    （原本是無條件持續 play 靜音 → 燈一直亮）。
    """
    while vc and vc.is_connected():
        try:
            if bridge.ai_audio_queue and not vc.is_playing():
                vc.play(AIAudioSource(bridge))
        except Exception as e:  # noqa: BLE001
            logger.debug("play monitor（忽略）：%s", e)
        await asyncio.sleep(0.1)


@bot.slash_command(name="join", description="加入你的語音頻道 🎙️")
async def slash_join(ctx: discord.ApplicationContext):
    """加入語音頻道並啟動語音 AI"""
    # 私訊裡 ctx.author 是 User 而不是 Member，而 User 沒有 .voice ——
    # 直接讀會 AttributeError，指令整個爛掉（使用者只看到「應用程式沒有回應」）。
    # 語音狀態是「伺服器裡」的概念，所以私訊要講清楚，不是丟一句請先加入語音頻道。
    if ctx.guild is None:
        await ctx.respond("❌ 語音頻道只有在伺服器裡才有喔，到伺服器再叫我 🎙️",
                          ephemeral=True)
        return
    voice = getattr(ctx.author, "voice", None)
    if not voice or not voice.channel:
        await ctx.respond("❌ 請先加入語音頻道！", ephemeral=True)
        return

    channel = voice.channel
    await ctx.defer()

    # 連接語音頻道（不自動重連，避免重啟後無限重試）
    try:
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect(timeout=15.0, reconnect=False)
    except Exception as e:
        logger.error("語音連接失敗: %s", e)
        await ctx.followup.send(f"⚠️ 語音連接失敗: {e}\n請稍後再試！")
        return

    # 確認連接
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.followup.send("⚠️ 語音連接失敗，請重試！")
        return

    # 連接語音伺服器
    if not bridge.is_connected:
        threading.Thread(target=bridge.connect, daemon=True).start()
        for _ in range(20):
            if bridge.is_connected:
                break
            await asyncio.sleep(0.5)

    if bridge.is_connected:
        bridge.start_polling()
        status_msg = f"✅ {config.BOT_NAME}已加入語音頻道！直接說話就可以和我聊天 🎤\n用 `/leave` 讓我離開"
    else:
        status_msg = f"⚠️ 語音 AI 連線失敗，{config.BOT_NAME}只能聽但不能說話"

    await ctx.followup.send(status_msg)

    # 加入後燈閃兩下（就緒提示），再啟動「僅奈奈說話時亮」的按需播放
    if ctx.voice_client:
        await _flash_speaking(ctx.voice_client, times=2)
        bot.loop.create_task(_ai_play_monitor(ctx.voice_client))

    # 開始接收使用者語音
    try:
        sink = OmniSink(bridge)
        sink.init(ctx.voice_client)
        ctx.voice_client.start_recording(sink, recording_done, ctx.channel)
        logger.info("🎤 語音錄音已啟動")
    except Exception as e:
        logger.error("語音接收啟動失敗: %s", e)
        await ctx.followup.send(f"⚠️ 語音接收啟動失敗: {e}")


@bot.slash_command(name="sound",
                   description="把奈奈瀏覽器的聲音接進語音頻道 🔊（例如聽 YouTube）")
@discord.option("開關", description="要開還是要關", choices=["開", "關"], default="開")
async def slash_sound(ctx: discord.ApplicationContext, 開關: str = "開"):
    """瀏覽器聲音的開關。

    面板上也有同一顆鈕，但不是每個人都會開著操作台 ——
    在頻道打一行字比較快。
    """
    await ctx.defer(ephemeral=True)
    msg = await set_browser_sound(ctx.author.id, 開關 == "開")
    await ctx.followup.send(msg, ephemeral=True)


@bot.slash_command(name="leave", description="離開語音頻道 👋")
async def slash_leave(ctx: discord.ApplicationContext):
    """離開語音頻道"""
    if not ctx.voice_client:
        await ctx.respond("❌ 我不在語音頻道中", ephemeral=True)
        return

    try:
        ctx.voice_client.stop_recording()
    except Exception:
        pass

    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()

    bridge.disconnect()
    await ctx.voice_client.disconnect()
    await ctx.respond(f"👋 {config.BOT_NAME}離開了～下次再聊！")


@bot.slash_command(name="status", description="查看機器人狀態 📊")
async def slash_status(ctx: discord.ApplicationContext):
    """顯示機器人狀態"""
    voice_status = "✅ 已連接" if bridge.is_connected else "❌ 未連接"
    vc = ctx.voice_client
    vc_status = "✅ 在語音頻道中" if vc and vc.is_connected() else "❌ 不在語音頻道"

    embed = discord.Embed(
        title=f"📊 {config.BOT_NAME} 狀態",
        color=0xFFB7C5,
    )
    embed.add_field(name="🤖 文字 AI", value=f"模型: `{config.LM_STUDIO_MODEL}`", inline=True)
    embed.add_field(name="🎙️ 語音 AI", value=voice_status, inline=True)
    embed.add_field(name="🔊 語音頻道", value=vc_status, inline=True)

    if bridge.is_connected:
        embed.add_field(
            name="📈 語音統計",
            value=(
                f"AI 音訊: {bridge.audio_received_count} 段\n"
                f"AI 佇列: {len(bridge.ai_audio_queue)} 段\n"
                f"使用者佇列: {len(bridge.user_audio_queue)} 段"
            ),
            inline=False,
        )

    await ctx.respond(embed=embed)


# ═══════════════════════════════════════════════════════
# 前綴指令
# ═══════════════════════════════════════════════════════


@bot.command(name="nana", aliases=["奈奈"])
async def cmd_nana(ctx: commands.Context, *, text: str = ""):
    if not text:
        await ctx.reply(
            f"嗨～我是{config.BOT_NAME}！💛\n"
            f"你可以直接 @我 或在訊息中提到「奈奈」和我聊天喔～\n"
            f"輸入 `/help` 查看更多功能 ✨",
            mention_author=False,
        )
        return
    message = ctx.message
    message.content = text
    await handle_direct_conversation(message)


@bot.command(name="reset", aliases=["重置"])
async def cmd_reset(ctx: commands.Context):
    conv_manager.clear_session(ctx.author.id, ctx.channel.id)
    await ctx.reply(
        f"好的，{config.BOT_NAME}把之前的對話忘掉了～🌱\n讓我們重新開始吧！",
        mention_author=False,
    )


@bot.command(name="help", aliases=["幫助"])
async def cmd_help(ctx: commands.Context):
    await ctx.reply(embed=build_help_embed(), mention_author=False)


@bot.command(name="mood", aliases=["心情"])
async def cmd_mood(ctx: commands.Context):
    import random

    tips = [
        "🌿 試著深呼吸 3 次，慢慢吸氣、慢慢吐氣",
        "☕ 給自己泡一杯溫暖的飲料吧",
        "🎵 聽一首你喜歡的歌",
        "🚶 起身走動一下，看看窗外的風景",
        "📝 把現在的心情寫下來，不管寫什麼都好",
        "💤 如果累了，允許自己休息一下",
        "🌸 跟一個你信任的人聊聊天",
        "🎨 做一件讓你感到快樂的小事",
    ]

    embed = discord.Embed(title="🌈 心情小提醒", description=random.choice(tips), color=0x87CEEB)
    embed.set_footer(text=f"— {config.BOT_NAME} 💛")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="support", aliases=["支援"])
async def cmd_support(ctx: commands.Context):
    embed = discord.Embed(
        title="📞 心理健康支持資源",
        description="如果你或身邊的人正在經歷困難，以下是可以提供幫助的專業資源：",
        color=0xFF8C00,
    )
    embed.add_field(
        name="🇹🇼 台灣",
        value=(
            "• **安心專線** 1925（24小時免費）\n"
            "• **生命線** 1995\n"
            "• **張老師專線** 1980\n"
            "• **家暴專線** 113\n"
            "• **男性關懷專線** 0800-013-999"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 提醒",
        value="尋求幫助是勇敢的表現，不是軟弱。\n記得，你不是一個人 💛",
        inline=False,
    )
    embed.set_footer(text=f"— {config.BOT_NAME} 🌸")
    await ctx.reply(embed=embed, mention_author=False)


# ═══════════════════════════════════════════════════════
# 工具函式
# ═══════════════════════════════════════════════════════


def build_help_embed() -> discord.Embed:
    """建立通用的幫助 Embed"""
    embed = discord.Embed(
        title=f"🌸 {config.BOT_NAME} — 你的情緒支持夥伴",
        description=(
            f"{config.BOT_NAME}會在這裡陪伴你，傾聽你的心聲。\n"
            f"無論是開心或難過，都可以和我聊聊～"
        ),
        color=0xFFB7C5,
    )

    embed.add_field(
        name="💬 和我聊天（文字）",
        value=(
            f"• **@{config.BOT_NAME}** + 你想說的話\n"
            f"• 訊息中提到「**奈奈**」\n"
            f"• **回覆**我的訊息繼續對話\n"
            f"• **私訊**我也可以喔\n"
            f"• 用 Discord 的**回覆**指著某則訊息問我，我看得到那則訊息的內容 ↩️\n"
            f"• 沒找我聊的時候，我也可能順手幫訊息按個表情 🌸\n"
            f"• 看到別人按了表情，我也會跟著按一個 🫱"
        ),
        inline=False,
    )

    embed.add_field(
        name="📎 傳檔案 / 圖片給我",
        value=(
            "搭話時一起附上就好，我會看內容再回你\n"
            f"• 🖼️ 圖片（png / jpg / gif / bmp）最多 {config.MAX_IMAGES_PER_MESSAGE} 張\n"
            f"• 📄 文字檔（txt / md / json / 程式碼…）最多 {config.MAX_TEXT_FILES_PER_MESSAGE} 個\n"
            "• 沒搭話的話我不會偷看頻道裡的檔案喔"
        ),
        inline=False,
    )

    embed.add_field(
        name="⏰ 幫你記著事情",
        value=(
            "**有時間的 → 提醒**\n"
            "• 「**30分鐘後**提醒我倒垃圾」\n"
            "• 「**每天晚上10點**提醒我吃藥」（平日／每週也可以）\n"
            "**沒時間的 → 待辦**\n"
            "• 「幫我**記一下**要買牛奶」／「買牛奶**做完了**」\n"
            "**要我主動找你**\n"
            "• 「每天晚上9點**來關心我**一下」"
        ),
        inline=False,
    )

    embed.add_field(
        name="🌤️ 其他小幫手",
        value=(
            "• 「**台北明天會下雨嗎**」— 查天氣\n"
            "• 「幫我**算** 1234 乘以 56」— 精確計算"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔎 幫你上網查",
        value=(
            "• 直接說「**查一下** ○○」「**搜尋** ○○」我就去查\n"
            "• 問到時事、天氣、股價這類即時的事，我也會自己去查\n"
            "• **貼網址**給我，我會去把那一頁讀完再回你\n"
            "• 單純聊天、訴苦的時候我不會亂查，就好好陪你 💛"
        ),
        inline=False,
    )

    embed.add_field(
        name="🌸 私人聊天室",
        value=(
            "• `/room open` — 開一間只有你和我的房間，**裡面不用 @我**，講什麼我都回\n"
            "• `/room close` — 聊完關掉\n"
            "• 敏感的事（身分證、病歷、心裡話）在裡面講比較安全"
        ),
        inline=False,
    )

    embed.add_field(
        name="🖥️ 幫你上網辦事",
        value=(
            "• `/browse` 或直接說「**幫我掛台大心臟科下週三下午**」\n"
            "• 我會自己開瀏覽器點按鈕、填表單，做完**把畫面截圖傳給你**\n"
            "• **送出前一定先問你**，你按確認我才按下去\n"
            "• 身分證、生日這些我不會自己編，缺了會問你\n"
            "• 遇到圖形驗證碼或要登入帳號，我會停下來把畫面給你接手"
        ),
        inline=False,
    )

    embed.add_field(
        name="⌨️ 斜線指令",
        value=(
            "• `/chat` — 和奈奈聊天\n"
            "• `/help` — 顯示此說明\n"
            "• `/mood` — 心情小提醒\n"
            "• `/reset` — 重新開始對話\n"
            "• `/memories` — 看我記得你什麼\n"
            "• `/forget` — 清掉我對你的記憶\n"
            "• `/reminders` — 看你設的提醒\n"
            "• `/todos` — 看你的待辦清單\n"
            "• `/support` — 心理健康資源"
        ),
        inline=True,
    )

    embed.add_field(
        name="🎙️ 語音指令",
        value=(
            "• `/join` — 加入語音頻道\n"
            "• `/leave` — 離開語音頻道\n"
            "• `/status` — 查看狀態"
        ),
        inline=True,
    )

    embed.add_field(
        name="⚙️ 管理員設定",
        value=(
            "• `/settings alert_channel` — 設定警告頻道\n"
            "• `/settings disable_alert` — 停用警告\n"
            "• `/settings view` — 查看設定"
        ),
        inline=False,
    )

    embed.add_field(
        name="🤗 自動功能",
        value=(
            f"• {config.BOT_NAME}會關注所有訊息的情緒 💛\n"
            f"• 偵測到需要支持時會主動關心\n"
            f"• 偵測到危險訊號時通知管理員 🚨"
        ),
        inline=False,
    )

    embed.set_footer(text="你不是一個人。需要專業協助：安心專線 1925 ｜ 生命線 1995")
    return embed


async def send_long_message(
    channel: discord.abc.Messageable,
    content: str,
    reference: discord.Message | None = None,
    max_length: int = 1900,
):
    """將過長的訊息分段發送"""
    if len(content) <= max_length:
        if reference:
            await reference.reply(content, mention_author=False)
        else:
            await channel.send(content)
        return

    chunks = []
    while content:
        if len(content) <= max_length:
            chunks.append(content)
            break
        split_pos = max_length
        for sep in ["\n", "。", "！", "？", ".", "!", "?", " "]:
            pos = content.rfind(sep, 0, max_length)
            if pos > max_length // 2:
                split_pos = pos + 1
                break
        chunks.append(content[:split_pos])
        content = content[split_pos:]

    for i, chunk in enumerate(chunks):
        if i == 0 and reference:
            await reference.reply(chunk, mention_author=False)
        else:
            await channel.send(chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)


# ═══════════════════════════════════════════════════════
# 定時任務
# ═══════════════════════════════════════════════════════


def _fmt_stats_block(d: dict[str, int]) -> str:
    """把統計數字排成簡介裡的那一段。

    只放「有發生過」的項目 —— 全新的機器人簡介上掛一排 0 很難看，
    而且會讓人以為功能是壞的。
    """
    bits = []
    if d["people"]:
        bits.append(f"陪 {d['people']:,} 個人")
    if d["messages"]:
        bits.append(f"聊了 {d['messages']:,} 句")
    if d["memories"]:
        bits.append(f"記住 {d['memories']:,} 件事")
    if d["cares"]:
        bits.append(f"主動關心 {d['cares']:,} 次")
    if d["reminders"]:
        bits.append(f"提醒 {d['reminders']:,} 次")
    if d["browse"]:
        bits.append(f"上網幫忙 {d['browse']:,} 趟")
    if d["reactions"]:
        bits.append(f"貼了 {d['reactions']:,} 個表情")
    if not bits:
        return ""
    return f"{config.PROFILE_STATS_MARK} " + "、".join(bits)


# 固定的介紹文字。第一次更新時從 Discord 上現有的簡介讀回來（把統計那段切掉），
# 之後每次更新都用它重新組 —— 不然統計會一段一段往後疊。
_profile_base: str | None = None


@tasks.loop(minutes=config.PROFILE_STATS_INTERVAL_MIN)
async def update_profile_stats():
    """把累計統計寫進機器人的簡介（個人資料那段描述）。"""
    if not config.PROFILE_STATS_ENABLED:
        return

    global _profile_base
    try:
        headers = {"Authorization": f"Bot {config.DISCORD_TOKEN}"}
        async with httpx.AsyncClient(timeout=20) as c:
            if _profile_base is None:
                r = await c.get("https://discord.com/api/v10/applications/@me",
                                headers=headers)
                if r.status_code != 200:
                    logger.warning("📊 讀不到現在的簡介（%s），這次跳過", r.status_code)
                    return
                current = (r.json().get("description") or "").strip()
                # 切掉上一次寫進去的統計，留下人寫的介紹文字
                _profile_base = current.split(config.PROFILE_STATS_MARK)[0].rstrip()
                logger.info("📊 簡介的固定內容記下來了（%d 字）", len(_profile_base))

            block = _fmt_stats_block(stats.collect())
            if not block:
                return
            desc = f"{_profile_base}\n\n{block}" if _profile_base else block
            if len(desc) > config.PROFILE_MAX_LEN:
                # 超過上限就砍統計那段，不要砍介紹 —— 介紹是人寫的，統計是附加的
                room = config.PROFILE_MAX_LEN - len(_profile_base or "") - 2
                if room < 20:
                    logger.warning("📊 簡介本文已經佔滿 400 字，放不下統計")
                    return
                desc = f"{_profile_base}\n\n{block[:room - 1]}…"

            r = await c.patch("https://discord.com/api/v10/applications/@me",
                              headers=headers, json={"description": desc})
            if r.status_code != 200:
                logger.warning("📊 更新簡介失敗：%s %s", r.status_code, r.text[:200])
                return
        logger.info("📊 簡介統計已更新：%s", block)
    except Exception as e:  # noqa: BLE001
        logger.warning("📊 更新簡介出錯（忽略）：%s", e)


@tasks.loop(minutes=30)
async def cleanup_sessions():
    count = conv_manager.cleanup_stale()
    if count:
        logger.info("🧹 已清除 %d 個過期 session", count)


@tasks.loop(seconds=config.REMINDER_CHECK_SECONDS)
async def deliver_reminders():
    """把到期的提醒送出去。

    包含機器人離線期間錯過的 —— 那些會照樣送出並註明遲到，總比默默消失好。
    無論送出成功與否都要 mark_fired，否則失敗的提醒會每 30 秒重試一次洗版。
    """
    try:
        due = await reminders.due_now()
    except Exception as e:  # noqa: BLE001
        logger.warning("讀取到期提醒失敗：%s", e)
        return

    for rem in due:
        try:
            target = bot.get_channel(rem.channel_id)
            if target is None:
                # 私訊或抓不到快取 → 改用使用者 DM
                user = bot.get_user(rem.user_id) or await bot.fetch_user(rem.user_id)
                target = user.dm_channel or await user.create_dm()

            if rem.kind == "auto_checkin":
                # 治理閘門 —— 排進來不代表一定要送
                enabled, last_proactive, last_seen = await reminders.get_prefs(rem.user_id)
                now = datetime.now()
                skip = None
                if not (config.AGENT_ENABLED and config.AUTO_CHECKIN_ENABLED):
                    skip = "功能已關閉"
                elif not enabled:
                    skip = "使用者已退出"
                elif last_proactive and (now - last_proactive) < timedelta(
                        days=config.AUTO_CHECKIN_MIN_GAP_DAYS):
                    skip = "距上次主動關心太近"
                elif last_seen and (now - last_seen) < timedelta(
                        minutes=config.AUTO_CHECKIN_SKIP_IF_ACTIVE_MINUTES):
                    # 他人就在線上講話，這時候「主動關心」是打擾而不是關心
                    skip = "他正在線上聊天"
                if skip:
                    logger.info("💛 自動關心 #%d 跳過：%s", rem.id, skip)
                    continue

                mem_ctx = await recall_memory(rem.user_id, rem.text)
                msg = await llm_client.generate_support_response(
                    user_message=config.AUTO_CHECKIN_PROMPT.format(trigger=rem.text),
                    memory_context=mem_ctx,
                )
                if not msg:
                    logger.warning("💛 自動關心 #%d 生成失敗，跳過", rem.id)
                    continue
                sent = await target.send(f"<@{rem.user_id}> {msg}")
                await reminders.mark_proactive(rem.user_id)
                # 記下「這句話是為了哪件事講的」。她被要求不要複述原話，所以
                # 講出來會是「之前聽你提到那些說法」——對方回一句「哪些說法」時
                # 沒有這筆對照，她就只能說「抱歉我沒對上訊號」（實際發生過）。
                await _remember_proactive(sent, rem.user_id, rem.text, msg, target)
                logger.info("💛 已送出自動關心 #%d", rem.id)

            elif rem.kind == "checkin":
                # 主動關心：不念字面，讓奈奈依長期記憶臨場想一句開場白。
                # 遲到太多就跳過 —— 半夜補一句「早安」很怪。
                if rem.late_minutes > config.CHECKIN_SKIP_LATE_MINUTES:
                    logger.info("💛 主動關心 #%d 遲到 %d 分，跳過這次",
                                rem.id, rem.late_minutes)
                    continue

                mem_ctx = await recall_memory(rem.user_id, "最近過得如何")
                msg = await llm_client.generate_support_response(
                    user_message=config.CHECKIN_PROMPT,
                    memory_context=mem_ctx,
                )
                if not msg:
                    logger.warning("💛 主動關心 #%d 生成失敗，跳過", rem.id)
                    continue
                sent = await target.send(f"<@{rem.user_id}> {msg}")
                stats.bump(stats.CARES)
                # 這條路沒有單一 trigger，改存當時手上的長期記憶摘要 ——
                # 她那句開場白就是從這些東西想出來的
                await _remember_proactive(sent, rem.user_id, mem_ctx, msg, target)
                logger.info("💛 已送出主動關心 #%d", rem.id)
            else:
                late = ""
                if rem.late_minutes >= config.REMINDER_LATE_NOTICE_MINUTES:
                    hrs, mins = divmod(rem.late_minutes, 60)
                    ago = f"{hrs} 小時 {mins} 分" if hrs else f"{mins} 分"
                    late = f"\n-# 抱歉晚了 {ago}，我剛才離線了 🥺"

                await target.send(f"⏰ <@{rem.user_id}> 提醒你：**{rem.text}**{late}")
                logger.info("⏰ 已送出提醒 #%d │ %s │ 遲到 %d 分",
                            rem.id, rem.text, rem.late_minutes)
        except Exception as e:  # noqa: BLE001
            logger.warning("送出提醒 #%d 失敗：%s", rem.id, e)
        finally:
            # 一定要推進，否則失敗的提醒會每輪重試造成洗版
            try:
                await reminders.mark_fired(rem)
            except Exception as e:  # noqa: BLE001
                logger.error("提醒 #%d 狀態更新失敗：%s", rem.id, e)


@deliver_reminders.before_loop
async def _before_reminders():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════
# 啟動
# ═══════════════════════════════════════════════════════


def main():
    if not config.DISCORD_TOKEN:
        logger.error("❌ 請在 .env 中設定 DISCORD_TOKEN")
        return

    logger.info("🌸 正在啟動 %s...", config.BOT_NAME)
    logger.info("   LM Studio: %s", config.LM_STUDIO_BASE_URL)
    logger.info("   模型: %s", config.LM_STUDIO_MODEL)
    logger.info("   語音 AI: %s", config.VOICE_SERVER_URL)

    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
