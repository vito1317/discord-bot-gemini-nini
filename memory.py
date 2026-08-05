"""
奈奈機器人 — 長期記憶

沿用 PAI ReflectiveMemory 的做法（SQLite + JSON 向量 + cosine 檢索），但針對
「陪伴型機器人記住使用者」調整，並修掉原版的一個地雷：

  PAI 的 HashingEmbedder 用 Python 內建 hash()，那個對字串是「每個 process
  隨機化」的（PYTHONHASHSEED）。向量寫進 DB 後，下次重啟算出來的 query 向量
  跟舊資料完全對不起來 → 檢索永遠失效。這裡改用 blake2b，跨 process 穩定。

embedding 來源可插拔：若哪天 llama-server 加上 --embeddings，把
config.EMBEDDING_BASE_URL 設起來就會自動改走語義向量。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

import config

logger = logging.getLogger("nana.memory")

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── embedding ───────────────────────────────────────────

class StableHashEmbedder:
    """零依賴詞袋雜湊向量。用 blake2b 取代內建 hash()，跨 process 穩定。"""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    @staticmethod
    def _tokens(text: str) -> list[str]:
        # 英數詞 + CJK 單字（中文沒空格）；再補 CJK bigram 抓住詞組
        toks = _TOKEN_RE.findall(text.lower())
        cjk = [t for t in toks if len(t) == 1 and "一" <= t <= "鿿"]
        toks += [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
        return toks

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in self._tokens(text):
            vec[self._bucket(tok)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class RemoteEmbedder:
    """OpenAI 相容 /v1/embeddings；失敗自動退回雜湊向量。"""

    def __init__(self, base_url: str, model: str, fallback: StableHashEmbedder) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback = fallback
        self._client = httpx.Client(timeout=30.0)
        self._dead = False

    def embed(self, text: str) -> list[float]:
        if self._dead:
            return self.fallback.embed(text)
        try:
            r = self._client.post(
                f"{self.base_url}/embeddings",
                json={"input": text, "model": self.model},
            )
            r.raise_for_status()
            v = r.json()["data"][0]["embedding"]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            return [x / norm for x in v]
        except Exception as e:  # noqa: BLE001
            logger.warning("embedding 服務不可用（改用本地雜湊向量）：%s", e)
            self._dead = True   # 只抱怨一次，之後直接走 fallback
            return self.fallback.embed(text)


def _make_embedder():
    fallback = StableHashEmbedder(config.MEMORY_VECTOR_DIM)
    if config.EMBEDDING_BASE_URL:
        return RemoteEmbedder(config.EMBEDDING_BASE_URL, config.EMBEDDING_MODEL, fallback)
    return fallback


def _cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))   # 兩邊都已正規化


# ── 記憶儲存 ────────────────────────────────────────────

@dataclass
class Memory:
    id: int
    kind: str
    content: str
    updated_at: str
    similarity: float = 0.0


_KIND_LABEL = {
    "profile": "基本資料",
    "preference": "喜好",
    "event": "生活事件",
    "concern": "在意的事",
    "relationship": "人際",
}


class MemoryStore:
    """每位使用者的長期記憶（SQLite）。所有方法都是同步的，
    對外由 module-level 的 async 包裝丟到 thread 執行，避免卡住 event loop。"""

    def __init__(self, db_path: str) -> None:
        self.embedder = _make_embedder()
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    user_name   TEXT,
                    kind        TEXT,
                    content     TEXT NOT NULL,
                    created_at  TEXT,
                    updated_at  TEXT,
                    hits        INTEGER DEFAULT 0,
                    embedding   TEXT
                )""")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id)")

    # ---- 寫入 ----

    def add(self, user_id: int, user_name: str, kind: str, content: str) -> str:
        """新增一則記憶。跟既有記憶太像就改成更新，避免同一件事塞爆。

        回傳 'added' / 'updated' / 'skipped'。
        """
        content = content.strip()
        if not content:
            return "skipped"

        emb = self.embedder.embed(content)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, content, embedding FROM memories WHERE user_id=?",
                (user_id,),
            ).fetchall()

        for row in rows:
            if _cosine(emb, json.loads(row["embedding"])) >= config.MEMORY_DEDUP_SIM:
                with self._lock, self.conn:
                    self.conn.execute(
                        "UPDATE memories SET content=?, kind=?, updated_at=?, embedding=? WHERE id=?",
                        (content, kind, _now(), json.dumps(emb), row["id"]),
                    )
                return "updated"

        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO memories (user_id,user_name,kind,content,created_at,updated_at,hits,embedding)"
                " VALUES (?,?,?,?,?,?,0,?)",
                (user_id, user_name, kind, content, _now(), _now(), json.dumps(emb)),
            )
        self._prune(user_id)
        return "added"

    def _prune(self, user_id: int) -> None:
        """每位使用者只留最多 N 則；先砍最少被用到、最久沒更新的。

        MEMORY_MAX_PER_USER <= 0 表示不限量，必須直接 return ——
        否則下面的 `COUNT(*) - 0` 會等於全部筆數，把該使用者的記憶整個刪光。
        """
        if config.MEMORY_MAX_PER_USER <= 0:
            return
        with self._lock, self.conn:
            self.conn.execute(
                """DELETE FROM memories WHERE id IN (
                       SELECT id FROM memories WHERE user_id=?
                       ORDER BY hits ASC, updated_at ASC
                       LIMIT MAX(0, (SELECT COUNT(*) FROM memories WHERE user_id=?) - ?)
                   )""",
                (user_id, user_id, config.MEMORY_MAX_PER_USER),
            )

    # ---- 檢索 ----

    def retrieve(self, user_id: int, query: str) -> list[Memory]:
        """撈出這一輪該讓奈奈記得的事。

        三個來源聯集，順序就是重要性：

          1. 相似度命中 —— 話題直接對上時最精準
          2. 基本資料（profile）—— 名字、職業這種跟當下話題不像但每次都該記得
          3. **最近更新的（任何 kind）** —— 這條最關鍵

        為什麼需要第 3 條：llama-server 沒開 --embeddings，實際用的是詞袋雜湊
        向量，那種向量沒有語義。問「你知道我最近在忙什麼嗎」跟記憶
        「他正在研究 TACT 演算法」字面零重疊，相似度只有 0.075，永遠撈不到 ——
        於是奈奈明明記著卻答不出來。每人記憶量本來就只有十幾則，直接把最近的
        帶上遠比靠字面相似度可靠。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,kind,content,updated_at,embedding FROM memories WHERE user_id=?",
                (user_id,),
            ).fetchall()
        if not rows:
            return []

        picked: dict[int, Memory] = {}

        # 1) 相似度命中
        qv = self.embedder.embed(query)
        scored: list[tuple[float, Memory]] = []
        for r in rows:
            sim = _cosine(qv, json.loads(r["embedding"]))
            if sim >= config.MEMORY_MIN_SIM:
                scored.append((sim, Memory(r["id"], r["kind"] or "", r["content"],
                                           r["updated_at"] or "", round(sim, 3))))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _s, m in scored[:config.MEMORY_TOP_K]:
            picked[m.id] = m

        def _add(sql: str, limit: int) -> None:
            with self._lock:
                got = self.conn.execute(sql, (user_id, limit)).fetchall()
            for r in got:
                picked.setdefault(
                    r["id"],
                    Memory(r["id"], r["kind"] or "", r["content"], r["updated_at"] or ""),
                )

        # 2) 基本資料恆帶
        if config.MEMORY_ALWAYS_PROFILE:
            _add("SELECT id,kind,content,updated_at FROM memories"
                 " WHERE user_id=? AND kind='profile'"
                 " ORDER BY updated_at DESC LIMIT ?", config.MEMORY_PROFILE_K)

        # 3) 最近更新的（不分 kind）
        if config.MEMORY_RECENT_K:
            _add("SELECT id,kind,content,updated_at FROM memories"
                 " WHERE user_id=? ORDER BY updated_at DESC, id DESC LIMIT ?",
                 config.MEMORY_RECENT_K)

        hits = list(picked.values())
        if hits:
            with self._lock, self.conn:
                self.conn.executemany(
                    "UPDATE memories SET hits=hits+1 WHERE id=?",
                    [(m.id,) for m in hits],
                )
        return hits

    # ---- 管理 ----

    def list_all(self, user_id: int) -> list[Memory]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,kind,content,updated_at FROM memories WHERE user_id=?"
                " ORDER BY kind, updated_at DESC",
                (user_id,),
            ).fetchall()
        return [Memory(r["id"], r["kind"] or "", r["content"], r["updated_at"] or "") for r in rows]

    def forget_all(self, user_id: int) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM memories WHERE user_id=?", (user_id,))
        return cur.rowcount

    def count(self, user_id: int) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id=?", (user_id,)
            ).fetchone()[0]


_store: MemoryStore | None = None


def store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore(config.MEMORY_DB_PATH)
    return _store


# ── async 包裝（SQLite 是同步的，丟到 thread 跑）────────

async def remember(user_id: int, user_name: str, items: list[dict]) -> int:
    """批次寫入。回傳實際新增/更新的筆數。"""
    def _work() -> int:
        s = store()
        n = 0
        for it in items:
            kind = str(it.get("kind", "") or "event").strip()
            content = str(it.get("content", "") or "").strip()
            if not content:
                continue
            if s.add(user_id, user_name, kind, content) in ("added", "updated"):
                n += 1
        return n
    return await asyncio.to_thread(_work)


async def recall(user_id: int, query: str) -> list[Memory]:
    return await asyncio.to_thread(lambda: store().retrieve(user_id, query))


async def list_memories(user_id: int) -> list[Memory]:
    return await asyncio.to_thread(lambda: store().list_all(user_id))


async def forget(user_id: int) -> int:
    return await asyncio.to_thread(lambda: store().forget_all(user_id))


def format_for_prompt(memories: list[Memory]) -> str:
    """把記憶整理成注入 system prompt 的區塊。"""
    if not memories:
        return ""
    lines = []
    for m in memories:
        label = _KIND_LABEL.get(m.kind, "其他")
        lines.append(f"- （{label}）{m.content}")
    return (
        "\n\n## 你記得關於這位對象的事\n"
        + "\n".join(lines)
        + "\n（這些是你們以前聊過留下的印象，**就是你確實知道的事**。"
        "他問到相關的事（例如「我最近在忙什麼」「我是誰」）時要用這些回答，"
        "不可以說不知道。但自然地講就好，不要一開口就條列出來，"
        "也不要說「根據我的記憶」這種話。如果跟他現在說的對不上，以他現在說的為準。）"
    )
