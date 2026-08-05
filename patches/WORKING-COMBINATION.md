# 目前可運作的 py-cord 組合（2026-08-04）

這個 venv 的 discord 套件是**混合體**，不是任何單一版本。純 pip 安裝會壞掉。

| 來源 | 檔案 |
|---|---|
| py-cord **2.7.1** | 大部分 |
| py-cord **2.8.1** | `discord/opus.py`（2.7.1 原版沒有 `PacketDecoder`） |
| 手裝的語音重寫 PR + 本地修改 | `discord/voice/` 整包 |

## 本地修改內容

- `voice/receive/reader.py`
  - SSRC→user 對映的暴力比對（原作者：vito）
  - 修掉暴力比對後又解密一次的 bug（會讓封包變 OPUS_SILENCE）
  - DAVE 解密統計 log
- `voice/receive/router.py`
  - 包住 `pop_data`/`sink.write` 的 try/except（上游沒有，一個壞封包會讓
    router 執行緒死掉、錄音停止且不自動恢復）
  - 把 per-packet 的 `print` 降成 `_log.debug`（原本每秒約 50 行洗掉整份 log）
  - 解碼器 pop 統計

## 不要做的事

- **不要 `pip install --upgrade py-cord`** —— 2.8.1 實測更差
  （DAVE 解密 100% → 90%，VAD mean 0.16-0.31 → 0.02，語音完全沒反應）。
  原因：2.8.1 的傳輸層解密改成固定 `result[8:]`，與 2.7.1 的
  `result[offset:]` 不同，DAVE 路徑的擴充標頭剝除是搭配各自傳輸層設計的。
- **不要設 `DISCORD_DISABLE_DAVE=1`** —— Discord 以 **4017** 拒絕連線，
  語音 E2EE 已強制，無法降級。

## 還原方式

    cp -a patches/discord-working-<timestamp>/discord \
          venv/lib/python3.13/site-packages/

## 2026-08-05: py-cord 2.8.1 slash 指令 bug 修復
`discord/commands/core.py` 的 `_invoke`：`issubclass(op._raw_type, Enum)`
沒先檢查 _raw_type 是不是 class，導致**所有帶參數的 slash 指令**在被呼叫時
崩潰（TypeError: issubclass() arg 1 must be a class → Discord 顯示「該申請未受回應」）。
改成 `isinstance(op._raw_type, type) and issubclass(...)`。
影響：voice_wake / checkin_status 等所有帶參數指令。重裝 py-cord 會被覆蓋。
