"""
奈奈機器人 — 新成員審核系統
自動偵測表單、AI 審核、追蹤待確認用戶
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import discord

import config
import llm_client

logger = logging.getLogger("nana.review")

# ── 審核 Prompt ────────────────────────────────────────

REVIEW_PROMPT = """你是一位 Discord 社群的新成員審核員。你需要根據表單內容判斷是否通過。
本社群為病友互助空間。

## 判斷規則（符合哪一條就執行對應動作）

1. 若申請者自己表明是「健康的人」、「沒有困擾」、「單純交友來玩」，請回傳 "reject"。
2. 若申請者是為了「陪伴病患」、「家屬」、「朋友代問」，請回傳 "redirect_admin"（讓管理員手動審核）。
3. 若內容出現明顯危險訊號（自殺、自傷），請回傳 "reject"。
4. 若申請者為病患，且「沒有就醫也沒有諮商」，請回傳 "reject"。
5. 若內容太短、模糊不清無法判斷，請回傳 "follow_up"。
6. 排除上述情況，請回傳 "approve"。

## 回覆格式
請根據以上條件，回傳下列 JSON 結構（只回傳 JSON 即可）：

```json
{
  "decision": "approve" | "reject" | "follow_up" | "redirect_admin",
  "reason": "簡短說明原因",
  "follow_up_question": "如果 decision 是 follow_up，這裡放要追問的問題",
  "risk_level": 1-5
}
```
"""

FOLLOWUP_PROMPT = """你是 Discord 社群的新成員審核員。這位用戶之前提交了入會表單，但資訊不夠明確，你已經追問了問題。
現在用戶回覆了，請根據所有對話內容，重新判斷是否通過審核。

## 原始表單
{form_content}

## 追問歷史
{conversation}

## 判斷規則

1. 若明確為「健康來交友」，請回傳 "reject"。
2. 若明確為「家屬/陪同者」，請回傳 "redirect_admin"。
3. 若有危險訊號（自殺、自傷等），請回傳 "reject"。
4. 若為病患但「未就醫未諮商」，請回傳 "reject"。
5. 若依然模糊不清，請回傳 "follow_up" 繼續追問。
6. 其他情況都沒問題，請回傳 "approve"。

