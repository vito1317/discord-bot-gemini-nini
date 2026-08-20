"""
奈奈的累計統計 📊

用途只有一個：把「她到目前為止做了多少事」算出來，寫進 Discord 的機器人簡介
（見 main.py 的 update_profile_stats）。

## 為什麼要另外一支
大部分數字本來就在資料庫裡（記住幾件事、認識幾個人、講過幾句話），直接算就好，
不需要另外記一份 —— 多記一份就會有兩個數字不一樣的那天。

但有四件事沒有任何地方留下痕跡：幫人上網幾趟、通報幾次危險訊息、貼了幾個表情、
主動關心過幾次。這些只有「發生的那一刻」知道，所以需要一個很小的累加器。

## 設計
單純到不會壞：一張 counters(name, n) 表，bump() 就 +1。
  • 失敗一律吞掉 —— 統計數字掉一筆沒關係，但絕對不能因為它讓她回不了訊息
  • 用自己的 db 檔，不跟記憶／提醒混在一起（那兩個是使用者資料，這個是可丟的）
  • 重啟後累積值還在（不然簡介上的數字每次重開就歸零，等於沒意義）
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

logger = logging.getLogger("nana.stats")

DB_PATH = os.getenv("NANA_STATS_DB", "nana_stats.db")

# 會被計數的事件。列在這裡是為了讓「簡介上那些數字是哪來的」一眼看得完。
BROWSE = "browse_tasks"          # 幫人上網／操作網站幾趟
ALERTS = "danger_alerts"         # 通報過幾次危險訊息
REACTIONS = "reactions"          # 主動貼了幾個表情符號
CARES = "proactive_cares"        # 主動關心過幾次


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.execute("CREATE TABLE IF NOT EXISTS counters ("
              "name TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0, "
              "updated_at REAL)")
    return c


def bump(name: str, by: int = 1) -> None:
    """某件事發生了。

    刻意是同步的：sqlite 的一次 upsert 是微秒級，包成 async 只是多一層 await
    卻仍然在同一個 event loop 上。呼叫點都在訊息處理流程裡，所以**絕不能拋例外**。
    """
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO counters (name, n, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET n = n + ?, updated_at = ?",
                (name, by, time.time(), by, time.time()))
    except Exception as e:  # noqa: BLE001
        logger.debug("統計 +1 失敗（忽略）：%s", e)


def get(name: str) -> int:
    try:
        with _conn() as c:
            row = c.execute("SELECT n FROM counters WHERE name = ?", (name,)).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        logger.debug("讀統計失敗（忽略）：%s", e)
        return 0


def collect() -> dict[str, int]:
    """簡介上要用的所有數字。

    資料庫讀不到就當 0 —— 統計是裝飾，不該讓它有機會弄掉簡介更新。
    """
    import config

    out = {
        "browse": get(BROWSE),
        "alerts": get(ALERTS),
        "reactions": get(REACTIONS),
        "cares": get(CARES),
        "messages": 0,
        "people": 0,
        "memories": 0,
        "reminders": 0,
    }

    # 講過幾句話、認識幾個人 —— reminders.py 那邊本來就在記（活躍時段統計用）
    try:
        with sqlite3.connect(getattr(config, "REMINDER_DB_PATH", "nana_reminders.db"),
                             timeout=5) as c:
            out["messages"] = c.execute(
                "SELECT COALESCE(SUM(count), 0) FROM activity").fetchone()[0]
            out["people"] = c.execute("SELECT COUNT(*) FROM prefs").fetchone()[0]
            out["reminders"] = c.execute(
                "SELECT COUNT(*) FROM reminders").fetchone()[0]
    except Exception as e:  # noqa: BLE001
        logger.debug("讀提醒庫統計失敗（忽略）：%s", e)

    # 記住幾件事
    try:
        with sqlite3.connect(getattr(config, "MEMORY_DB_PATH", "nana_memory.db"),
                             timeout=5) as c:
            out["memories"] = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except Exception as e:  # noqa: BLE001
        logger.debug("讀記憶庫統計失敗（忽略）：%s", e)

    return out
