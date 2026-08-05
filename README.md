# 奈奈 🌸 — Discord 情緒支持機器人

一位溫暖、有同理心的 Discord 情緒支持夥伴，整合 LM Studio（Qwen3.5-35b-a3b）文字 AI 和 Freeze-Omni 語音 AI。

## 功能

### 💬 文字功能
- **自動情緒偵測**：監聽所有訊息，偵測到負面情緒時主動關心
- **關鍵字觸發**：訊息中包含「奈奈」時自動回應
- **對話模式**：@奈奈、回覆、私訊均可直接對話
- **危險訊息警告**：偵測到自傷等訊號時，發送警告到指定頻道

### 🎙️ 語音功能
- **語音頻道**：透過 `/join` 加入語音頻道
- **即時語音 AI**：橋接 Freeze-Omni，支援全雙工語音對話
- **自動打斷**：使用者說話時自動中斷 AI 語音

### ⌨️ 指令
| 斜線指令 | 前綴指令 | 說明 |
|---------|---------|------|
| `/chat` | `!nana` | 和奈奈聊天 |
| `/help` | `!help` | 顯示說明 |
| `/mood` | `!mood` | 心情小提醒 |
| `/reset` | `!reset` | 重置對話 |
| `/support` | `!support` | 心理健康資源 |
| `/join` | — | 加入語音頻道 |
| `/leave` | — | 離開語音頻道 |
| `/status` | — | 查看狀態 |

## 快速開始

### 前置需求
- Python 3.10+
- [LM Studio](https://lmstudio.ai/) 執行中（使用 Qwen3.5-35b-a3b 模型）
- [Freeze-Omni](https://github.com/) 語音伺服器（選用，語音功能需要）
- Discord Bot Token

### 安裝

```bash
cd /home/vito/nana-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 設定

```bash
cp .env.example .env
# 編輯 .env，至少設定 DISCORD_TOKEN
# 設定 ALERT_CHANNEL_ID 以啟用危險訊息警告
```

### 啟動

```bash
source venv/bin/activate
python main.py
```

## 架構

```
nana-bot/
├── main.py             # 主程式（Discord 事件、指令）
├── config.py           # 設定載入
├── llm_client.py       # LM Studio API 客戶端
├── conversation.py     # 對話歷史管理
├── voice_bridge.py     # Freeze-Omni 語音橋接
├── requirements.txt    # Python 依賴
├── .env                # 環境變數（不入版控）
└── .env.example        # 環境變數範本
```

## 技術棧

- **Discord**: py-cord 2.6+
- **文字 AI**: LM Studio + Qwen3.5-35b-a3b（OpenAI-compatible API）
- **語音 AI**: Freeze-Omni（Socket.IO 全雙工語音）
- **情緒偵測**: LLM-based 情緒分析