## 回覆格式
請回傳下列 JSON 結構（只回傳 JSON）：
```json
{{
  "decision": "approve" | "reject" | "follow_up" | "redirect_admin",
  "reason": "簡短說明原因",
  "follow_up_question": "如果需要繼續追問，這裡放問題",
  "risk_level": 1-5
}}
```
"""


# ── 表單欄位 ───────────────────────────────────────────

FORM_FIELDS = [
    "我叫",
    "我從這裡來",
    "我目前的困擾有",
    "是否有正在諮商或就醫",
    "為什麼想加入這邊",
    "我最近狀況如何",
]


@dataclass
class PendingReview:
    """追蹤待確認的用戶"""
    user_id: int
    channel_id: int
    form_content: str
    form_message_id: int
    conversation: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    follow_up_count: int = 0
    max_follow_ups: int = 5


class ReviewManager:
    """新成員審核管理器"""

    def __init__(self):
        self._pending: dict[int, PendingReview] = {}  # user_id -> PendingReview

    def is_pending(self, user_id: int) -> bool:
        return user_id in self._pending

    def get_pending(self, user_id: int) -> Optional[PendingReview]:
        return self._pending.get(user_id)

    def add_pending(self, review: PendingReview):
        self._pending[review.user_id] = review

    def remove_pending(self, user_id: int):
        self._pending.pop(user_id, None)

    def cleanup_stale(self, max_age_hours: int = 48) -> int:
        """清除超過 max_age_hours 的待審核"""
        now = time.time()
        stale = [
            uid for uid, r in self._pending.items()
            if now - r.last_activity > max_age_hours * 3600
        ]
        for uid in stale:
            del self._pending[uid]
        return len(stale)


def extract_json(text: str) -> Optional[dict]:
    import json
    import re
    # 移除 <think> 標籤和內文
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    
    # 嘗試找 markdown 裡的 json
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n*(.*?)\n*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
            
    # 擷取以 { 開頭和 } 結尾的區塊
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        json_str = text[start_idx:end_idx+1]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def detect_form(content: str) -> bool:
    """偵測訊息是否為表單格式"""
    matched = 0
    for f in FORM_FIELDS:
        if f in content:
            matched += 1
    # 至少匹配 3 個欄位就算是表單
    return matched >= 3


def parse_form(content: str) -> dict[str, str]:
    """解析表單欄位內容"""
    result = {}
    lines = content.split("\n")
    current_field = None
    current_value = []

    for line in lines:
        stripped = line.strip()
        found_field = False
        for f in FORM_FIELDS:
            if stripped.startswith(f):
                if current_field:
                    result[current_field] = "\n".join(current_value).strip()
                current_field = f
                # 取冒號後面的內容
                after = stripped[len(f):]
                after = after.lstrip(":：").strip()
                current_value = [after] if after else []
                found_field = True
                break
        if not found_field and current_field:
            current_value.append(stripped)

    if current_field:
        result[current_field] = "\n".join(current_value).strip()

    return result


async def ai_review(form_content: str) -> Optional[dict]:
    """使用 AI 審核表單"""
    messages = [
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user", "content": f"以下是新成員提交的入會表單：\n\n{form_content}"},
    ]

    result = await llm_client.review_completion(messages, temperature=0.1, max_tokens=1024)
    if not result:
        return None


    parsed = extract_json(result)
    if not parsed:
        logger.warning("無法解析審核結果:\n---- BEGIN ----\n%s\n---- END ----", result)
        with open("/tmp/nana_llm_debug.log", "w", encoding="utf-8") as f:
            f.write(result)
    return parsed


async def ai_followup_review(pending: PendingReview) -> Optional[dict]:
    """使用 AI 根據追問對話重新審核"""
    import json

    conv_text = "\n".join(
        f"{'審核員' if m['role'] == 'assistant' else '用戶'}: {m['content']}"
        for m in pending.conversation
    )

    prompt = FOLLOWUP_PROMPT.format(
        form_content=pending.form_content,
        conversation=conv_text,
    )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": pending.conversation[-1]["content"]},
    ]

    result = await llm_client.review_completion(messages, temperature=0.1, max_tokens=1024)
    if not result:
        return None

    parsed = extract_json(result)
    if not parsed:
        logger.warning("無法解析追問審核結果:\n---- BEGIN ----\n%s\n---- END ----", result)
        with open("/tmp/nana_llm_debug_followup.log", "w", encoding="utf-8") as f:
            f.write(result)
    return parsed


async def approve_member(
    member: discord.Member,
    channel: discord.abc.Messageable,
    reason: str = "",
):
    """通過審核：移除舊 tag、加上新 tag、發送通過訊息"""
    guild = member.guild
    errors = []

    # 移除舊 role
    if config.REVIEW_REMOVE_ROLE_ID:
        role = guild.get_role(config.REVIEW_REMOVE_ROLE_ID)
        if role:
            try:
                await member.remove_roles(role, reason=f"審核通過: {reason}")
                logger.info("✅ 已移除 %s 的角色: %s", member.display_name, role.name)
            except discord.Forbidden:
                errors.append(f"無法移除角色 {role.name}（權限不足）")
            except Exception as e:
                errors.append(f"移除角色失敗: {e}")
        else:
            logger.warning("找不到要移除的角色 ID: %d", config.REVIEW_REMOVE_ROLE_ID)

    # 加上新 role
    if config.REVIEW_ADD_ROLE_ID:
        role = guild.get_role(config.REVIEW_ADD_ROLE_ID)
        if role:
            try:
                await member.add_roles(role, reason=f"審核通過: {reason}")
                logger.info("✅ 已加上 %s 的角色: %s", member.display_name, role.name)
            except discord.Forbidden:
                errors.append(f"無法加上角色 {role.name}（權限不足）")
            except Exception as e:
                errors.append(f"加上角色失敗: {e}")
        else:
            logger.warning("找不到要加上的角色 ID: %d", config.REVIEW_ADD_ROLE_ID)

    # 發送通過訊息
    embed = discord.Embed(
        title="✅ 審核通過",
        description=f"{member.mention} 審核通過！冒險團歡迎你！",
        color=0x4CAF50,  # Green color
    )
    embed.add_field(
        name="👋 接下來",
        value="可以先到 [新人報到](https://canary.discord.com/channels/1212105433943117844/1348893805268697129) 和大家打招呼，\n"
              "再到 [新手教學及頻道指南](https://canary.discord.com/channels/1212105433943117844/1324269864201752586) 查看頻道使用方法。",
        inline=False,
    )
    embed.set_footer(text="— 奈奈")
    await channel.send(f"{member.mention}", embed=embed)

    if errors:
        error_msg = "\n".join(f"⚠️ {e}" for e in errors)
        await channel.send(f"（角色設定有部分問題：\n{error_msg}）")

    logger.info("🎉 %s 審核通過 │ 原因: %s", member.display_name, reason)
