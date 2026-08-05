#!/usr/bin/env python
"""從歷史 log 回填活躍時段統計。

activity 表是 2026-08-04 才上線的，空的表示要等好幾週才有足夠樣本來判斷
每個人的活躍時段。但 log 裡的「💬 對話」行本來就帶了時間與 user_id，
直接回填就能讓主動關心立刻挑對時間。

只回填有直接對話的人 —— 那正是「奈奈認識的人」，也正是會被主動關心的族群。
情緒偵測那些行只有暱稱、沒有 user_id，無法回填。

用法：  venv/bin/python backfill_activity.py [--apply]
        不加 --apply 只預覽，不寫入。
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/home/vito/nana-bot")
import config  # noqa: E402
import reminders  # noqa: E402

LOG = "/home/vito/nana-bot/logs/nana-bot.log"
LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}):\d{2}:\d{2}.*?💬 對話 │ (.+?) \((\d+)\):"
)

APPLY = "--apply" in sys.argv

hours: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
names: dict[int, str] = {}
seen_last: dict[int, datetime] = {}
total = 0

with open(LOG, encoding="utf-8", errors="replace") as f:
    for line in f:
        m = LINE.match(line)
        if not m:
            continue
        stamp, name, uid = m.group(1), m.group(2), int(m.group(3))
        dt = datetime.strptime(stamp, "%Y-%m-%d %H")
        hours[uid][dt.hour] += 1
        names[uid] = name
        seen_last[uid] = max(seen_last.get(uid, dt), dt)
        total += 1

print(f"從 log 解析出 {total} 筆對話、{len(hours)} 位使用者\n")

rows = sorted(hours.items(), key=lambda kv: -sum(kv[1].values()))
for uid, hist in rows:
    n = sum(hist.values())
    top = sorted(hist.items(), key=lambda kv: -kv[1])
    # 跟 reminders.active_hours 一樣的 80% 覆蓋邏輯
    keep, acc = [], 0
    for h, c in top:
        keep.append(h)
        acc += c
        if acc >= n * 0.8:
            break
    ok = n >= config.AUTO_CHECKIN_MIN_SAMPLES
    mark = "✅" if ok else f"⚠️ 樣本不足（<{config.AUTO_CHECKIN_MIN_SAMPLES}）"
    dist = " ".join(f"{h:02d}點×{c}" for h, c in top[:6])
    print(f"{mark} {names[uid]}  ({uid})")
    print(f"    共 {n} 筆 │ {dist}")
    print(f"    活躍時段 → {sorted(keep)}  最後出現 {seen_last[uid]:%Y-%m-%d %H:00}")

if not APPLY:
    print("\n（預覽模式，未寫入。加上 --apply 才會實際回填）")
    sys.exit(0)

store = reminders.store()
with store._lock, store.conn:
    for uid, hist in hours.items():
        for h, c in hist.items():
            store.conn.execute(
                "INSERT INTO activity (user_id,hour,count) VALUES (?,?,?)"
                " ON CONFLICT(user_id,hour) DO UPDATE SET count=count+excluded.count",
                (uid, h, c))
        store.conn.execute(
            "INSERT INTO prefs (user_id,last_seen) VALUES (?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
            (uid, seen_last[uid].strftime(reminders.FMT)))
print(f"\n✅ 已回填 {len(hours)} 位使用者的活躍時段")
