"""重點關注用戶的訊息人工審核與持久化佇列。"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import discord

import config
import llm_client

logger = logging.getLogger("nana.focus_review")
_DB_PATH = Path(__file__).parent / "focus_review.db"
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

AI_MODERATION_PROMPT = """你是 Discord 社群的訊息審核員。判斷訊息是否適合公開發佈。
拒絕包含騷擾、仇恨、威脅、露骨色情、個資外洩、詐騙、惡意連結、洗版，或鼓勵自傷／犯罪的內容。
一般聊天、合理抱怨、心理支持與善意討論應通過。不要因為負面情緒本身拒絕。
只回 JSON：{"decision":"approve 或 reject","reason":"簡短繁體中文理由"}。"""


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(_DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("""
        CREATE TABLE IF NOT EXISTS pending_focus_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            author_avatar TEXT,
            content TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            review_message_id INTEGER,
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_id INTEGER,
            reviewed_at REAL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS pending_focus_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pending_id INTEGER NOT NULL REFERENCES pending_focus_messages(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            data BLOB NOT NULL
        )
    """)
    db.commit()
    return db


@dataclass
class PendingMessage:
    id: int
    guild_id: int
    channel_id: int
    author_id: int
    author_name: str
    author_avatar: str | None
    content: str
    source_message_id: int
    review_message_id: int | None
    created_at: float
    status: str


def _from_row(row: sqlite3.Row) -> PendingMessage:
    return PendingMessage(**{key: row[key] for key in PendingMessage.__annotations__})


def get_pending_by_review_message(review_message_id: int) -> PendingMessage | None:
    with _connect() as db:
        row = db.execute(
            "SELECT * FROM pending_focus_messages WHERE review_message_id=? AND status='pending'",
            (review_message_id,),
        ).fetchone()
    return _from_row(row) if row else None


def pending_count() -> int:
    with _connect() as db:
        return int(db.execute(
            "SELECT COUNT(*) FROM pending_focus_messages WHERE status='pending'"
        ).fetchone()[0])


def pending_summary(limit: int = 15) -> list[PendingMessage]:
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM pending_focus_messages WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT ?", (limit,)
        ).fetchall()
    return [_from_row(row) for row in rows]


def _mark(message_id: int, status: str, reviewer_id: int) -> bool:
    with _connect() as db:
        cur = db.execute(
            "UPDATE pending_focus_messages SET status=?, reviewer_id=?, reviewed_at=? "
            "WHERE id=? AND status='pending'",
            (status, reviewer_id, time.time(), message_id),
        )
        db.commit()
        return cur.rowcount == 1


def _claim(message_id: int, reviewer_id: int) -> bool:
    """先取得處理權，避免兩位管理員同時核准而重複發佈。"""
    return _mark(message_id, "publishing", reviewer_id)


def _restore_pending(message_id: int) -> None:
    with _connect() as db:
        db.execute(
            "UPDATE pending_focus_messages SET status='pending', reviewer_id=NULL, reviewed_at=NULL "
            "WHERE id=? AND status='publishing'", (message_id,)
        )
        db.commit()


def _attachments(message_id: int) -> list[tuple[str, bytes]]:
    with _connect() as db:
        rows = db.execute(
            "SELECT filename, data FROM pending_focus_attachments WHERE pending_id=? ORDER BY id",
            (message_id,),
        ).fetchall()
    return [(row["filename"], bytes(row["data"])) for row in rows]


async def ai_decide(content: str) -> tuple[str, str] | None:
    try:
        raw = await llm_client.review_completion([
            {"role": "system", "content": AI_MODERATION_PROMPT},
            {"role": "user", "content": content},
        ], temperature=0.0, max_tokens=256, timeout=60.0)
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        data = json.loads(match.group() if match else (raw or ""))
        decision = str(data.get("decision", "")).lower()
        if decision not in {"approve", "reject"}:
            return None
        return decision, str(data.get("reason", "未提供理由"))[:500]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("AI 訊息審核結果無法解析：%s", exc)
        return None


async def _notify_user(bot: discord.Client, user_id: int, text: str) -> None:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(text)
    except discord.HTTPException:
        pass


def review_embed(item: PendingMessage) -> discord.Embed:
    content = item.content or "*（只有附件）*"
    embed = discord.Embed(
        title="🔎 重點關注訊息待審核",
        description=content[:4096],
        color=0xF0A500,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=item.author_name, icon_url=item.author_avatar or None)
    embed.add_field(name="發言者", value=f"<@{item.author_id}> (`{item.author_id}`)", inline=True)
    embed.add_field(name="原頻道", value=f"<#{item.channel_id}>", inline=True)
    embed.set_footer(text=f"審核編號 #{item.id} • 原訊息已攔截")
    return embed


async def intercept(message: discord.Message, bot: discord.Client) -> bool:
    """攔截名單成員訊息；成功攔截時回傳 True。"""
    if message.guild is None or message.author.id not in config.FOCUS_WATCHED_USERS:
        return False
    if not config.FOCUS_REVIEW_CHANNEL_ID:
        logger.warning("重點關注名單非空，但尚未設定審核頻道")
        return False
    review_channel = bot.get_channel(config.FOCUS_REVIEW_CHANNEL_ID)
    if review_channel is None:
        try:
            review_channel = await bot.fetch_channel(config.FOCUS_REVIEW_CHANNEL_ID)
        except discord.HTTPException:
            logger.error("找不到重點關注審核頻道 %s", config.FOCUS_REVIEW_CHANNEL_ID)
            return False

    blobs: list[tuple[str, bytes]] = []
    for attachment in message.attachments:
        try:
            raw = await attachment.read()
            if len(raw) <= _MAX_ATTACHMENT_BYTES:
                blobs.append((attachment.filename, raw))
            else:
                blobs.append((attachment.filename + ".too-large.txt", b"Attachment exceeded 25 MiB"))
        except discord.HTTPException as exc:
            logger.warning("讀取待審附件失敗 %s: %s", attachment.filename, exc)

    # 先完整保存，再刪除原訊息。資料庫故障時寧可不攔截，也不能讓訊息憑空消失。
    avatar = getattr(message.author.display_avatar, "url", None)
    try:
        with _connect() as db:
            cur = db.execute(
                "INSERT INTO pending_focus_messages "
                "(guild_id, channel_id, author_id, author_name, author_avatar, content, "
                "source_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message.guild.id, message.channel.id, message.author.id,
                 message.author.display_name, str(avatar) if avatar else None,
                 message.content, message.id, time.time()),
            )
            pending_id = int(cur.lastrowid)
            db.executemany(
                "INSERT INTO pending_focus_attachments (pending_id, filename, data) VALUES (?, ?, ?)",
                [(pending_id, name, data) for name, data in blobs],
            )
            db.commit()
    except sqlite3.Error as exc:
        logger.exception("無法保存待審訊息，取消攔截：%s", exc)
        try:
            await review_channel.send(
                f"⚠️ 審核資料庫寫入失敗，未攔截 <@{message.author.id}> 在 "
                f"{message.channel.mention} 的訊息，請管理員檢查日誌。"
            )
        except discord.HTTPException:
            pass
        return False

    try:
        await message.delete(reason="重點關注用戶：訊息送交管理員審核")
    except discord.HTTPException as exc:
        # 原訊息仍在，撤銷這筆佇列資料，避免稍後又被核准發佈一次。
        with _connect() as db:
            db.execute("DELETE FROM pending_focus_messages WHERE id=?", (pending_id,))
            db.commit()
        logger.error("無法攔截 %s 的訊息（請檢查管理訊息權限）: %s", message.author, exc)
        try:
            await review_channel.send(
                f"⚠️ 無法攔截 <@{message.author.id}> 在 {message.channel.mention} 的訊息；"
                "請確認機器人有「管理訊息」權限。"
            )
        except discord.HTTPException:
            pass
        return False

    item = PendingMessage(
        pending_id, message.guild.id, message.channel.id, message.author.id,
        message.author.display_name, str(avatar) if avatar else None,
        message.content, message.id, None, time.time(), "pending",
    )

    # 附件不能只靠文字模型可靠判斷，因此即使在 AI 模式也轉人工，避免未審附件外流。
    if config.FOCUS_REVIEW_MODE == "ai" and not blobs and message.content.strip():
        result = await ai_decide(message.content)
        if result is not None:
            decision, reason = result
            if decision == "approve":
                channel = bot.get_channel(item.channel_id) or await bot.fetch_channel(item.channel_id)
                published = discord.Embed(description=item.content[:4096], color=0x57F287)
                published.set_author(name=item.author_name, icon_url=item.author_avatar or None)
                published.set_footer(text="此訊息經 AI 審核後發佈")
                try:
                    await channel.send(embed=published, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as exc:
                    logger.error("AI 核准後發佈失敗，轉人工：%s", exc)
                else:
                    _mark(item.id, "ai_approved", bot.user.id)
                    audit = review_embed(item)
                    audit.title = "🤖 AI 已核准並發佈"
                    audit.color = 0x57F287
                    audit.add_field(name="AI 理由", value=reason, inline=False)
                    await review_channel.send(embed=audit)
                    await _notify_user(bot, item.author_id, "你的訊息已通過 AI 審核並發佈。")
                    return True
            else:
                _mark(item.id, "ai_rejected", bot.user.id)
                audit = review_embed(item)
                audit.title = "🤖 AI 已拒絕"
                audit.color = 0xED4245
                audit.add_field(name="AI 理由", value=reason, inline=False)
                await review_channel.send(embed=audit)
                await _notify_user(bot, item.author_id, "你的訊息未通過 AI 審核，因此沒有發佈。")
                return True
        logger.warning("AI 審核失敗，訊息 #%d 自動轉人工審核", item.id)

    try:
        review_message = await review_channel.send(embed=review_embed(item), view=FocusReviewView())
    except discord.HTTPException:
        logger.exception("送出重點關注審核卡片失敗")
        await _notify_user(bot, message.author.id, "你的訊息攔截成功，但審核系統暫時無法送件，請聯絡管理員。")
        return True

    with _connect() as db:
        db.execute("UPDATE pending_focus_messages SET review_message_id=? WHERE id=?",
                   (review_message.id, pending_id))
        db.commit()
    await _notify_user(bot, message.author.id, "你的訊息已送交管理員審核，通過後才會在原頻道發佈。")
    logger.info("🔎 已攔截重點用戶訊息 #%d │ %s", pending_id, message.author)
    return True


class FocusReviewView(discord.ui.View):
    """固定 custom_id，讓重啟前產生的審核卡片仍可操作。"""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and perms.administrator:
            return True
        await interaction.response.send_message("只有管理員可以審核訊息。", ephemeral=True)
        return False

    async def _item(self, interaction: discord.Interaction) -> PendingMessage | None:
        item = get_pending_by_review_message(interaction.message.id)
        if item is None:
            await interaction.response.send_message("這則訊息已處理或找不到審核資料。", ephemeral=True)
        return item

    @discord.ui.button(label="核准發佈", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id="focus_review:approve")
    async def approve(self, _button: discord.ui.Button, interaction: discord.Interaction):
        item = await self._item(interaction)
        if item is None:
            return
        await interaction.response.defer(ephemeral=True)
        if not _claim(item.id, interaction.user.id):
            await interaction.followup.send("這則訊息已被其他管理員處理。", ephemeral=True)
            return
        channel = interaction.client.get_channel(item.channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(item.channel_id)
            except discord.HTTPException:
                _restore_pending(item.id)
                await interaction.followup.send("找不到原頻道，無法發佈。", ephemeral=True)
                return
        files = [discord.File(io.BytesIO(data), filename=name)
                 for name, data in _attachments(item.id)]
        published = discord.Embed(description=(item.content or "*（只有附件）*")[:4096], color=0x57F287)
        published.set_author(name=item.author_name, icon_url=item.author_avatar or None)
        published.set_footer(text="此訊息經管理員審核後發佈")
        try:
            await channel.send(embed=published, files=files,
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            _restore_pending(item.id)
            await interaction.followup.send(f"發佈失敗：{exc}", ephemeral=True)
            return
        with _connect() as db:
            db.execute(
                "UPDATE pending_focus_messages SET status='approved', reviewed_at=? WHERE id=?",
                (time.time(), item.id),
            )
            db.commit()
        done = review_embed(item)
        done.color = 0x57F287
        done.title = "✅ 已核准並發佈"
        done.set_footer(text=f"審核編號 #{item.id} • 審核者 {interaction.user}")
        await interaction.message.edit(embed=done, view=None)
        await _notify_user(interaction.client, item.author_id, "你的訊息已通過審核並發佈。")
        await interaction.followup.send("已發佈到原頻道。", ephemeral=True)

    @discord.ui.button(label="拒絕", style=discord.ButtonStyle.danger,
                       emoji="✖️", custom_id="focus_review:reject")
    async def reject(self, _button: discord.ui.Button, interaction: discord.Interaction):
        item = await self._item(interaction)
        if item is None:
            return
        if not _mark(item.id, "rejected", interaction.user.id):
            await interaction.response.send_message("這則訊息已被其他管理員處理。", ephemeral=True)
            return
        done = review_embed(item)
        done.color = 0xED4245
        done.title = "✖️ 已拒絕"
        done.set_footer(text=f"審核編號 #{item.id} • 審核者 {interaction.user}")
        await interaction.response.edit_message(embed=done, view=None)
        await _notify_user(interaction.client, item.author_id, "你的訊息未通過管理員審核，因此沒有發佈。")


class FocusControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and perms.administrator:
            return True
        await interaction.response.send_message("只有管理員可以使用控制台。", ephemeral=True)
        return False

    @discord.ui.button(label="查看名單", style=discord.ButtonStyle.primary, emoji="👥")
    async def users(self, _button: discord.ui.Button, interaction: discord.Interaction):
        users = config.FOCUS_WATCHED_USERS
        text = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in users) or "目前沒有重點關注用戶。"
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="待審佇列", style=discord.ButtonStyle.secondary, emoji="📨")
    async def queue(self, _button: discord.ui.Button, interaction: discord.Interaction):
        items = pending_summary()
        text = "\n".join(
            f"• `#{item.id}` <@{item.author_id}> → <#{item.channel_id}>" for item in items
        ) or "目前沒有待審訊息。"
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="操作說明", style=discord.ButtonStyle.secondary, emoji="ℹ️")
    async def help(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_message(
            "使用 `/focus channel` 設定審核頻道、`/focus add` 加入名單、"
            "`/focus remove` 移除名單。審核請直接使用審核頻道卡片上的按鈕。",
            ephemeral=True,
        )

    @discord.ui.button(label="人工審核", style=discord.ButtonStyle.success, emoji="👤", row=1)
    async def manual(self, _button: discord.ui.Button, interaction: discord.Interaction):
        config.FOCUS_REVIEW_MODE = "manual"
        config.save_settings()
        await interaction.response.edit_message(embed=control_embed(), view=self)

    @discord.ui.button(label="AI 審核", style=discord.ButtonStyle.success, emoji="🤖", row=1)
    async def ai(self, _button: discord.ui.Button, interaction: discord.Interaction):
        config.FOCUS_REVIEW_MODE = "ai"
        config.save_settings()
        await interaction.response.edit_message(embed=control_embed(), view=self)


def control_embed() -> discord.Embed:
    channel = f"<#{config.FOCUS_REVIEW_CHANNEL_ID}>" if config.FOCUS_REVIEW_CHANNEL_ID else "❌ 未設定"
    embed = discord.Embed(title="🔎 重點關注控制台", color=0x5865F2)
    embed.add_field(name="審核頻道", value=channel, inline=True)
    embed.add_field(name="關注人數", value=str(len(config.FOCUS_WATCHED_USERS)), inline=True)
    embed.add_field(name="待審訊息", value=str(pending_count()), inline=True)
    embed.add_field(
        name="審核模式",
        value="🤖 AI 審核" if config.FOCUS_REVIEW_MODE == "ai" else "👤 人工審核",
        inline=True,
    )
    embed.description = "所有名單成員的伺服器發言都會先被攔截，管理員核准後才重新發佈。"
    return embed
