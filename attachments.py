"""
奈奈機器人 — Discord 附件讀取（文字檔 + 圖片）

Gemma 4（:10003 的 llama-server 有掛 mmproj）原生支援 OpenAI 格式的
base64 image_url，所以圖片直接轉 data URI 塞進 content blocks 送出去。
文字檔則解碼後包成標記區塊，接在使用者訊息後面。

只有在奈奈本來就被搭話時（@提及／回覆／私訊／關鍵字／固定頻道）才會讀附件，
不會主動去翻頻道裡所有人貼的檔案。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field

import discord

import config

logger = logging.getLogger("nana.attach")

# ── 可讀的副檔名 ────────────────────────────────────────
# llama.cpp 的 mtmd 用 stb_image 解碼，支援 jpg/png/bmp/gif/pnm，
# 不支援 webp。webp 仍會嘗試送出，失敗時由 main.py 的降級路徑接住。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

PDF_EXTS = {".pdf"}

TEXT_EXTS = {
    # 純文字 / 文件
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    # 設定 / 資料
    ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".env",
    ".xml", ".properties",
    # 程式碼
    ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".php", ".go", ".rs",
    ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".swift",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".html", ".htm", ".css",
    ".scss", ".less", ".lua", ".r", ".pl", ".dart", ".m", ".scala",
    # 其他
    ".srt", ".vtt", ".gitignore", ".dockerfile", ".makefile",
}

# 解碼順序：優先 UTF-8，再試中文常見編碼（使用者多為繁中環境）
_ENCODINGS = ("utf-8", "utf-8-sig", "big5", "cp950", "gb18030", "shift_jis")


@dataclass
class Bundle:
    """一則訊息裡所有能讀的附件"""
    texts: list[tuple[str, str]] = field(default_factory=list)   # (檔名, 內容)
    images: list[tuple[str, str]] = field(default_factory=list)  # (檔名, data URI)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (檔名, 原因)

    @property
    def has_content(self) -> bool:
        return bool(self.texts or self.images)

    @property
    def has_any(self) -> bool:
        return bool(self.texts or self.images or self.skipped)

    def summary(self) -> str:
        """給 log 用的一行摘要"""
        bits = []
        if self.images:
            bits.append(f"{len(self.images)} 張圖")
        if self.texts:
            bits.append(f"{len(self.texts)} 個文字檔")
        if self.skipped:
            bits.append(f"{len(self.skipped)} 個略過")
        return "、".join(bits) or "無"

    def placeholder(self) -> str:
        """存進對話歷史用的純文字佔位符。

        圖片的 base64 不進 history —— 否則每輪都重送，context 會爆掉。
        """
        bits = [f"[圖片: {name}]" for name, _ in self.images]
        bits += [f"[檔案: {name}]" for name, _ in self.texts]
        return " ".join(bits)


def _ext(filename: str) -> str:
    name = filename.lower()
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def _is_image(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    return _ext(att.filename) in IMAGE_EXTS


def _is_text(att: discord.Attachment) -> bool:
    ctype = att.content_type or ""
    if ctype.startswith("text/"):
        return True
    if any(t in ctype for t in ("json", "xml", "javascript", "x-yaml")):
        return True
    return _ext(att.filename) in TEXT_EXTS


def _decode(raw: bytes) -> str | None:
    """把 bytes 解成文字；看起來是二進位就回 None。"""
    # NUL byte 幾乎只出現在二進位檔
    if b"\x00" in raw[:4096]:
        return None
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 全部失敗 → 容錯解碼，至少讓模型看到大部分內容
    return raw.decode("utf-8", errors="replace")


def _is_pdf(att: discord.Attachment) -> bool:
    if (att.content_type or "").startswith("application/pdf"):
        return True
    return _ext(att.filename) in PDF_EXTS


def _read_pdf(raw: bytes, name: str) -> tuple[str, list[str], str]:
    """解析 PDF。回傳 (文字, 圖片 data URI 清單, 失敗原因)。

    先抽文字；抽不到（掃描檔／純圖排版）就把前幾頁轉成圖片，交給 Gemma 的
    vision 去看。兩條路都走不通才回報失敗。
    """
    import io

    text_parts: list[str] = []
    n_pages = 0
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                reader.decrypt("")   # 有些 PDF 只是空密碼加密
            except Exception:  # noqa: BLE001
                return "", [], "PDF 有密碼保護"
        n_pages = len(reader.pages)
        for i, page in enumerate(reader.pages[:config.MAX_PDF_PAGES]):
            try:
                t = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if t:
                text_parts.append(f"--- 第 {i + 1} 頁 ---\n{t}")
    except Exception as e:  # noqa: BLE001
        logger.warning("pypdf 解析 %s 失敗：%s", name, e)

    text = "\n\n".join(text_parts)
    if len(text) >= config.PDF_MIN_TEXT_CHARS:
        if n_pages > config.MAX_PDF_PAGES:
            text += f"\n\n（這份 PDF 共 {n_pages} 頁，只讀了前 {config.MAX_PDF_PAGES} 頁）"
        return text, [], ""

    # 抽不到足夠文字 → 多半是掃描檔或純圖排版，改用 vision 看前幾頁
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=raw, filetype="pdf")
        uris: list[str] = []
        for page in doc[:config.MAX_PDF_RENDER_PAGES]:
            pix = page.get_pixmap(dpi=config.PDF_RENDER_DPI)
            uris.append("data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode())
        total = doc.page_count
        doc.close()
        if uris:
            note = f"（{name} 看起來是掃描檔或圖片排版，抽不到文字，改用看圖的方式讀前 {len(uris)} 頁"
            note += f"，全文共 {total} 頁）" if total > len(uris) else "）"
            return note, uris, ""
    except Exception as e:  # noqa: BLE001
        logger.warning("PyMuPDF 轉圖 %s 失敗：%s", name, e)

    return "", [], "PDF 讀不出內容（可能是掃描檔或損毀）"


async def collect(message: discord.Message) -> Bundle:
    """讀取訊息裡的附件，回傳整理過的 Bundle。"""
    bundle = Bundle()
    if not message.attachments or not config.ATTACHMENT_ENABLED:
        return bundle

    max_img_bytes = int(config.MAX_IMAGE_MB * 1024 * 1024)
    max_txt_bytes = config.MAX_TEXT_FILE_KB * 1024
    chars_left = config.MAX_TEXT_CHARS_TOTAL

    for att in message.attachments:
        name = att.filename

        if _is_image(att):
            if len(bundle.images) >= config.MAX_IMAGES_PER_MESSAGE:
                bundle.skipped.append((name, f"一次最多看 {config.MAX_IMAGES_PER_MESSAGE} 張圖"))
                continue
            if att.size > max_img_bytes:
                bundle.skipped.append((name, f"圖片超過 {config.MAX_IMAGE_MB:g} MB"))
                continue
            try:
                raw = await att.read()
            except Exception as e:  # noqa: BLE001
                logger.warning("下載圖片失敗 %s: %s", name, e)
                bundle.skipped.append((name, "下載失敗"))
                continue
            mime = att.content_type or f"image/{_ext(name).lstrip('.') or 'png'}"
            mime = mime.split(";")[0].strip()
            uri = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
            bundle.images.append((name, uri))
            continue

        if _is_pdf(att):
            if att.size > config.MAX_PDF_MB * 1024 * 1024:
                bundle.skipped.append((name, f"PDF 超過 {config.MAX_PDF_MB:g} MB"))
                continue
            try:
                raw = await att.read()
            except Exception as e:  # noqa: BLE001
                logger.warning("下載 PDF 失敗 %s: %s", name, e)
                bundle.skipped.append((name, "下載失敗"))
                continue
            # 解析是 CPU 密集的，丟到 thread 免得卡住 event loop
            text, page_images, err = await asyncio.to_thread(_read_pdf, raw, name)
            if err:
                bundle.skipped.append((name, err))
                continue
            if text:
                if len(text) > chars_left:
                    text = text[:chars_left] + f"\n…（內容過長，只讀了前 {chars_left} 字）"
                chars_left -= len(text)
                bundle.texts.append((name, text))
            for idx, uri in enumerate(page_images, 1):
                if len(bundle.images) >= config.MAX_IMAGES_PER_MESSAGE:
                    break
                bundle.images.append((f"{name} 第{idx}頁", uri))
            continue

        if _is_text(att):
            if len(bundle.texts) >= config.MAX_TEXT_FILES_PER_MESSAGE:
                bundle.skipped.append((name, f"一次最多讀 {config.MAX_TEXT_FILES_PER_MESSAGE} 個檔案"))
                continue
            if att.size > max_txt_bytes:
                bundle.skipped.append((name, f"檔案超過 {config.MAX_TEXT_FILE_KB} KB"))
                continue
            if chars_left <= 0:
                bundle.skipped.append((name, "已達單次讀取字數上限"))
                continue
            try:
                raw = await att.read()
            except Exception as e:  # noqa: BLE001
                logger.warning("下載檔案失敗 %s: %s", name, e)
                bundle.skipped.append((name, "下載失敗"))
                continue
            text = _decode(raw)
            if text is None:
                bundle.skipped.append((name, "看起來是二進位檔，讀不了"))
                continue
            if len(text) > chars_left:
                text = text[:chars_left] + f"\n…（檔案過長，只讀了前 {chars_left} 字）"
            chars_left -= len(text)
            bundle.texts.append((name, text))
            continue

        bundle.skipped.append((name, "不支援的檔案格式"))

    if bundle.has_any:
        logger.info("📎 附件 │ %s │ %s", message.author.display_name, bundle.summary())
    return bundle


async def collect_images(message: discord.Message, limit: int) -> list[tuple[str, str]]:
    """只讀某則訊息裡的圖片，回傳 [(檔名, data URI)]。

    給「使用者回覆的那則訊息」用（別人貼了圖，他回覆那張圖問「這是什麼」）。
    刻意不碰 PDF／文字檔 —— 那些要下載＋解析，為了一則被回覆的訊息做太重了。
    """
    out: list[tuple[str, str]] = []
    if not message.attachments or limit <= 0:
        return out

    max_img_bytes = int(config.MAX_IMAGE_MB * 1024 * 1024)
    for att in message.attachments:
        if len(out) >= limit:
            break
        if not _is_image(att) or att.size > max_img_bytes:
            continue
        try:
            raw = await att.read()
        except Exception as e:  # noqa: BLE001
            logger.warning("下載被回覆訊息的圖片失敗 %s: %s", att.filename, e)
            continue
        mime = (att.content_type or f"image/{_ext(att.filename).lstrip('.') or 'png'}")
        mime = mime.split(";")[0].strip()
        out.append((att.filename, f"data:{mime};base64,{base64.b64encode(raw).decode()}"))
    return out


def build_content(text: str, bundle: Bundle) -> str | list[dict]:
    """把使用者文字 + 附件組成要送給模型的 content。

    沒有圖片時回傳字串（維持原本行為）；有圖片時回傳 OpenAI 的 content blocks。
    """
    parts = [text] if text else []

    for name, body in bundle.texts:
        parts.append(f"\n\n===== 檔案：{name} =====\n{body}\n===== 檔案結束 =====")

    if bundle.skipped:
        notes = "、".join(f"{n}（{why}）" for n, why in bundle.skipped)
        parts.append(f"\n\n（以下附件沒能讀取：{notes}）")

    merged = "".join(parts).strip()

    if not bundle.images:
        return merged

    blocks: list[dict] = [{"type": "text", "text": merged or "請看看這張圖片。"}]
    for _name, uri in bundle.images:
        blocks.append({"type": "image_url", "image_url": {"url": uri}})
    return blocks


def strip_images(content: str | list[dict]) -> str:
    """把 content blocks 退化成純文字（圖片送失敗時的降級用）。"""
    if isinstance(content, str):
        return content
    texts = [b.get("text", "") for b in content if b.get("type") == "text"]
    return "\n".join(t for t in texts if t)
