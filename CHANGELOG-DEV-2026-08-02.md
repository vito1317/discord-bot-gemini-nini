# nana-bot — 開發日誌 2026-08-02

修復 3 個既有 bug、新增 4 項功能，並在共用的語音服務中找出 3 個導致「語音完全不可用」的缺陷。
另外在兩份第三方／共用程式碼中發現 2 個資料級 bug（已記錄，未修）。

**環境**：`/home/vito/nana-bot`（Python 3.13, venv）、supervisor 管理、後端 llama-server `:10003`（Gemma 4 + mmproj）、語音服務 `:8891`（MiniCPM-o 4.5）

---

## 🚨 P0 — 服務中斷 10 天

### `supervisor.service` 開機後 crash-loop 17,929 次

**症狀**：奈奈自 2026-07-22 15:03 起離線，10 天無人察覺。

**根因鏈**
1. 主機於 07-22 15:04 重開機（`uptime` 10 天，與 log 最後一筆 `Closing the event loop` 吻合 → graceful SIGINT，非 crash）
2. `/var/log/supervisor/` 目錄不存在（`/var/log` 為 ext4 持久化，非 tmpfs；`tmpfiles.d` 亦無重建規則）
3. `supervisord` 啟動即 `status=2/INVALIDARGUMENT`：
   ```
   Error: The directory named as part of the path /var/log/supervisor/supervisord.log does not exist
   ```
4. systemd 無限重啟 → `nana-bot.conf` 的 `autostart=true` 從未生效

> 當時系統上唯一存活的 `supervisord` 位於 docker container（mnt namespace `4026533404`），與 host 無關，容易誤判為「supervisor 正常」。

**修復**
- `mkdir -p /var/log/supervisor`（systemd 的 auto-restart 迴圈當場自行恢復）
- 新增 drop-in 防復發：

```ini
# /etc/systemd/system/supervisor.service.d/logdir.conf
[Service]
LogsDirectory=supervisor
LogsDirectoryMode=0755
```

**已知限制**：drop-in 已載入（`systemctl status` 可見），但因 `supervisord.log` 已佔用該目錄，無法以刪除目錄的方式實測重建行為，需待下次重開機驗證。

**連帶發現**（未修）：`llama-proxy.service` 為 `enabled` 但 `inactive`，因其宣告 `Requires=llama-server.service`，而該 unit 檔已被改名為 `llama-server.service.disabled`（Qwen3.5-35B）→ 依賴無法解析。

---

## 🔧 後端切換

`.env` 的聊天後端由已停用的 Qwen3.5（`:10001`，經 llama-proxy）改指 Gemma 4：

```diff
-LM_STUDIO_BASE_URL=http://127.0.0.1:10001/v1
-LM_STUDIO_MODEL=Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf
+LM_STUDIO_BASE_URL=http://127.0.0.1:10003/v1
+LM_STUDIO_MODEL=gemma-4-26B-A4B-it-qat-q4_0.gguf
```

原檔備份於 `.env.bak.20260802`。聊天與情緒偵測／審核現共用同一顆 Gemma，**人格語氣與原本的 Qwen3.5 不同**。

`llama-server` 啟動參數為 `--ctx-size 131072 --parallel 4` → **每個 slot 實際可用 context 僅 32k**，此數字是後續所有注入長度上限的依據。

---

## ✨ 新功能

### 1. 附件讀取 — `attachments.py`（新檔）

支援文字檔、圖片、PDF。**僅在奈奈被搭話時讀取**，不會主動翻頻道內其他人的檔案。

**圖片**：轉 base64 data URI，走 Gemma 4 的 vision。事前以手工產生的 PNG 驗證端點確實接受 OpenAI 格式 `image_url`（回傳「紅色」）。

**PDF**（兩段式）
1. `pypdf` 抽文字
2. 抽出 < `PDF_MIN_TEXT_CHARS`（200）視為掃描檔／純圖排版 → `PyMuPDF` 將前 3 頁以 120 DPI 轉 PNG，改走 vision

