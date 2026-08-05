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
import logging
import re
import threading
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import attachments
import config
import llm_client
import agent
import memory
import reactions
import reminders
import webfetch
import websearch
from conversation import ConversationManager
from voice_bridge import bridge, OmniSink, AIAudioSource, recording_done
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

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)

conv_manager = ConversationManager()
review_manager = ReviewManager()


# ═══════════════════════════════════════════════════════
# 事件處理
# ═══════════════════════════════════════════════════════


@bot.event
async def on_ready():
    """機器人上線"""
    logger.info("🌸 %s 已上線！(%s)", config.BOT_NAME, bot.user)
    logger.info("   已連接 %d 個伺服器", len(bot.guilds))
    logger.info("   觸發關鍵字: %s", ", ".join(config.TRIGGER_KEYWORDS))
    if config.ALERT_CHANNEL_ID:
        logger.info("   ⚠️ 危險訊息警告頻道: %d", config.ALERT_CHANNEL_ID)
    else:
        logger.warning("   ⚠️ 未設定 ALERT_CHANNEL_ID，危險訊息警告功能停用")

    activity = discord.Activity(
        type=discord.ActivityType.listening,
        name="你的心聲 💛 | /help",
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)

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

    if not cleanup_sessions.is_running():
        cleanup_sessions.start()
    if not deliver_reminders.is_running():
        deliver_reminders.start()


@bot.event
async def on_message(message: discord.Message):
    """處理所有收到的訊息"""
    if message.author == bot.user or message.author.bot:
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

    # 關鍵字偵測（文字訊息中包含「奈奈」等關鍵字）
    content_lower = message.content.lower()
    has_keyword = getattr(config, "REPLY_ON_KEYWORD", True) and any(kw in content_lower for kw in config.TRIGGER_KEYWORDS)

    # 附件只在下面這些「有搭話」的情境才會被讀（見 handle_direct_conversation），
    # 一般頻道裡別人隨手貼的檔案奈奈不會去翻。

    talking_to_nana = bool(
        is_fixed_channel or is_mentioned or is_reply_to_bot or is_dm or has_keyword
    )

    if talking_to_nana:
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


async def _noop_str() -> str:
    """給 asyncio.gather 用的空佔位（已經讀了連結就不再搜尋）。"""
    return ""


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


async def maybe_search(content: str, progress: "Progress | None" = None) -> str:
    """判斷這句話要不要上網查；要的話回傳塞進 prompt 的結果區塊，否則回空字串。

    兩段式，為了不讓「陪聊」這條主線白白多花一次 LLM 呼叫：
      1. 明確講「查一下 / 搜尋 / google」→ 直接搜，關鍵字就是去掉觸發詞的剩餘部分
      2. 看起來像在問資訊（有問號、什麼、最新…）→ 才問模型要不要搜
      3. 其餘（訴苦、打招呼、閒聊）→ 完全不碰網路
    """
    if not config.WEB_SEARCH_ENABLED or not content:
        return ""

    lowered = content.lower()

    # ① 明確要求
    for kw in config.SEARCH_TRIGGERS:
        if kw in lowered:
            query = re.sub(re.escape(kw), " ", content, flags=re.IGNORECASE)
            for extra in ("奈奈", "nana", "幫我", "一下", "好嗎", "好不好", "謝謝"):
                query = re.sub(re.escape(extra), " ", query, flags=re.IGNORECASE)
            query = re.sub(r"\s+", " ", query).strip(" ，。,.?？!！")
            if not query:
                return ""
            logger.info("🔎 明確要求搜尋 │ %s", query[:60])
            return await _run_search(query, progress)

    # ② 像在問資訊 → 交給模型判斷
    if not any(h in lowered for h in config.QUESTION_HINTS):
        return ""

    need, query = await llm_client.decide_search(content)
    if not need:
        return ""

    logger.info("🔎 模型判定需要搜尋 │ %s", query[:60])
    return await _run_search(query, progress)


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
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
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
    is_admin = bool(message.guild and message.author.guild_permissions.manage_guild)
    agent_text = content
    if target_user:   # 把 @提及換成名字，免得 <@id> 干擾 LLM 解析
        for tag in (f"<@{target_user.id}>", f"<@!{target_user.id}>"):
            agent_text = agent_text.replace(tag, target_user.display_name)
    agent_result = await agent.handle(
        agent_text, user_id=user_id, user_name=user_name, channel_id=channel_id,
        target_user_id=(target_user.id if target_user else None),
        target_user_name=(target_user.display_name if target_user else None),
        is_admin=is_admin,
    )
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
        memory_context, search_block = await asyncio.gather(
            recall_memory(user_id, content),
            maybe_search(content, progress) if not fetch_block else _noop_str(),
        )

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
    if not ctx.author.voice:
        await ctx.respond("❌ 請先加入語音頻道！", ephemeral=True)
        return

    channel = ctx.author.voice.channel
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
                await target.send(f"<@{rem.user_id}> {msg}")
                await reminders.mark_proactive(rem.user_id)
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
                await target.send(f"<@{rem.user_id}> {msg}")
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
