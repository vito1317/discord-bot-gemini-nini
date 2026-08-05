"""
The MIT License (MIT)

Copyright (c) 2021-present Pycord Development

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import logging

import os

try:
    import davey
except ImportError:
    HAS_DAVEY = False
    DAVE_PROTOCOL_VERSION = 0
else:
    HAS_DAVEY = True
    DAVE_PROTOCOL_VERSION = davey.DAVE_PROTOCOL_VERSION

# 本地修改：可用環境變數把宣告的 DAVE 版本壓成 0，讓 Discord 把頻道降級成
# 非 E2EE 傳輸（傳輸層仍加密）。
#
# 為什麼需要：py-cord 2.7.1 + davey 0.1.4 的 DAVE 接收實測不可靠 ——
# DAVE 解密本身 76% 成功，但 opus 解碼失敗累積 542 次，抵達 sink 的音框
# 只剩一小部分，音訊出現 20-30% 的空洞。使用者聽到的是「字是對的，
# 但斷斷續續到幾乎聽不懂」，語音模型只能產生幻覺回答。
#
# 設 DISCORD_DISABLE_DAVE=1 即宣告 0，整條 DAVE 路徑不會被走到。
if os.environ.get("DISCORD_DISABLE_DAVE", "0") == "1":
    DAVE_PROTOCOL_VERSION = 0
    logging.getLogger(__name__).warning(
        "DISCORD_DISABLE_DAVE=1 → 宣告 DAVE 版本 0，語音將不使用端對端加密"
    )

try:
    import nacl.secret
    import nacl.utils
except ImportError:
    HAS_NACL = False
else:
    HAS_NACL = True

VOICE_DEPENDENCY_WARNING_EMITTED = False

_log = logging.getLogger("discord.client")


def get_missing_voice_dependencies() -> tuple[str, ...]:
    missing: list[str] = []
    if not HAS_NACL:
        missing.append("PyNaCl")
    if not HAS_DAVEY:
        missing.append("davey")
    return tuple(missing)


def warn_if_voice_dependencies_missing() -> None:
    global VOICE_DEPENDENCY_WARNING_EMITTED
    if VOICE_DEPENDENCY_WARNING_EMITTED:
        return

    missing = get_missing_voice_dependencies()
    if not missing:
        return

    VOICE_DEPENDENCY_WARNING_EMITTED = True
    deps = ", ".join(missing)
    _log.warning(
        "%s %s not installed, voice will NOT be supported",
        deps,
        "is" if len(missing) == 1 else "are",
    )