**編碼**：解碼順序 `utf-8 → utf-8-sig → big5 → cp950 → gb18030 → shift_jis`，全失敗則 `errors="replace"`；前 4096 bytes 含 NUL 判定為二進位並略過。

**兩個關鍵設計**
- **base64 不進對話歷史** — `Bundle.placeholder()` 僅存 `[圖片: x.png]`，否則每輪重送整包圖，32k context 很快耗盡
- **圖片請求失敗自動降級** — `strip_images()` 退成純文字重試，並在 prompt 註明「附了圖但這次沒看到」。涵蓋 webp 等 llama.cpp `mtmd`（stb_image）不支援的格式

新增依賴：`pypdf 6.14.2`、`pymupdf 1.28.0`

**config**
```
ATTACHMENT_ENABLED / MAX_IMAGES_PER_MESSAGE=4 / MAX_IMAGE_MB=8
MAX_TEXT_FILES_PER_MESSAGE=5 / MAX_TEXT_FILE_KB=256 / MAX_TEXT_CHARS_TOTAL=8000
MAX_PDF_MB=20 / MAX_PDF_PAGES=30 / PDF_MIN_TEXT_CHARS=200
MAX_PDF_RENDER_PAGES=3 / PDF_RENDER_DPI=120
```

---

### 2. 上網搜尋 — `websearch.py`（新檔）

沿用 PAI 平台 `WebSearchSkill.php` 的「免金鑰多來源備援」策略，但**來源清單經 2026-08-02 實測後重挑**：

| 來源 | 狀態 |
|---|---|
| DuckDuckGo html | ✅ 可用（實測 10 筆），主力 |
| DuckDuckGo Instant Answer | ✅ 官方 API，部分查詢有摘要 |
| Wikipedia API | ✅ 官方 API，查人事時地物穩定 |
| Brave | ❌ 改版為 JS 殼，無任何 class 可抓 |
| DuckDuckGo lite | ❌ 回 202 挑戰頁（內容為 DDG 首頁） |
| Mojeek | ❌ captcha |
| searx.be (`format=json`) | ❌ anti-bot 驗證頁 |

> ⚠️ **平台那支 PHP 的前兩個來源（brave、ddgLite）同樣已失效**，實際上只剩 ddgHtml 在支撐。

**兩處抓取修正**
- **濾除 DDG 廣告** — 贊助結果混在 `result__a` 中，特徵為 `duckduckgo.com/y.js` / `ad_provider=`
- **href 需先 `html.unescape`** — 原始 href 中的 `&amp;` 會導致 `uddg=` 轉址參數解析失敗

**觸發策略**（兩段式，避免拖慢「陪聊」主線）
1. 明確觸發詞（查一下／搜尋／google…）→ 直接搜，關鍵字為去除觸發詞後的殘餘
2. 含疑問特徵（`?`／什麼／最新／幾／多少…）→ 才呼叫 LLM 判斷器（`force_json`）
3. 其餘（訴苦、問候、閒聊）→ **完全不呼叫 LLM，不碰網路**

判斷器與分流各以 4／5 個案例驗證，全數正確。

---

### 3. 長期記憶 — `memory.py`（新檔）

架構沿用 PAI `ReflectiveMemory`（SQLite + JSON 向量 + cosine），但針對「記住使用者」重新設計。

**修掉原版的資料級 bug** — 見文末〈第三方缺陷〉。本版使用 `blake2b`，已驗證跨 process 檢索有效。

**schema**
```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, user_name TEXT,
    kind TEXT,              -- profile|preference|event|concern|relationship
    content TEXT NOT NULL,
    created_at TEXT, updated_at TEXT,
    hits INTEGER DEFAULT 0, embedding TEXT
);
CREATE INDEX idx_mem_user ON memories(user_id);
```

