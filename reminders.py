"""
奈奈機器人 — 提醒事項

存 SQLite，由 main.py 的排程迴圈定期撈到期的送出。所有時間都用「本機時間」
（伺服器固定在 Asia/Taipei，使用者也都在同一時區），不做 tz 轉換 —— 少一層
轉換就少一種錯法。

重點處理：
  - **錯過的提醒**：機器人掛掉期間到期的，重啟後仍會送出並註明遲到
  - **重複提醒不補發**：每天的提醒漏了三天，不會一次跳三則，直接推進到下一次
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import config

logger = logging.getLogger("nana.remind")

FMT = "%Y-%m-%d %H:%M"
_REPEATS = ("none", "daily", "weekly", "weekdays")


@dataclass
class Reminder:
    id: int
    user_id: int
    channel_id: int
    text: str
    due_at: datetime
    repeat: str
    kind: str = "remind"
    late_minutes: int = 0

    def when_str(self) -> str:
        return self.due_at.strftime("%Y-%m-%d %H:%M")

    def repeat_str(self) -> str:
        return {"daily": "每天", "weekly": "每週", "weekdays": "平日每天"}.get(self.repeat, "")


def next_occurrence(dt: datetime, repeat: str, after: datetime) -> datetime | None:
    """算出 `after` 之後的下一次時間。不重複則回 None。

    刻意用 while 往前推而不是只加一次 —— 漏了好幾天時要直接跳到未來，
    否則重啟後會把積欠的每一次都補送一遍。
    """
    if repeat == "none":
        return None
    nxt = dt
    guard = 0
    while nxt <= after and guard < 4000:
        guard += 1
        if repeat == "daily":
            nxt += timedelta(days=1)
        elif repeat == "weekly":
            nxt += timedelta(weeks=1)
        elif repeat == "weekdays":
            nxt += timedelta(days=1)
            while nxt.weekday() >= 5:      # 5=六 6=日
                nxt += timedelta(days=1)
        else:
            return None
    return nxt if nxt > after else None


class ReminderStore:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    channel_id  INTEGER NOT NULL,
                    user_name   TEXT,
                    text        TEXT NOT NULL,
                    due_at      TEXT NOT NULL,
                    repeat      TEXT DEFAULT 'none',
                    created_at  TEXT,
                    done        INTEGER DEFAULT 0,
                    fired_count INTEGER DEFAULT 0
                )""")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(done, due_at)")
            # kind：remind=一般提醒（照字面念出來）、checkin=主動關心
            #（不念字面，由奈奈依長期記憶臨場想一句關心的話）
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(reminders)")}
            if "kind" not in cols:
                self.conn.execute(
                    "ALTER TABLE reminders ADD COLUMN kind TEXT DEFAULT 'remind'")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    text       TEXT NOT NULL,
                    created_at TEXT,
                    done       INTEGER DEFAULT 0
                )""")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_todo_user ON todos(user_id, done)")
            # 每位使用者的主動關心偏好與節流紀錄
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS prefs (
                    user_id        INTEGER PRIMARY KEY,
                    auto_checkin   INTEGER DEFAULT 1,
                    last_proactive TEXT,
                    last_seen      TEXT
                )""")
            # 活躍時段直方圖：他平常幾點在線。主動關心要挑他會看到的時間，
            # 寫死「白天才發」對凌晨活動的人沒用。
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS activity (
                    user_id INTEGER NOT NULL,
                    hour    INTEGER NOT NULL,
                    count   INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, hour)
                )""")

    # ---- 建立 ----

    def add(self, user_id: int, user_name: str, channel_id: int,
            text: str, due_at: datetime, repeat: str,
            kind: str = "remind") -> tuple[int | None, str]:
        """回傳 (id, 錯誤訊息)。"""
        text = (text or "").strip()
        if not text:
            return None, "沒說要提醒什麼"
        if len(text) > 200:
            text = text[:200]
        if repeat not in _REPEATS:
            repeat = "none"

        now = datetime.now()
        if due_at <= now:
            # 重複型的可以往後推到下一次；一次性的就是設過去了
            nxt = next_occurrence(due_at, repeat, now)
            if nxt is None:
                return None, "那個時間已經過了"
            due_at = nxt
        if due_at > now + timedelta(days=config.REMINDER_MAX_DAYS):
            return None, f"太久以後了（最多 {config.REMINDER_MAX_DAYS} 天內）"

        with self._lock:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE user_id=? AND done=0", (user_id,)
            ).fetchone()[0]
        if n >= config.REMINDER_MAX_PER_USER:
            return None, f"你的提醒太多了（上限 {config.REMINDER_MAX_PER_USER} 個）"

        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO reminders (user_id,user_name,channel_id,text,due_at,repeat,created_at,kind)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (user_id, user_name, channel_id, text, due_at.strftime(FMT), repeat,
                 datetime.now().strftime(FMT), kind),
            )
        return cur.lastrowid, ""

    # ---- 查詢 ----

    def _row_to_reminder(self, r: sqlite3.Row) -> Reminder:
        return Reminder(r["id"], r["user_id"], r["channel_id"], r["text"],
                        datetime.strptime(r["due_at"], FMT), r["repeat"] or "none",
                        (r["kind"] if "kind" in r.keys() else None) or "remind")

    def list_for(self, user_id: int) -> list[Reminder]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE user_id=? AND done=0 ORDER BY due_at",
                (user_id,),
            ).fetchall()
        return [self._row_to_reminder(r) for r in rows]

    def find(self, user_id: int, keyword: str) -> list[Reminder]:
        kw = (keyword or "").strip()
        if not kw:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE user_id=? AND done=0 AND text LIKE ?"
                " ORDER BY due_at",
                (user_id, f"%{kw}%"),
            ).fetchall()
        return [self._row_to_reminder(r) for r in rows]

    def cancel(self, user_id: int, rid: int) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE reminders SET done=1 WHERE id=? AND user_id=? AND done=0",
                (rid, user_id),
            )
        return cur.rowcount > 0

    # ---- 待辦（沒有時間的事，跟提醒分開）----

    def todo_add(self, user_id: int, text: str) -> tuple[int | None, str]:
        text = (text or "").strip()[:200]
        if not text:
            return None, "沒說要記什麼"
        with self._lock:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE user_id=? AND done=0", (user_id,)
            ).fetchone()[0]
        if n >= config.TODO_MAX_PER_USER:
            return None, f"待辦太多了（上限 {config.TODO_MAX_PER_USER} 個）"
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO todos (user_id,text,created_at) VALUES (?,?,?)",
                (user_id, text, datetime.now().strftime(FMT)))
        return cur.lastrowid, ""

    def todo_list(self, user_id: int) -> list[tuple[int, str]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,text FROM todos WHERE user_id=? AND done=0 ORDER BY id",
                (user_id,)).fetchall()
        return [(r["id"], r["text"]) for r in rows]

    def todo_find(self, user_id: int, keyword: str) -> list[tuple[int, str]]:
        kw = (keyword or "").strip()
        if not kw:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,text FROM todos WHERE user_id=? AND done=0 AND text LIKE ?"
                " ORDER BY id", (user_id, f"%{kw}%")).fetchall()
        return [(r["id"], r["text"]) for r in rows]

    def todo_done(self, user_id: int, tid: int) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE todos SET done=1 WHERE id=? AND user_id=? AND done=0",
                (tid, user_id))
        return cur.rowcount > 0

    # ---- 活躍時段 / 主動關心的治理資料 ----

    def note_activity(self, user_id: int) -> None:
        """記一次活動（訊息時間的小時），同時更新 last_seen。"""
        now = datetime.now()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO activity (user_id,hour,count) VALUES (?,?,1)"
                " ON CONFLICT(user_id,hour) DO UPDATE SET count=count+1",
                (user_id, now.hour))
            self.conn.execute(
                "INSERT INTO prefs (user_id,last_seen) VALUES (?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
                (user_id, now.strftime(FMT)))

    def active_hours(self, user_id: int) -> list[int]:
        """他最常出現的小時（由多到少）。資料太少就回空。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT hour, count FROM activity WHERE user_id=? ORDER BY count DESC",
                (user_id,)).fetchall()
        total = sum(r["count"] for r in rows)
        if total < config.AUTO_CHECKIN_MIN_SAMPLES:
            return []
        # 只留累積覆蓋到 80% 訊息量的那些小時，避免把偶爾一次的離群時段也算進去
        keep, acc = [], 0
        for r in rows:
            keep.append(r["hour"])
            acc += r["count"]
            if acc >= total * 0.8:
                break
        return keep

    def next_active_time(self, user_id: int, not_before: datetime) -> datetime:
        """找出 not_before 之後、落在他活躍時段裡的最近時間點。

        沒有足夠活躍資料時退回 AUTO_CHECKIN_FALLBACK_HOURS。
        """
        hours = self.active_hours(user_id) or list(config.AUTO_CHECKIN_FALLBACK_HOURS)
        hourset = set(hours)
        t = not_before.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        for _ in range(24 * 8):        # 最多往後找 8 天
            if t.hour in hourset:
                # 在該小時內隨機挑分鐘，避免每次都整點、看起來像機器
                return t.replace(minute=(user_id + t.day * 7) % 60)
            t += timedelta(hours=1)
        return not_before + timedelta(days=1)

    def get_prefs(self, user_id: int) -> tuple[bool, datetime | None, datetime | None]:
        """回傳 (要不要自動關心, 上次主動關心時間, 最後出現時間)。"""
        with self._lock:
            r = self.conn.execute(
                "SELECT auto_checkin,last_proactive,last_seen FROM prefs WHERE user_id=?",
                (user_id,)).fetchone()
        if r is None:
            return True, None, None

        def _p(v):
            try:
                return datetime.strptime(v, FMT) if v else None
            except ValueError:
                return None
        enabled = bool(r["auto_checkin"]) if r["auto_checkin"] is not None else True
        return enabled, _p(r["last_proactive"]), _p(r["last_seen"])

    def set_auto_checkin(self, user_id: int, enabled: bool) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO prefs (user_id,auto_checkin) VALUES (?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET auto_checkin=excluded.auto_checkin",
                (user_id, 1 if enabled else 0))

    def mark_proactive(self, user_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO prefs (user_id,last_proactive) VALUES (?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET last_proactive=excluded.last_proactive",
                (user_id, datetime.now().strftime(FMT)))

    def has_pending_auto_checkin(self, user_id: int) -> bool:
        with self._lock:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE user_id=? AND done=0 AND kind=?",
                (user_id, "auto_checkin")).fetchone()[0]
        return n > 0

    # ---- 排程 ----

    def due_now(self) -> list[Reminder]:
        """撈出所有已到期的（含機器人離線期間錯過的）。"""
        now = datetime.now()
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE done=0 AND due_at<=? ORDER BY due_at",
                (now.strftime(FMT),),
            ).fetchall()

        out: list[Reminder] = []
        for r in rows:
            rem = self._row_to_reminder(r)
            rem.late_minutes = max(0, int((now - rem.due_at).total_seconds() // 60))
            out.append(rem)
        return out

    def mark_fired(self, rem: Reminder) -> None:
        """送出後推進到下一次；不重複的就結案。"""
        now = datetime.now()
        nxt = next_occurrence(rem.due_at, rem.repeat, now)
        with self._lock, self.conn:
            if nxt is None:
                self.conn.execute(
                    "UPDATE reminders SET done=1, fired_count=fired_count+1 WHERE id=?",
                    (rem.id,))
            else:
                self.conn.execute(
                    "UPDATE reminders SET due_at=?, fired_count=fired_count+1 WHERE id=?",
                    (nxt.strftime(FMT), rem.id))


_store: ReminderStore | None = None


def store() -> ReminderStore:
    global _store
    if _store is None:
        _store = ReminderStore(config.REMINDER_DB_PATH)
    return _store


# ── async 包裝（SQLite 同步，丟到 thread）────────────────

async def add(user_id: int, user_name: str, channel_id: int,
              text: str, due_at: datetime, repeat: str,
              kind: str = "remind") -> tuple[int | None, str]:
    return await asyncio.to_thread(
        store().add, user_id, user_name, channel_id, text, due_at, repeat, kind)


async def list_for(user_id: int) -> list[Reminder]:
    return await asyncio.to_thread(store().list_for, user_id)


async def find(user_id: int, keyword: str) -> list[Reminder]:
    return await asyncio.to_thread(store().find, user_id, keyword)


async def cancel(user_id: int, rid: int) -> bool:
    return await asyncio.to_thread(store().cancel, user_id, rid)


async def todo_add(user_id: int, text: str) -> tuple[int | None, str]:
    return await asyncio.to_thread(store().todo_add, user_id, text)


async def todo_list(user_id: int) -> list[tuple[int, str]]:
    return await asyncio.to_thread(store().todo_list, user_id)


async def todo_find(user_id: int, keyword: str) -> list[tuple[int, str]]:
    return await asyncio.to_thread(store().todo_find, user_id, keyword)


async def todo_done(user_id: int, tid: int) -> bool:
    return await asyncio.to_thread(store().todo_done, user_id, tid)


async def note_activity(user_id: int) -> None:
    await asyncio.to_thread(store().note_activity, user_id)


async def active_hours(user_id: int) -> list[int]:
    return await asyncio.to_thread(store().active_hours, user_id)


async def next_active_time(user_id: int, not_before: datetime) -> datetime:
    return await asyncio.to_thread(store().next_active_time, user_id, not_before)


async def get_prefs(user_id: int):
    return await asyncio.to_thread(store().get_prefs, user_id)


async def set_auto_checkin(user_id: int, enabled: bool) -> None:
    await asyncio.to_thread(store().set_auto_checkin, user_id, enabled)


async def mark_proactive(user_id: int) -> None:
    await asyncio.to_thread(store().mark_proactive, user_id)


async def has_pending_auto_checkin(user_id: int) -> bool:
    return await asyncio.to_thread(store().has_pending_auto_checkin, user_id)


async def due_now() -> list[Reminder]:
    return await asyncio.to_thread(store().due_now)


async def mark_fired(rem: Reminder) -> None:
    await asyncio.to_thread(store().mark_fired, rem)
