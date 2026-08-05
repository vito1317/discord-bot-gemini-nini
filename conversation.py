"""
奈奈機器人 — 對話管理器
管理每位使用者在每個頻道的獨立對話歷史紀錄與冷卻機制
（不同頻道的上下文完全分離）
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

import config


@dataclass
class UserSession:
    """單一使用者在某頻道的對話 session"""
    history: list[dict] = field(default_factory=list)
    last_auto_reply: float = 0.0  # 上次自動回覆的時間戳
    last_interaction: float = 0.0  # 上次互動的時間戳

    # 對話歷史最多保留的輪數
    MAX_HISTORY: int = 20

    def add_message(self, role: str, content: str) -> None:
        """加入一則訊息到對話歷史"""
        self.history.append({"role": role, "content": content})
        self.last_interaction = time.time()

        # 保留最近 N 則，避免 context 過長
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]

    def can_auto_reply(self) -> bool:
        """檢查是否已過冷卻時間（針對自動偵測回覆）"""
        return (time.time() - self.last_auto_reply) >= config.COOLDOWN_SECONDS

    def mark_auto_replied(self) -> None:
        """標記已自動回覆"""
        self.last_auto_reply = time.time()

    def is_stale(self, timeout: float = 1800.0) -> bool:
        """檢查 session 是否過期（預設 30 分鐘無互動）"""
        if self.last_interaction == 0:
            return False
        return (time.time() - self.last_interaction) > timeout

    def clear(self) -> None:
        """清除對話歷史"""
        self.history.clear()


class ConversationManager:
    """
    管理所有使用者的對話 session。
    使用 (user_id, channel_id) 作為 key，不同頻道的對話完全分離。
    私訊使用 channel_id=0。
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int], UserSession] = defaultdict(UserSession)

    def _key(self, user_id: int, channel_id: int = 0) -> tuple[int, int]:
        return (user_id, channel_id)

    def get_session(self, user_id: int, channel_id: int = 0) -> UserSession:
        """取得或建立使用者在特定頻道的 session"""
        key = self._key(user_id, channel_id)
        session = self._sessions[key]

        # 如果 session 過期，自動清除歷史
        if session.is_stale():
            session.clear()

        return session

    def clear_session(self, user_id: int, channel_id: int = 0) -> None:
        """清除指定使用者在特定頻道的 session"""
        key = self._key(user_id, channel_id)
        if key in self._sessions:
            self._sessions[key].clear()

    def clear_all_user_sessions(self, user_id: int) -> int:
        """清除指定使用者在所有頻道的 session，回傳清除數量"""
        keys = [k for k in self._sessions if k[0] == user_id]
        for k in keys:
            self._sessions[k].clear()
        return len(keys)

    def cleanup_stale(self) -> int:
        """清除所有過期的 session，回傳清除數量"""
        stale_keys = [
            key for key, s in self._sessions.items()
            if s.is_stale(timeout=3600.0)  # 1 小時後完全移除
        ]
        for key in stale_keys:
            del self._sessions[key]
        return len(stale_keys)
