"""
瀏覽器的聲音 🔊

讓奈奈開的那個瀏覽器**發出聲音**，並且把聲音接進 Discord 語音頻道 ——
她幫你開了 YouTube，你在語音頻道裡就聽得到。

## 為什麼需要這一支

三個關卡，缺一個就完全沒聲音（實測都踩過）：

1. **Playwright 預設就把瀏覽器靜音**。它會自己加 `--mute-audio`，
   從 chrome://version 印出來的參數可以看到。要用 `ignore_default_args`
   把它拿掉（見 browser.py 的 _ensure）。
   實測：拿掉之前錄到 -91 dB（完全無聲），拿掉之後 -7.5 dB。

2. **要有一個 bot 用得到的音效伺服器**。桌面那份 PipeWire 是跑在另一個使用者
   底下的，bot 用不到。這裡用 bot 自己的 XDG_RUNTIME_DIR，跟桌面那份完全隔離，
   **不會動到別人正在聽的東西**。
   有現成的 daemon 就用現成的、沒有才自己起 —— 這台機器的 pipewire 是 systemd
   socket-activated，硬起第二份會被 lockfile 擋掉然後立刻死掉（實測踩過）。

3. **要有地方可以錄**。瀏覽器的聲音送進一個假的輸出裝置（null sink），
   我們再從它的 monitor 錄出來餵給 Discord。不建這個的話聲音只是被丟掉。

## 設計

  • 不裝套件、不需要 root、不改系統設定 —— 全部是 bot 自己的子行程，
    bot 收攤就一起收掉（object.linger 只讓 sink 活過建立它的那個 pw-cli）
  • 起不來就回 False，讓呼叫端安靜降級成「沒有聲音」。
    聲音是加分功能，不該讓瀏覽器操作本身失敗
  • 冪等：ensure() 可以一直呼叫
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

import config

logger = logging.getLogger("nana.audio")

_procs: list[subprocess.Popen] = []
_ready = False


def _runtime_dir() -> str:
    """bot 自己的 XDG_RUNTIME_DIR。

    supervisor 起的行程通常沒有這個環境變數，但目錄本身存在（登入時建的），
    所以自己算出來。PipeWire 和 libpulse 都靠它找 socket。
    """
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d):
        return d
    return f"/run/user/{os.getuid()}"


def available() -> bool:
    """這台機器有沒有需要的工具。

    wireplumber 也是必要的，不是選用 —— 它是 PipeWire 的 session manager，
    「把播放的程式接到輸出裝置上」這件事是它在做。沒有它的話 daemon、裝置、
    pulse 相容層看起來全都正常，但 pw-link -l 是空的：沒有任何一條連線，
    所以錄出來永遠是 0 bytes（實測卡在這裡很久）。
    """
    return all(shutil.which(x) for x in
               ("pipewire", "pipewire-pulse", "wireplumber", "pw-cli"))


def sink_name() -> str:
    return config.BROWSER_AUDIO_SINK


def monitor_source() -> str:
    """ffmpeg 要錄的來源（null sink 的 monitor）。"""
    return f"{sink_name()}.monitor"


def _alive() -> bool:
    return bool(_procs) and all(p.poll() is None for p in _procs)


async def _run(cmd: list[str], env: dict, timeout: int = 10):
    """跑一個外部命令。

    **一定要丟到執行緒。** 這裡每個命令（pw-cli / pw-dump / ffmpeg -sources）都
    要幾百毫秒到幾秒，直接 subprocess.run 會把整個 bot 的 event loop 卡住 ——
    那段時間她收不到訊息，Discord 的心跳也可能逾時斷線。
    """
    return await asyncio.to_thread(
        subprocess.run, cmd, env=env, capture_output=True, text=True, timeout=timeout)


async def _daemon_ok(env: dict) -> bool:
    """已經有一個能用的 PipeWire daemon 了嗎。

    這台機器的 pipewire 是 systemd socket-activated —— 只要有人碰 socket 它就自己
    起來。所以**要先問清楚再決定要不要自己起一份**，硬起會被 lockfile 擋掉：
        unable to lock lockfile '/run/user/1001/pipewire-0.lock'
        (maybe another daemon is running)
    然後我們自己起的那份立刻死掉，結果一路降級成沒有聲音（實測踩過）。
    """
    try:
        return (await _run(["pw-cli", "info", "0"], env, 5)).returncode == 0
    except Exception:  # noqa: BLE001
        return False


async def _links_managed(env: dict) -> bool:
    """有沒有 session manager 在管接線。

    直接問「有沒有 wireplumber 在跑」比較誠實：pw-link -l 在剛開機、還沒有任何
    客戶端的時候本來就會是空的，用它判斷會誤判。
    """
    try:
        out = (await _run(["pw-dump"], env, 10)).stdout
        for node in json.loads(out or "[]"):
            props = (node.get("info") or {}).get("props") or {}
            if "wireplumber" in str(props.get("application.name", "")).lower():
                return True
            if props.get("api.acp.auto-port") is not None:
                return True
    except Exception as e:  # noqa: BLE001
        logger.debug("查 session manager 失敗：%s", e)
    return False


async def _sink_exists(env: dict) -> bool:
    try:
        out = (await _run(["pw-dump"], env, 10)).stdout
        for node in json.loads(out or "[]"):
            props = (node.get("info") or {}).get("props") or {}
            if props.get("node.name") == sink_name():
                return True
    except Exception as e:  # noqa: BLE001
        logger.debug("查音效裝置失敗（當成沒有）：%s", e)
    return False


async def _pulse_sees_it(env: dict) -> bool:
    """ffmpeg 的 pulse 層看不看得到我們那個裝置。

    **不要用「試錄一段」來驗證。** null sink 在沒有客戶端播放時它的 monitor
    完全不出資料，ffmpeg 會就這樣掛在那裡等 —— 試錄一定逾時，然後我們就會誤判成
    「音效環境壞了」把整套收掉（實測就是這樣一路降級成沒有聲音）。
    閒置時錄不到是**正常**的，不是故障。

    所以這裡只問「裝置在 pulse 那一層看得見嗎」，這個檢查即時回、不會卡：
        ffmpeg -sources pulse
          nana_browser.monitor [Monitor of Nana browser audio]
    真正的「有沒有聲音」由 BrowserAudioSource 在播的時候處理（缺資料就補靜音）。
    """
    try:
        r = await _run(["ffmpeg", "-hide_banner", "-sources", "pulse"], env, 10)
        return monitor_source() in (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("列 pulse 裝置失敗：%s", e)
        return False


def _spawn(cmd: list[str], env: dict) -> None:
    _procs.append(subprocess.Popen(
        cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True))


async def ensure() -> bool:
    """把音效環境準備好。已經好了就直接回 True。"""
    global _ready

    if not config.BROWSER_AUDIO:
        return False
    if not available():
        logger.warning("🔊 這台機器沒有 pipewire / pw-cli，瀏覽器不會有聲音")
        return False
    rt = _runtime_dir()
    if not os.path.isdir(rt):
        logger.warning("🔊 找不到 XDG_RUNTIME_DIR（%s），瀏覽器不會有聲音", rt)
        return False
    env = {**os.environ, "XDG_RUNTIME_DIR": rt}

    if _ready and await _alive_enough(env):
        return True

    try:
        # ① daemon：有現成的就用，沒有才自己起（見 _daemon_ok 的說明）
        if not await _daemon_ok(env):
            _spawn(["pipewire"], env)
            await asyncio.sleep(1.5)
            if not await _daemon_ok(env):
                logger.warning("🔊 PipeWire 起不來，瀏覽器不會有聲音")
                await stop()
                return False

        # ②a session manager。少了它就沒有人把「播放的程式」接到「輸出裝置」上，
        #    pw-link -l 會是空的，錄出來永遠 0 bytes。
        if not await _links_managed(env):
            _spawn(["wireplumber"], env)
            await asyncio.sleep(2)

        # ② 假的輸出裝置：瀏覽器往這裡播，我們從 <name>.monitor 錄出來
        if not await _sink_exists(env):
            # node.always-process 一定要開。null sink 預設只有「有客戶端在播」
            # 的時候才跑時鐘，沒人播的時候它的 monitor 完全不出資料 ——
            # ffmpeg 會就這樣掛在那裡等（實測卡滿 20 秒 timeout），
            # 而 Discord 那邊拿不到幀就會把播放停掉。網頁安靜時要出的是「靜音」，
            # 不是「沒有資料」。
            spec = ("{ factory.name=support.null-audio-sink"
                    f" node.name={sink_name()} node.description=\"Nana browser audio\""
                    " media.class=Audio/Sink object.linger=true"
                    " node.always-process=true node.pause-on-idle=false"
                    " audio.position=[FL FR] }")
            r = await _run(["pw-cli", "create-node", "adapter", spec], env)
            if r.returncode != 0:
                logger.warning("🔊 建不出音效裝置：%s", (r.stderr or r.stdout)[:150])
                await stop()
                return False
            await asyncio.sleep(0.8)

        # ③ 確認 pulse 那一層看得到它。看不到通常是少了 pulse 相容層，補起來再看
        if not await _pulse_sees_it(env):
            _spawn(["pipewire-pulse"], env)
            await asyncio.sleep(1.5)
            if not await _pulse_sees_it(env):
                logger.warning("🔊 pulse 層看不到那個音效裝置，瀏覽器不會有聲音")
                await stop()
                return False
    except Exception as e:  # noqa: BLE001
        logger.warning("🔊 音效環境起不來（%s），瀏覽器不會有聲音", str(e)[:150])
        await stop()
        return False

    # 之後開的子行程（Chromium、ffmpeg）都要看得到這個 runtime dir
    os.environ["XDG_RUNTIME_DIR"] = rt
    _ready = True
    logger.info("🔊 音效環境好了（裝置 %s，錄音來源 %s）", sink_name(), monitor_source())
    return True


async def _alive_enough(env: dict) -> bool:
    """之前準備好的東西還在不在。

    只看「我們自己起的行程」不夠：daemon 可能是系統起的（我們沒有 _procs），
    也可能是我們起的但被 systemd 換掉了。真正該問的是裝置還在不在。
    """
    if _procs and not _alive():
        return False
    return await _sink_exists(env)


def browser_env() -> dict[str, str]:
    """給 Chromium 的環境變數 —— 把它的聲音導到我們的假裝置。"""
    return {"XDG_RUNTIME_DIR": _runtime_dir(), "PULSE_SINK": sink_name()}


async def stop() -> None:
    """收掉**我們自己起的**音效行程（bot 關機時叫）。

    只收 _procs 裡的 —— daemon 可能是系統／別的 session 起的，那種不該我們去殺。
    """
    global _ready
    _ready = False
    for p in _procs:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    for p in _procs:
        try:
            p.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    _procs.clear()
