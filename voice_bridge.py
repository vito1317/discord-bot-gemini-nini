"""
奈奈機器人 — 語音橋接模組
整合 MiniCPM-o 4.5 語音 AI，提供 Discord 語音頻道即時對話
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import threading
import time
import queue
from datetime import datetime
from collections import deque
from typing import Optional

import discord
import numpy as np
import socketio as sio_client

import config

try:
    import memory
except Exception:  # noqa: BLE001
    memory = None
try:
    import agent as _agent
    import llm_client as _llm
except Exception:  # noqa: BLE001
    _agent = None
    _llm = None

logger = logging.getLogger("nana.voice")


def _tts_to_pcm48(text: str) -> "np.ndarray | None":
    """呼叫伺服器 /voice/tts-raw，把文字轉成 48kHz stereo int16（塞播放佇列用）。"""
    import base64 as _b64, json as _json, urllib.parse as _up, urllib.request as _u
    url = (config.VOICE_SERVER_URL.rstrip("/") + "/voice/tts-raw?text="
           + _up.quote((text or "")[:200]))
    try:
        with _u.urlopen(url, timeout=60) as r:
            d = _json.loads(r.read())
        if not d.get("success"):
            return None
        pcm = np.frombuffer(_b64.b64decode(d["audio_base64"]), dtype=np.int16)
        sr = int(d.get("sample_rate", 24000))
        # sr → 48k，再複製成 stereo（跟 on_ai_audio 同路徑）
        x_old = np.linspace(0, 1, len(pcm))
        x_new = np.linspace(0, 1, int(len(pcm) * 48000 / sr))
        pcm48 = np.interp(x_new, x_old, pcm.astype(np.float64)).astype(np.int16)
        return np.column_stack([pcm48, pcm48]).flatten().astype(np.int16)
    except Exception as e:  # noqa: BLE001
        logger.warning("tts-raw 失敗：%s", e)
        return None


def _voice_system_prompt(user_id: int | None) -> str:
    """語音的 system prompt，喚醒者已知時注入他的長期記憶。"""
    base = config.VOICE_SYSTEM_PROMPT
    if user_id and memory is not None and getattr(config, "MEMORY_ENABLED", False):
        try:
            mems = memory.store().retrieve(user_id, "最近 過得如何")
        except Exception as e:  # noqa: BLE001
            logger.debug("語音撈記憶失敗：%s", e)
            mems = []
        if mems:
            lines = "\n".join(f"- {m.content}" for m in mems[:8])
            base += ("\n\n## 你記得這位對象的事\n" + lines +
                     "\n（自然地運用，被問到時要記得，不要說「根據記憶」。）")
            logger.info("🧠 語音載入 %d 則記憶 │ user=%s", len(mems), user_id)
    return base


def _transcribe_pcm(pcm_16k: "np.ndarray") -> str:
    """呼叫伺服器 /voice/transcribe 做純辨識（不觸發回覆）。同步、給 wake thread 用。"""
    import base64 as _b64, io as _io, json as _json, wave as _wave, urllib.request as _u
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(np.clip(pcm_16k, -32768, 32767).astype(np.int16).tobytes())
    req = _u.Request(
        config.VOICE_TRANSCRIBE_URL,
        data=_json.dumps({"audio_base64": _b64.b64encode(buf.getvalue()).decode(),
                          "language": "zh"}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with _u.urlopen(req, timeout=30) as r:
            return (_json.loads(r.read()).get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("喚醒辨識失敗：%s", e)
        return ""


# ═══════════════════════════════════════════════════════
# 語音橋接器（MiniCPM-o 4.5，port 8891）
# ═══════════════════════════════════════════════════════


class VoiceBridge:
    """管理與 MiniCPM-o 4.5 語音 AI 的 Socket.IO 連線"""

    def __init__(self) -> None:
        self.sio: Optional[sio_client.Client] = None
        self.is_connected: bool = False
        self.ai_audio_queue: deque = deque(maxlen=500)
        self.user_audio_queue = queue.Queue()
        self.audio_received_count: int = 0
        self._polling: bool = False
        self._ai_last_audio_ts: float = 0.0   # 最後一次收到 AI 音訊的時間
        # ── 喚醒詞狀態（只在 nana 端）──
        self.awake_until: float = 0.0          # < now = 待機；> now = 喚醒視窗內
        self.wake_user_id: Optional[int] = None
        self._wake_buf: list = []              # 待機時累積的音訊，用來偵測喚醒詞
        self._wake_last_voice: float = 0.0
        self._wake_uid: Optional[int] = None
        self._voice_transcript: deque = deque(maxlen=30)

    def is_awake(self) -> bool:
        if not getattr(config, "VOICE_WAKE_ENABLED", False):
            return True                        # 沒開喚醒 → 一律放行
        return time.time() < self.awake_until

    def _set_wake_prompt(self, user_id: Optional[int]) -> None:
        """喚醒時重設 system prompt，注入該使用者的長期記憶。"""
        try:
            self.sio.emit("prompt_text", _voice_system_prompt(user_id))
        except Exception as e:  # noqa: BLE001
            logger.debug("重設語音 prompt 失敗：%s", e)

    def _check_wake(self, sentence: "np.ndarray", user_id: Optional[int]) -> None:
        """背景辨識一句話，開頭含喚醒詞就開啟喚醒視窗並載入記憶。"""
        text = _transcribe_pcm(sentence).lower().lstrip(" ,，。.!！?？~～")
        if not text:
            logger.info("💤 待機：辨識為空（音質差或非語音）")
            return
        head = text[:12]
        if any(w in head for w in config.VOICE_WAKE_WORDS):
            self.awake_until = time.time() + config.VOICE_WAKE_WINDOW_S
            self.wake_user_id = user_id
            self._set_wake_prompt(user_id)
            logger.info("🌸 喚醒！user=%s 聽到=%r", user_id, text[:30])
        else:
            logger.info("💤 待機中聽到：%r（沒有喚醒詞）", text[:30])

    def _process_command(self, sentence: "np.ndarray", user_id: Optional[int]) -> None:
        """喚醒視窗內的一句話：辨識 → 跑 agent（提醒等）＋抽記憶 → 指令則念回確認。"""
        if _agent is None:
            return
        text = _transcribe_pcm(sentence).strip()
        if len(text) < 2:
            return
        logger.info("🎙️ 喚醒中聽到：%r", text)
        # 存逐字稿供「總結剛剛」用
        self._voice_transcript.append(text)
        if any(k in text for k in ("總結", "剛剛說", "剛才說", "整理一下", "摘要", "回顧一下")):
            self._summarize_voice()
            return
        import asyncio as _aio

        async def _run():
            handled, say = False, ""
            try:
                res = await _agent.handle(text, user_id=user_id or 0,
                                          user_name="語音使用者", channel_id=0)
                handled = res.handled
                say = res.reply
                if handled and not say and res.context and _llm:
                    say = await _llm.generate_support_response(
                        user_message="[語音使用者] 說：" + text,
                        memory_context="\n\n## 你剛剛幫他做的事\n" + res.context) or ""
            except Exception as e:  # noqa: BLE001
                logger.warning("語音 agent 失敗：%s", e)
            # 記憶抽取（背景學習，不論是否指令）
            if _llm and memory and user_id:
                try:
                    items = await _llm.extract_memories("[語音使用者] 說：" + text)
                    if items:
                        n = await memory.remember(user_id, "語音使用者", items)
                        if n:
                            logger.info("🧠 語音記住 %d 則", n)
                except Exception as e:  # noqa: BLE001
                    logger.debug("語音記憶失敗：%s", e)
            return handled, say

        try:
            handled, say = _aio.run(_run())
        except Exception as e:  # noqa: BLE001
            logger.warning("語音指令處理失敗：%s", e)
            return
        if handled and say:
            self._speak_text(say)

    def _speak_text(self, text: str) -> None:
        """把一段文字用 TTS 念回（打斷可能的 S2S 回應）。"""
        pcm48 = _tts_to_pcm48(text)
        if pcm48 is None or not len(pcm48):
            return
        self.ai_audio_queue.clear()               # 打斷伺服器 S2S 的回應
        try:
            self.sio.emit("recording-started")    # 重置伺服器本輪
        except Exception:  # noqa: BLE001
            pass
        self._ai_last_audio_ts = time.time()
        raw = pcm48.tobytes()
        for i in range(0, len(raw), 3840):        # 20ms @ 48k stereo
            self.ai_audio_queue.append(raw[i:i + 3840])
        logger.info("🔊 語音念回：%r", text[:40])

    def speak(self, text: str, *, interrupt: bool = False) -> bool:
        """對外的 TTS 入口：把一段話念到語音頻道。回傳有沒有排進佇列。

        interrupt=False 時**接在後面排隊**，不清佇列 —— 旁白是一句接一句講的，
        用 _speak_text 那條會把前一句砍掉（它是為了「打斷 S2S 回應」設計的）。
        """
        if not (text or "").strip():
            return False
        pcm48 = _tts_to_pcm48(text)
        if pcm48 is None or not len(pcm48):
            return False
        if interrupt:
            self.ai_audio_queue.clear()
        self._ai_last_audio_ts = time.time()
        raw = pcm48.tobytes()
        for i in range(0, len(raw), 3840):        # 20ms @ 48k stereo
            self.ai_audio_queue.append(raw[i:i + 3840])
        logger.info("🔊 旁白：%r", text[:50])
        return True

    def _summarize_voice(self) -> None:
        """把剛剛語音對話的逐字稿總結後念回。"""
        kws = ("總結", "剛剛說", "剛才說", "整理一下", "摘要", "回顧一下")
        lines = [t for t in list(self._voice_transcript) if not any(k in t for k in kws)]
        if not lines:
            self._speak_text("我們剛剛好像還沒聊到什麼呢，多跟我說說吧。")
            return
        if _llm is None:
            return
        import asyncio as _aio
        joined = "。".join(lines[-20:])
        async def _s():
            return await _llm.generate_support_response(
                user_message="請用溫暖簡短的語氣，幫我總結我剛剛講的重點：\n" + joined)
        try:
            summary = _aio.run(_s())
        except Exception as e:  # noqa: BLE001
            logger.warning("語音總結失敗：%s", e)
            summary = None
        self._speak_text(summary or "我幫你回顧一下剛剛聊的內容。")

    def _feed_wake(self, pcm_16k: "np.ndarray", user_id: Optional[int]) -> None:
        """累積音訊，能量 VAD 判句尾 → 待機時偵測喚醒詞、喚醒中跑 agent。"""
        now = time.time()
        amp = int(np.max(np.abs(pcm_16k))) if pcm_16k.size else 0
        if amp > config.VOICE_WAKE_SPEECH_AMP:
            self._wake_buf.append(pcm_16k)
            self._wake_last_voice = now
            self._wake_uid = user_id
        elif self._wake_buf and (now - self._wake_last_voice) * 1000 > config.VOICE_WAKE_END_SIL_MS:
            sentence = np.concatenate(self._wake_buf)
            self._wake_buf = []
            if sentence.size >= 16000 * config.VOICE_WAKE_MIN_MS / 1000:
                # 待機 → 偵測喚醒詞；喚醒視窗內 → 跑 agent（提醒／記憶／總結）
                target = self._process_command if self.is_awake() else self._check_wake
                threading.Thread(target=target,
                                 args=(sentence, self._wake_uid), daemon=True).start()
        # 待機緩衝別無限成長
        if len(self._wake_buf) > 500:
            self._wake_buf = self._wake_buf[-250:]

    def ai_is_speaking(self) -> bool:
        """AI 是不是正在（或剛剛還在）講話。

        不能只看佇列空不空 —— 伺服器是一段一段串流過來的，兩段之間佇列會短暫
        清空，那個瞬間若把麥克風放行，回音就會誤觸打斷。所以最後一次收到 AI
        音訊後的一小段時間內都算「還在講」。
        """
        if self.ai_audio_queue:
            return True
        return (time.time() - self._ai_last_audio_ts) < config.VOICE_AI_SPEAKING_HOLD_S

    def connect(self) -> None:
        """連接到語音伺服器（在背景執行緒中呼叫）"""
        self.sio = sio_client.Client(
            reconnection=True,
            reconnection_attempts=5,
            logger=False,
            engineio_logger=False,
        )

        @self.sio.on("connect")
        def on_connect() -> None:
            logger.info("✅ 已連接語音伺服器，正在設定專屬提示詞...")
            self.is_connected = True
            # 初始用待機 prompt（無特定使用者記憶）；喚醒時再重設帶記憶的
            self.sio.emit("prompt_text", _voice_system_prompt(None))

        @self.sio.on("prompt_success")
        def on_prompt_success() -> None:
            logger.info("✅ 提示詞設定完成，開始啟動語音辨識")
            self.sio.emit("recording-started")
            if not self._polling:
                self.start_polling()

        @self.sio.on("disconnect")
        def on_disconnect() -> None:
            logger.info("❌ 語音伺服器已斷開（將自動重連）")
            self.is_connected = False
            self._polling = False

        @self.sio.on("audio")
        def on_ai_audio(data: bytes) -> None:
            if not self.is_connected:
                return
            try:
                self.audio_received_count += 1
                self._ai_last_audio_ts = time.time()
                pcm_24k = np.frombuffer(data, dtype=np.int16)

                # 24kHz mono → 48kHz stereo（線性插值）
                x_old = np.linspace(0, 1, len(pcm_24k))
                x_new = np.linspace(0, 1, len(pcm_24k) * 2)
                pcm_48k = np.interp(x_new, x_old, pcm_24k.astype(np.float64)).astype(np.int16)
                stereo = np.column_stack([pcm_48k, pcm_48k]).flatten().astype(np.int16)
                self.ai_audio_queue.append(stereo.tobytes())
            except Exception as e:
                logger.error("AI 音訊處理錯誤: %s", e)

        @self.sio.on("stop_tts")
        def on_stop_tts() -> None:
            self._ai_last_audio_ts = 0.0
            if self.ai_audio_queue:
                self.ai_audio_queue.clear()
                logger.info("⏸️ AI TTS 被打斷")

        try:
            self.sio.connect(
                config.VOICE_SERVER_URL,
                transports=["websocket", "polling"],
                wait_timeout=10,
            )
        except Exception as e:
            logger.error("❌ 語音伺服器連線失敗: %s", e)

    def queue_user_audio(self, pcm_16k_mono: np.ndarray, user_id: Optional[int] = None) -> None:
        """將使用者語音加入佇列。待機時順便偵測喚醒詞。"""
        # 一律切句：待機時偵測喚醒詞，喚醒視窗內跑 agent（提醒／記憶／總結）
        if getattr(config, "VOICE_WAKE_ENABLED", False):
            self._feed_wake(pcm_16k_mono, user_id)

        chunk_size = 320  # 20ms @ 16kHz
        for i in range(0, len(pcm_16k_mono), chunk_size):
            chunk = pcm_16k_mono[i : i + chunk_size]
            if len(chunk) < chunk_size:
                chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
            self.user_audio_queue.put(chunk)

    def start_polling(self) -> None:
        """啟動 20ms 音訊輪詢迴圈"""
        if self._polling:
            return
        self._polling = True
        logger.info("🔄 音訊輪詢已啟動")

        CHUNK_SIZE = 320   # 20ms @ 16kHz，跟 queue_user_audio 切的一致

        def poll_loop() -> None:
            """以牆鐘定速把音訊送給伺服器：每 20ms 恰好送 20ms。

            為什麼一定要定速：Discord 的 opus 解碼器會丟包、會成批吐資料，
            實測 1500 個 RTP 封包只產出 300 個可用音框。若「有資料才送」，
            送出去的串流就是把零散片段接在一起 —— 時間軸被壓縮數倍，
            音素時長全毀，模型聽到的是人聲但辨識不出字，只能產生幻覺回答
            （實測轉錄為「韓國、Gmail、Gmail、Gmail…」這種退化重複）。

            定速輸出保證伺服器端的 1 秒就是真實的 1 秒：佇列有資料就送真實
            音訊，沒有就補靜音。這樣 VAD 的停頓判斷與模型的語速都會正確。
            """
            period = CHUNK_SIZE / 16000.0        # 20ms
            silence = np.zeros(CHUNK_SIZE, dtype=np.int16)
            next_at = time.monotonic()
            idle_frames = 0                      # 連續補靜音的幀數
            loud_frames = 0
            interrupting = False
            sent = 0

            while self._polling and self.is_connected and self.sio:
                try:
                    # 定速 20ms 取用（回退版）：blocking get 會被 queue 抖動
                    # 帶歪整段時序，反而更糟。維持固定節奏，空檔補靜音。
                    try:
                        chunk = self.user_audio_queue.get_nowait()
                        idle_frames = 0
                    except queue.Empty:
                        idle_frames += 1
                        if idle_frames > config.VOICE_IDLE_STOP_FRAMES:
                            next_at += period
                            slack = next_at - time.monotonic()
                            if slack > 0:
                                time.sleep(slack)
                            else:
                                next_at = time.monotonic()
                            continue
                        chunk = silence

                    max_val = int(np.max(np.abs(chunk)))

                    # ── 喚醒 gate ──
                    # 待機時把真實音訊換成靜音（伺服器 VAD 不觸發 → 不回應），
                    # 喚醒偵測仍在 queue_user_audio 那側進行。喚醒視窗內放行，
                    # 且有聲就刷新視窗 → 同一人可連續講、不必重覆喊「奈奈」。
                    if getattr(config, "VOICE_WAKE_ENABLED", False):
                        if not self.is_awake():
                            chunk = silence
                        elif max_val > config.VOICE_WAKE_SPEECH_AMP:
                            self.awake_until = time.time() + config.VOICE_WAKE_WINDOW_S

                    ai_playing = self.ai_is_speaking()

                    if not ai_playing:
                        interrupting = False
                        loud_frames = 0
                    elif not interrupting:
                        # AI 正在講話，而 Discord 這條路徑沒有回音消除：
                        # 使用者喇叭放出的奈奈聲音會被自己的麥克風收回來。
                        if not config.VOICE_BARGE_IN_ENABLED:
                            # 一律送靜音，杜絕回音回傳 → 伺服器不會誤判插話、
                            # 不會打斷奈奈自己（避免「一直重複同一句」）。
                            chunk = silence
                        elif max_val > config.VOICE_BARGE_IN_THRESHOLD:
                            loud_frames += 1
                        else:
                            loud_frames = 0

                        if loud_frames >= config.VOICE_BARGE_IN_FRAMES:
                            interrupting = True
                            loud_frames = 0
                            logger.info("🛑 Barge-in：使用者蓋過 AI（振幅 %d），中斷播放",
                                        max_val)
                            self.ai_audio_queue.clear()
                            self.sio.emit("recording-started")
                        else:
                            chunk = silence

                    self.sio.emit("audio", json.dumps({
                        "audio": list(chunk.tobytes()),
                        "sample_rate": 16000,
                    }))

                    sent += 1
                    if sent % 500 == 0:
                        logger.info("📤 送出 %d 幀（%.1fs 音訊）│ 佇列積壓 %d",
                                    sent, sent * period, self.user_audio_queue.qsize())

                    # 定速對齊到下一個 20ms 邊界
                    next_at += period
                    slack = next_at - time.monotonic()
                    if slack > 0:
                        time.sleep(slack)
                    elif slack < -0.5:
                        next_at = time.monotonic()

                except Exception as e:
                    logger.error("輪詢錯誤: %s", e)
                    break

        threading.Thread(target=poll_loop, daemon=True).start()

    def stop_polling(self) -> None:
        self._polling = False

    def disconnect(self) -> None:
        self.stop_polling()
        if self.sio and self.is_connected:
            try:
                self.sio.emit("recording-stopped")
                self.sio.disconnect()
            except Exception:
                pass
        self.is_connected = False


# ═══════════════════════════════════════════════════════
# Discord 音訊 Sink（接收使用者語音）
# ═══════════════════════════════════════════════════════


class OmniSink(discord.sinks.Sink):
    """接收 Discord 語音並轉發到語音伺服器"""

    __sink_listeners__: list = []

    def __init__(self, bridge: VoiceBridge, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bridge = bridge
        self._decoded_count: int = 0
        self._last_voiced = None
        self._gap_run = 0

    def is_opus(self) -> bool:
        return False

    def walk_children(self):
        return []

    def write(self, data, user) -> None:
        """接收解碼後的 48kHz stereo PCM"""
        if hasattr(data, "pcm"):
            raw = data.pcm
        elif isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data)
        else:
            return

        if not raw:
            return

        self._decoded_count += 1
        
        # 防止多個使用者同時講話導致音訊交錯破音 (Active Speaker Lock)
        import time
        if not hasattr(self, "active_speaker"):
            self.active_speaker = None
            self.active_speaker_time = 0
            self.last_loud_time = 0

        now = time.time()
        pcm_48k_float = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        if len(pcm_48k_float) < 2:
            return
            
        is_loud = int(np.max(np.abs(pcm_48k_float))) > 500
        
        # 判斷是否需要切換說話者
        if self.active_speaker is None or (now - self.active_speaker_time) > 1.0:
            self.active_speaker = str(user)
        elif str(user) != self.active_speaker:
            if is_loud and (now - self.last_loud_time) > 0.5:
                # 原本的人沒聲音了，被新的人搶走發言權
                self.active_speaker = str(user)
            else:
                # 忽略其他人（避免跟當前發言者音頻交錯）
                self._lock_drops = getattr(self, "_lock_drops", 0) + 1
                self._seen_users = getattr(self, "_seen_users", set())
                self._seen_users.add(str(user))
                if self._lock_drops % 100 == 0:
                    logger.warning(
                        "🔒 主動說話者鎖已丟棄 %d 個封包（收到 %d 個）│ "
                        "目前鎖定 %r │ 看到的來源: %r",
                        self._lock_drops, self._decoded_count,
                        self.active_speaker, sorted(self._seen_users)[:6],
                    )
                return

        self.active_speaker_time = now
        if is_loud:
            self.last_loud_time = now

        # 48kHz stereo int16 → 16kHz mono int16
        pcm_48k_mono = pcm_48k_float.reshape(-1, 2).mean(axis=1)

        # 自動增益（取代原本固定 ×2.5）
        #
        # 固定倍率的問題：麥克風大聲時會硬削波。實測某次錄音有 786 個樣本打到
        # int16 上下限、RMS 高達 6077，削波產生的寬頻失真讓 Silero VAD 的
        # speech probability 從 0.78 掉到 0.08 —— 表現出來就是「語音亂回答」。
        #
        # 改成：用緩慢衰減的峰值追蹤器決定倍率，只在訊號真的偏小時才放大，
        # 且保證放大後不超過 TARGET_PEAK。不對靜音放大（否則底噪會被推成人聲）。
        peak = float(np.max(np.abs(pcm_48k_mono)))
        self._peak_track = max(peak, getattr(self, "_peak_track", 0.0) * 0.95)
        gain = 1.0
        if self._peak_track > config.VOICE_AGC_MIN_PEAK:
            gain = min(config.VOICE_AGC_MAX_GAIN,
                       config.VOICE_AGC_TARGET_PEAK / self._peak_track)
            gain = max(1.0, gain)     # 只放大，不衰減（衰減交給下面的限幅）
        pcm_48k_mono = pcm_48k_mono * gain

        # 削波統計 —— 沒有這個就只能猜「是不是聽不清楚」
        self._agc_frames = getattr(self, "_agc_frames", 0) + 1
        self._clip_samples = getattr(self, "_clip_samples", 0) + int(
            (np.abs(pcm_48k_mono) >= 32700).sum())
        self._total_samples = getattr(self, "_total_samples", 0) + len(pcm_48k_mono)
        if self._agc_frames % 100 == 0:
            ratio = self._clip_samples / max(1, self._total_samples) * 100
            logger.info(
                "🎚️ 音訊 │ 處理 %d 幀 / 收到 %d（鎖丟 %d, 合成靜音丟 %d）│ "
                "增益 %.2fx │ 峰值追蹤 %.0f │ 削波 %.2f%%",
                self._agc_frames, self._decoded_count,
                getattr(self, "_lock_drops", 0), getattr(self, "_synth_silence", 0),
                gain, self._peak_track, ratio)
            self._clip_samples = self._total_samples = 0

        # 高品質反鏡像降頻：48kHz → 16kHz
        import scipy.signal
        pcm_16k_mono = scipy.signal.resample_poly(pcm_48k_mono, 1, 3)

        pcm_16k_mono = np.clip(pcm_16k_mono, -32768, 32767).astype(np.int16)
        
        # 把收到的音訊錄下來保留（存專案目錄，重開機不會消失）。
        # 這是「奈奈實際收到什麼」的唯一憑據 —— 音質問題不用靠猜。
        if config.VOICE_RECORD_SECONDS > 0:
            if not hasattr(self, "_rec_buf"):
                self._rec_buf = []
                self._rec_n = 0
                self._rec_done = False
                self._rec_path = None
                self._rec_limit = int(16000 * config.VOICE_RECORD_SECONDS)
                self._rec_last_flush = 0.0
            if not self._rec_done:
                self._rec_buf.append(pcm_16k_mono)
                self._rec_n += len(pcm_16k_mono)
                # 以「牆鐘時間」決定落盤，不看累積音訊量。
                # 看音訊量的話，解碼器大量出錯時 write() 收到的幀很少，
                # 永遠達不到門檻 → 一個檔案都不會產生（實際踩到過）。
                _now = time.monotonic()
                if _now - getattr(self, "_rec_last_flush", 0.0) >= 2.0:
                    self._rec_last_flush = _now
                    self._flush_recording()
                if self._rec_n >= self._rec_limit:
                    self._flush_recording()
                    self._rec_done = True
                    logger.info("🎙️ 錄音已達上限 %.0fs，停止錄製",
                                config.VOICE_RECORD_SECONDS)

        # 注意：**不要**丟棄全零音框。
        # 曾經以為那是「解碼器補丟包的合成靜音、屬於雜訊」而把它丟掉，結果更糟：
        # 那些零其實是「這 20ms 沒有可用封包」的**時間佔位符**。丟掉之後前後
        # 不連續的語音會被接在一起，音素時長被壓縮，聽起來是人聲卻無法辨識
        # （轉錄出來是「韓國、Gmail、Gmail、Gmail…」這種退化重複）。
        # 時間軸的正確性由 poll_loop 的定速輸出負責，這裡照原樣送進佇列。
        # 填補短洞（PCM 層丟包補償）。
        # 順序修好後，缺口是「連續序號裡的單一跳號」＝散落的短靜音塊。
        # 硬靜音會把語音切斷、干擾 S2S 模型；改用「前一幀的衰減延續」填，
        # 讓聽感連續。只補短洞（≤ 上限），長靜音（真的沒說話）保持靜音。
        if config.VOICE_FILL_GAPS and pcm_16k_mono.size:
            if not np.any(pcm_16k_mono):
                prev = getattr(self, "_last_voiced", None)
                if prev is not None and self._gap_run < config.VOICE_FILL_MAX_FRAMES:
                    self._gap_run += 1
                    decay = config.VOICE_FILL_DECAY ** self._gap_run
                    pcm_16k_mono = (prev * decay).astype(np.int16)
            else:
                self._last_voiced = pcm_16k_mono.copy()
                self._gap_run = 0

        # 帶上講話者的 Discord user id（喚醒偵測 + 記憶注入用）
        uid = getattr(user, "id", None)
        if uid is None and isinstance(user, int):
            uid = user
        elif uid is None and str(user).isdigit():
            uid = int(user)
        self.bridge.queue_user_audio(pcm_16k_mono, uid)

    def _flush_recording(self) -> None:
        """把目前緩衝的音訊寫進檔案（同一 session 覆寫同一檔）。"""
        if not self._rec_buf and self._rec_path:
            return
        import os
        import wave

        try:
            os.makedirs(config.VOICE_RECORD_DIR, exist_ok=True)
            if self._rec_path is None:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self._rec_path = os.path.join(
                    config.VOICE_RECORD_DIR, f"nana-{stamp}.wav")
                self._prune_recordings()
            # 每次都整段重寫（音檔不長，比維護 wave 檔頭簡單且不會壞檔）
            with wave.open(self._rec_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"".join(c.tobytes() for c in self._rec_all()))
            logger.info("🎙️ 錄音已存 %s（%.1fs）",
                        self._rec_path, self._rec_n / 16000)
        except Exception as e:
            logger.warning("錄音寫檔失敗：%s", e)

    def _rec_all(self):
        """目前 session 累積的全部音框（落盤後不清空，維持整段完整）。"""
        return self._rec_buf

    @staticmethod
    def _prune_recordings() -> None:
        """只保留最近 N 個錄音檔。"""
        import os

        try:
            d = config.VOICE_RECORD_DIR
            files = sorted(
                (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".wav")),
                key=os.path.getmtime,
            )
            for old in files[: max(0, len(files) - config.VOICE_RECORD_KEEP)]:
                os.remove(old)
                logger.debug("刪除舊錄音 %s", old)
        except Exception as e:
            logger.debug("清理舊錄音失敗（忽略）：%s", e)

    def cleanup(self) -> None:
        # 離開語音時把尾段也寫下來
        if getattr(self, "_rec_buf", None) and not getattr(self, "_rec_done", True):
            self._flush_recording()
        self.finished = True


# ═══════════════════════════════════════════════════════
# Discord 音訊 Source（播放 AI 語音）
# ═══════════════════════════════════════════════════════


class AIAudioSource(discord.AudioSource):
    """從語音伺服器播放 AI 回應到 Discord 語音頻道"""

    def __init__(self, bridge: VoiceBridge) -> None:
        self.bridge = bridge
        self._buffer = b""
        self._frame_count = 0

    def read(self) -> bytes:
        frame_size = 3840  # 20ms at 48kHz stereo 16-bit
        while len(self._buffer) < frame_size and self.bridge.ai_audio_queue:
            self._buffer += self.bridge.ai_audio_queue.popleft()

        if len(self._buffer) >= frame_size:
            frame = self._buffer[:frame_size]
            self._buffer = self._buffer[frame_size:]
            self._frame_count += 1
            return frame
        # queue 空：
        # - AI 仍在說（ai_is_speaking 的 1.5s hold 內，串流間隙）→ 送靜音維持
        #   播放，speaking 燈保持亮
        # - 真的說完 → 回 b"" 讓 py-cord 結束這次播放，speaking 自動熄滅
        #   （main 的 play monitor 會在下次有音訊時重新 play）
        if self.bridge.ai_is_speaking():
            return b"\x00" * frame_size
        return b""

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        pass


_FRAME = 3840        # 20ms、48kHz、雙聲道、16-bit —— Discord 要的一幀


class MixedAudioSource(discord.AudioSource):
    """把奈奈的說話聲和瀏覽器的聲音疊在一起送進語音頻道。

    為什麼要疊而不是二選一：py-cord 一次只能 play 一個 source。瀏覽器的聲音是
    持續的，如果讓它獨占輸出，_ai_play_monitor 裡的 `not vc.is_playing()` 就永遠
    不會成立 —— 她會變成完全說不出話。所以自己把兩邊的 PCM 加起來。

    （Python 3.13 把 audioop 移除了，所以用 numpy 做加總與削峰。）
    """

    def __init__(self, browser_src, ai_src) -> None:
        self.browser = browser_src
        self.ai = ai_src
        self._browser_dead = False

    def _read(self, src) -> bytes:
        try:
            d = src.read()
        except Exception as e:  # noqa: BLE001
            logger.debug("讀音訊失敗（當靜音）：%s", e)
            return b""
        return d if len(d) == _FRAME else b""

    def read(self) -> bytes:
        parts = []

        if not self._browser_dead:
            b = self._read(self.browser)
            if b:
                parts.append(np.frombuffer(b, dtype=np.int16).astype(np.int32)
                             * config.BROWSER_AUDIO_GAIN)
            else:
                # ffmpeg 收掉了（瀏覽器聲音沒了）—— 剩下她的聲音繼續播，
                # 不要把整個播放停掉，不然她也跟著閉嘴
                self._browser_dead = True

        a = self._read(self.ai)
        if a:
            parts.append(np.frombuffer(a, dtype=np.int16).astype(np.int32))

        if not parts:
            # 兩邊都沒東西：瀏覽器還活著就送靜音維持這條播放（等它出聲），
            # 瀏覽器都沒了就結束，讓 speaking 燈熄掉
            return b"\x00" * _FRAME if not self._browser_dead else b""

        mixed = parts[0] if len(parts) == 1 else parts[0] + parts[1]
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        for s in (self.browser, self.ai):
            try:
                s.cleanup()
            except Exception:  # noqa: BLE001
                pass


# ═══════════════════════════════════════════════════════
# 全域橋接實例
# ═══════════════════════════════════════════════════════

bridge = VoiceBridge()


def recording_done(error=None) -> None:
    """錄音結束回呼"""
    if error:
        logger.error("錄音錯誤: %s", error)