**設計要點**
- **抽取在背景執行** — 回覆送出後才 `asyncio.create_task(learn_from(...))`，不影響回話延遲；訊息 < 6 字直接跳過
- **去重** — 寫入前比對同 user 既有向量，cosine ≥ 0.88 視為同一件事改為 UPDATE
- **profile 恆帶** — 姓名、職業這類與當下話題語意不相似但每次都該記得的，不依賴相似度檢索
- **prune** — 每 user 上限 200 則，超出時依 `hits ASC, updated_at ASC` 淘汰
- **不記情緒** — extractor prompt 明確排除「他現在很累」這類會過期的內容
- SQLite 為同步，對外一律以 `asyncio.to_thread` 包裝

**新增指令**：`/memories`（ephemeral 列出）、`/forget`（清空該 user 全部記憶 + 所有 session）。`/reset` 的文案已修正為僅清除當次對話，並提示長期記憶保留。

**驗證**：寫入／去重（3 筆寫入 + 1 筆近似 → 仍 3 筆）／檢索（TACT 查詢 0.562、寵物 0.208、無關話題僅回 profile）／跨 process 檢索（0.612）／清空，全數通過。

---

### 4. 階段性回覆 — `Progress`（`main.py`）

單一訊息 in-place edit，回覆送出前刪除：

```
📎 正在讀取 tact.pdf… → 🔎 正在上網查「…」… → 💭 正在想怎麼回你… → (delete) → 回覆
```

- 每個階段獨立 `try`，進度提示失敗**絕不影響**真正的回覆
- 僅在確有慢動作（讀附件／搜尋）時建立訊息；純聊天不產生任何額外訊息（已驗證 `progress.msg is None`）
- `PROGRESS_ENABLED` 可關閉

---

## 🎙️ 語音鏈路 — 3 個缺陷

作用於共用服務 `/opt/security-one-waf/voice/voice_server.py`（`:8891`，另有 WAF 前端、intellitrust-website 使用）。

> 📌 該服務雖以 Freeze-Omni 的 Socket.IO 協定對外，**後端模型實為 `openbmb/MiniCPM-o-4_5`**。真正的 Freeze-Omni 在 `:8892`，`autostart=false` 且未運行。nana-bot 端的 `FREEZE_OMNI_URL` 等命名僅為歷史遺留，已正名為 `VOICE_SERVER_URL`（保留舊名 alias）。

### Bug 1 — Silero VAD 單例共用且從不 `reset_states()`

**影響**：第一個 session 灌完音訊後 RNN 狀態被污染，之後**所有** session 永久失聰，須重啟服務才恢復一次。

**證據**（同一段真實錄音）

| VAD 狀態 | max prob | 超過門檻的視窗 |
|---|---|---|
| 乾淨 | 0.959 | 6 / 124 |
| 灌入 2 分鐘前段音訊後 | **0.005** | **0 / 124** |
| `reset_states()` 後 | 0.959 | 6 / 124 |

與時間軸完全吻合：01:37 VAD 首次載入 → 01:42 唯一一次成功回話 → 此後全聾。

**修復**：改為 per-session 實例（`new_vad()` / `session_vad()`，實測 `load_silero_vad()` 約 22ms，`MAX_USERS=8` 可負擔），並在 `_reset_turn_state()` 中重置。順帶解決多人同時連線互相污染。

### Bug 2 — 待機期間不重置導致死鎖

僅在「回合結束」重置並不足夠：客戶端在無人說話時仍持續送靜音填充，長串靜音會把狀態帶偏 → 偵測不到語音 → **永遠沒有回合結束 → 永遠不會重置**，就此鎖死。實測使用者 session 落在 `max=0.00`。

**修復**：新增 `IDLE_VAD_RESET_WINDOWS`（預設 47 窗 ≈ 1.5s），連續非語音達門檻即重置。`PRESPEECH_WINDOWS`（12 窗前導）確保開口瞬間的音訊不因重置而遺失。

**驗證**：先送 60 秒待機背景音再送 6 秒真人語音 → `turn start: speech=5344ms audio=6.72s`，完整切出。

