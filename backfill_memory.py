#!/usr/bin/env python
"""從歷史 log 回填長期記憶。

長期記憶 2026-08-02 上線，但 DB 檔一開始被 root 佔住（測試時用錯身分建立），
vito 只能讀不能寫，所以到 08-04 修好權限之前實際上一筆都沒存進去。log 裡
有大量歷史對話，直接抽取回填就不用從零開始。

兩個資料來源：
  1. 「💬 對話」行 —— 有 user_id，是跟奈奈的直接對話（主要來源）
  2. 「🔍 情緒偵測」行 —— 只有暱稱，用來源 1 建立的暱稱→id 對照回推；
     只取 needs_support=True 的（那些才含真實處境，中性閒聊沒價值）

注意 log 把訊息截斷在 80 字，所以抽出來的是概略事實而非完整脈絡 —— 這正是
長期記憶想要的粒度。

用法：  venv/bin/python backfill_memory.py            # 預覽（不呼叫 LLM）
        venv/bin/python backfill_memory.py --dry-run  # 呼叫 LLM 但不寫入
        venv/bin/python backfill_memory.py --apply    # 實際寫入
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/home/vito/nana-bot")
import config      # noqa: E402
import llm_client  # noqa: E402
import memory      # noqa: E402

LOG = "/home/vito/nana-bot/logs/nana-bot.log"

RE_CHAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2}.*?💬 對話 │ (.+?) \((\d+)\): (.*)$")
RE_EMO = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2}.*?🔍 情緒偵測 │ (.+?): "
    r"(\S+) \(強度 (\d+), 支持: (\w+), 危險: \w+\) │ (.*)$")

# 一次抽取塞幾則訊息。太少會浪費呼叫次數，太多會讓模型漏掉細節。
BATCH = 8
# 每人最多回填幾則訊息（取最近的）—— 舊的事實價值低，而且去重會收斂
MAX_PER_USER = 60

APPLY = "--apply" in sys.argv
DRY_LLM = "--dry-run" in sys.argv


def collect() -> tuple[dict[int, list[tuple[str, str]]], dict[int, str]]:
    """回傳 {user_id: [(時間, 訊息)]} 與 {user_id: 暱稱}。"""
    msgs: dict[int, list[tuple[str, str]]] = defaultdict(list)
    names: dict[int, str] = {}
    name_to_id: dict[str, int] = {}

    raw = open(LOG, encoding="utf-8", errors="replace").read().splitlines()

    # 第一輪：對話行（建立 暱稱 → id 對照）
    for line in raw:
        m = RE_CHAT.match(line)
        if not m:
            continue
        stamp, name, uid, text = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        names[uid] = name
        name_to_id[name] = uid
        text = text.strip()
        if len(text) >= config.MEMORY_MIN_CHARS:
            msgs[uid].append((stamp, text))

    # 第二輪：情緒偵測行，只取需要支持的（含真實處境）
    for line in raw:
        m = RE_EMO.match(line)
        if not m:
            continue
        stamp, name, _emo, _inten, support, text = m.groups()
        if support != "True":
            continue
        uid = name_to_id.get(name)
        if uid is None:
            continue          # 沒對映到 id 的暱稱只能放棄
        text = text.strip()
        if len(text) >= config.MEMORY_MIN_CHARS:
            msgs[uid].append((stamp, text))

    # 依時間排序、去重、只留最近 MAX_PER_USER 則
    for uid, items in msgs.items():
        seen, uniq = set(), []
        for stamp, text in sorted(items):
            if text in seen:
                continue
            seen.add(text)
            uniq.append((stamp, text))
        msgs[uid] = uniq[-MAX_PER_USER:]

    return msgs, names


async def main() -> None:
    msgs, names = collect()
    msgs = {u: m for u, m in msgs.items() if m}
    total = sum(len(m) for m in msgs.values())
    calls = sum((len(m) + BATCH - 1) // BATCH for m in msgs.values())

    print(f"可回填 {len(msgs)} 位使用者、{total} 則訊息")
    print(f"預估 LLM 呼叫 {calls} 次（每次 {BATCH} 則）\n")
    for uid, m in sorted(msgs.items(), key=lambda kv: -len(kv[1])):
        print(f"  {names.get(uid, uid)[:28]:<30} {len(m):>3} 則  "
              f"{m[0][0][:10]} ~ {m[-1][0][:10]}")

    if not (APPLY or DRY_LLM):
        print("\n（僅預覽。--dry-run 會呼叫 LLM 但不寫入，--apply 才實際寫入）")
        return

    print(f"\n開始抽取{'（不寫入）' if DRY_LLM else ''} …\n")
    grand = 0
    for uid, items in sorted(msgs.items(), key=lambda kv: -len(kv[1])):
        name = names.get(uid, str(uid))
        found: list[dict] = []
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            blob = "\n".join(f"[{name}] 說：{t}" for _s, t in chunk)
            try:
                got = await llm_client.extract_memories(blob)
            except Exception as e:  # noqa: BLE001
                print(f"    ⚠️ 抽取失敗：{e}")
                continue
            found.extend(got)

        if not found:
            print(f"  {name[:28]:<30} 沒抽到值得記的事")
            continue

        if DRY_LLM:
            print(f"  {name[:28]:<30} 抽到 {len(found)} 則")
            for it in found:
                print(f"      ({it.get('kind','?')}) {it.get('content','')[:60]}")
        else:
            n = await memory.remember(uid, name, found)
            grand += n
            print(f"  {name[:28]:<30} 寫入 {n} 則（抽到 {len(found)}，去重後）")
            for it in found[:3]:
                print(f"      ({it.get('kind','?')}) {it.get('content','')[:60]}")

    if not DRY_LLM:
        print(f"\n✅ 共寫入 {grand} 則記憶")


asyncio.run(main())