### Bug 3 — `MIN_SPEECH_MS=400` 過嚴且靜默丟棄

中文短句（「奈奈」「哈囉」約 190–350ms）整段被丟棄，且**無任何 log**，外部表現僅為「不理人」。

參數矩陣實測後採 **threshold 0.35 + 250ms**（0.3 會把相鄰兩句黏成單一 672ms turn）。原為硬編碼，已改為環境變數且**程式碼預設值維持 400 不變**。

**新增診斷**
- 丟棄時輸出 `utterance {N}ms < MIN_SPEECH_MS`
- turn 起始輸出 `speech={N}ms audio={N}s`——模型直接吃這段音訊（speech-to-speech，無中間轉錄），此數字是判斷「切得對不對」的唯一依據
- `VOICE_VAD_DEBUG=1` 每 ~3s 輸出機率分布，用以區分「切太碎」與「音訊本身聽不出人聲」

**supervisor env**
```
VOICE_VAD_THRESHOLD=0.35  VOICE_MIN_SPEECH_MS=250  VOICE_VAD_DEBUG=1
```

> `_TTS_CANCEL` 為全域 Event 一度被懷疑是 bug，查證後確認為**刻意設計**（單 GPU，`_GEN_LOCK` 一次僅允許一個生成，新回合要求舊回合讓位）。未修改。

---

## 🔇 回授誤觸打斷 — `voice_bridge.py`

**症狀**：AI 語音講到一半突然中斷。

```
02:21:39  stream done: '你最近是不是有点累呀？…'  audio=5.56s
02:21:40  🛑 Barge-in: 打斷 AI          ← 播放 1 秒即被清空
02:22:31  stream done: '有什么轻松的话题'   ← 半句
```

**根因**：使用者喇叭播出的奈奈聲音被自身麥克風收回 → 誤判為插話。`voice_server.py` 原始碼已註明 barge-in 預設關閉正是為此；supervisor 設定以「前端已過濾回授」為由啟用，該前提僅對具備 AEC 的瀏覽器成立，**Discord 機器人無回音消除**。且 nana 端門檻僅振幅 500（環境噪音等級）並以**單一幀**觸發。

**修復**
- AI 播放期間一律送靜音，避免回音進入伺服器 VAD
- 需振幅 > `VOICE_BARGE_IN_THRESHOLD`（6000）連續 `VOICE_BARGE_IN_FRAMES`（15 幀 = 300ms）方判定為真實插話
- 新增 `ai_is_speaking()`：不可僅看佇列是否為空——伺服器分段串流，兩段之間佇列會短暫清空，該空隙足以讓回音溜入。以 `VOICE_AI_SPEAKING_HOLD_S`（1.5s）延遲判定；`stop_tts` 時立即失效

⚠️ 此修復的門檻值需以實際喇叭音量校準，**尚未經真人測試**。

---

## 🐛 暱稱被誤判為檔案（本次引入的 regression）

**症狀**：使用者 Discord 暱稱為 `【窮鬼】vito.ipynb`，奈奈回覆「看到你傳來一個名為 `vito.ipynb` 的檔案」。

**根因**：訊息包裝格式為 `[{display_name}] 說：{content}`（既有設計），而本次為附件功能在 `SYSTEM_PROMPT` 常駐加入「對方可能會傳圖片或檔案給你」，**將模型注意力導向檔案**，遂將暱稱中的 `.ipynb` 幻覺為附件。

**修復**
1. 檔案指引抽出為 `ATTACHMENT_PROMPT`，**僅在 `bundle.has_any` 時注入**
2. `SYSTEM_PROMPT` 新增〈訊息格式〉段落，明示方括號內為 Discord 暱稱、可能形似檔名，且僅 `===== 檔案：… =====` 區塊代表真實附件

**驗證**：無附件時不再提及檔案（並正確稱呼為 vito）；有附件時仍正確讀出 `bug.py` 的 `a - b` 缺陷。

---

## 🧹 雜項

**移除 log 洗版** — `discord/voice/receive/router.py` 中有一行 per-packet `print(...)` 至 stderr（約 50 行/秒），將 `nana-bot.log` 中的真實事件完全淹沒，實質妨礙本次除錯。已改為 `_log.debug`。

> ⚠️ 此修改位於 venv 內，**重裝套件會被覆蓋**。備份：`router.py.bak`

---

## 🔴 第三方／共用程式碼缺陷（已記錄，未修）

### 1. `pai/learning.py` — `HashingEmbedder` 使用內建 `hash()`

```python
h = hash(tok) % self.dim     # ← 字串 hash 每個 process 隨機化（PYTHONHASHSEED）
```

實測同一字串三次得三個不同值。向量寫入 SQLite 後，**重啟即與新算出的 query 向量完全對不起來，語義檢索永久失效**。

影響 `/opt/pai-agents/guardian_memory.db`（現有 9 筆 experiences）。`EmbeddingClient` 在遠端 embedding 服務不可用時會 fallback 到此類別——而 `:10003` 目前回 `501 not_supported`（未帶 `--embeddings`），故該 fallback 路徑必然被觸發。

**建議**：改用 `hashlib.blake2b`（本專案 `memory.py` 的 `StableHashEmbedder` 即為此實作，可直接移植）。

### 2. `app/Pai/Skills/Builtin/WebSearchSkill.php` — 3 個來源已死其 2

`brave` 與 `ddgLite` 皆已失效（詳見上文表格），實際僅 `ddgHtml` 可用，備援機制形同虛設。另建議補上 DDG 廣告過濾與 href `html_entity_decode`。

---

## 📁 異動檔案

**新增**
```
attachments.py      附件讀取（文字／圖片／PDF）
websearch.py        上網搜尋
memory.py           長期記憶
CHANGELOG-2026-08-02.md       使用者向公告（Discord）
CHANGELOG-DEV-2026-08-02.md   本檔
```

**修改**
```
main.py             Progress 類別、recall_memory/learn_from/_run_search、
                    on_message 與 handle_direct_conversation 串接、
                    /memories 與 /forget 指令、help embed、Freeze-Omni 命名正名
config.py           附件／搜尋／記憶／進度／barge-in 設定；SYSTEM_PROMPT
                    〈訊息格式〉段；ATTACHMENT_PROMPT、SEARCH_DECIDER_PROMPT、
                    MEMORY_EXTRACTOR_PROMPT；VOICE_SERVER_URL（含舊名 alias）
llm_client.py       generate_support_response 支援 content blocks 與
                    memory_context；新增 decide_search()、extract_memories()
voice_bridge.py     ai_is_speaking() 與回授防護；命名正名
.env                LLM 後端改指 :10003；FREEZE_OMNI_URL → VOICE_SERVER_URL
```

**專案外**
```
/etc/systemd/system/supervisor.service.d/logdir.conf   （新增）
/etc/supervisor/conf.d/minicpm-o-voice.conf            VAD env
/opt/security-one-waf/voice/voice_server.py            VAD 3 項修復 + 診斷
venv/.../discord/voice/receive/router.py               停止 log 洗版
```

---

## ✅ 現況

- `nana-bot` RUNNING；supervisor 11 個 program 全數恢復
- 文字、附件（含 PDF）、搜尋、記憶：**均已實測通過**
- 語音：伺服器端已以 6 秒真人語音 + 60 秒待機情境驗證；**回授打斷門檻待真人校準**
- `VOICE_VAD_DEBUG=1` 暫時開啟，語音確認正常後應關閉

**待辦**
1. 真人測試語音，校準 `VOICE_BARGE_IN_THRESHOLD`
2. 關閉 `VOICE_VAD_DEBUG`
3. 下次重開機驗證 `LogsDirectory` drop-in
4. 評估修復 `pai/learning.py` 的 embedder
5. `llama-server.service.disabled` 的處置（確認是否為刻意停用）
