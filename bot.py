import asyncio
import os
import base64
import logging
import threading
from datetime import datetime
from pathlib import Path

import fitz  # pymupdf
import whisper
import ollama as ollama_client
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

VISION_MODEL = os.getenv("VISION_MODEL", "moondream")
VISION_PROMPT = (
    "Describe what you see in one or two sentences in English. "
    "Focus on the type of content (document, receipt, photo, QR code, diagram, etc.) "
    "and its main subject. Do not transcribe text."
)

_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                log.info("Loading Whisper model...")
                _whisper_model = whisper.load_model(os.getenv("WHISPER_MODEL", "base"))
                log.info("Whisper ready")
    return _whisper_model


def transcribe(path: Path) -> str:
    try:
        result = get_whisper().transcribe(str(path), language="ru")
        return result["text"].strip()
    except Exception as e:
        log.warning("Transcription failed: %s", e)
        return ""


def _describe_sync(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    resp = ollama_client.generate(
        model=VISION_MODEL,
        prompt=VISION_PROMPT,
        images=[img_b64],
    )
    return resp["response"].strip()


async def describe_image(image_path: Path) -> str:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _describe_sync, image_path)
    except Exception as e:
        log.warning("Vision failed for %s: %s", image_path, e)
        return ""


def pdf_to_image(pdf_path: Path) -> Path | None:
    try:
        doc = fitz.open(pdf_path)
        page = doc[0]
        pix = page.get_pixmap(dpi=150)
        img_path = pdf_path.with_suffix(".png")
        pix.save(img_path)
        doc.close()
        return img_path
    except Exception as e:
        log.warning("PDF render failed: %s", e)
        return None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
NOTES_DIR = Path(os.getenv("NOTES_DIR", "/home/ann/Obsidian/Входящие"))
ATTACHMENTS_DIR = NOTES_DIR / "attachments"
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", "300"))

CLOSE_WORDS = {
    "все", "всё", "закончил", "закончила", "закончить", "закрыть", "закрой",
    "конец", "стоп", "хватит", "достаточно", "готово", "end", "stop", "done", "finish",
}

sessions: dict[int, dict] = {}
msg_map: dict[int, Path] = {}
MAP_FILE = NOTES_DIR / ".msg_map"


def load_map():
    if MAP_FILE.exists():
        for line in MAP_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2:
                try:
                    msg_map[int(parts[0])] = Path(parts[1])
                except ValueError:
                    pass


def append_map(msg_id: int, path: Path):
    msg_map[msg_id] = path
    with open(MAP_FILE, "a", encoding="utf-8") as f:
        f.write(f"{msg_id}|{path}\n")


def is_close(text: str) -> bool:
    return text.lower().strip().rstrip("!.,…") in CLOSE_WORDS


def new_filepath() -> Path:
    ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    return NOTES_DIR / f"{ts}.md"


def open_session(user_id: int) -> dict:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = new_filepath()
    f = open(path, "a", encoding="utf-8")
    f.write(f"# {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
    f.flush()
    session = {"path": path, "file": f}
    sessions[user_id] = session
    log.info("Opened %s", path)
    return session


def close_session(user_id: int, context: ContextTypes.DEFAULT_TYPE | None = None):
    session = sessions.pop(user_id, None)
    if not session:
        return
    session["file"].close()
    log.info("Closed %s", session["path"])
    if context:
        _cancel_timeout(user_id, context)


def _cancel_timeout(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    for job in context.job_queue.get_jobs_by_name(f"to_{user_id}"):
        job.schedule_removal()


def _schedule_timeout(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    _cancel_timeout(user_id, context)

    async def _job(ctx: ContextTypes.DEFAULT_TYPE):
        close_session(ctx.job.data)

    context.job_queue.run_once(_job, SESSION_TIMEOUT, data=user_id, name=f"to_{user_id}")


async def download_file(context: ContextTypes.DEFAULT_TYPE, file_id: str, filename: str) -> Path:
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ATTACHMENTS_DIR / filename
    # avoid overwrite
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        dest = ATTACHMENTS_DIR / f"{stem}_{int(datetime.now().timestamp())}{suffix}"
    tg_file = await context.bot.get_file(file_id)
    await tg_file.download_to_drive(dest)
    return dest


async def build_content(message, context: ContextTypes.DEFAULT_TYPE) -> str:
    parts = []
    text = message.text or message.caption or ""
    if text:
        parts.append(text)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if message.photo:
        photo = message.photo[-1]
        path = await download_file(context, photo.file_id, f"photo-{ts}.jpg")
        desc = await describe_image(path)
        parts.append(f"![[{path.name}]]")
        if desc:
            parts.append(f"👁 {desc}")

    if message.voice:
        path = await download_file(context, message.voice.file_id, f"voice-{ts}.ogg")
        transcript = transcribe(path)
        parts.append(f"![[{path.name}]]")
        if transcript:
            parts.append(f"🗣 {transcript}")

    if message.video:
        path = await download_file(context, message.video.file_id, f"video-{ts}.mp4")
        parts.append(f"![[{path.name}]]")

    if message.document:
        orig = message.document.file_name or f"file-{ts}"
        path = await download_file(context, message.document.file_id, orig)
        parts.append(f"![[{path.name}]]")
        mime = message.document.mime_type or ""
        if mime.startswith("image/"):
            desc = await describe_image(path)
            if desc:
                parts.append(f"👁 {desc}")
        elif mime == "application/pdf":
            img = pdf_to_image(path)
            if img:
                desc = describe_image(img)
                img.unlink(missing_ok=True)
                if desc:
                    parts.append(f"👁 {desc}")

    if message.audio:
        orig = message.audio.file_name or f"audio-{ts}.mp3"
        path = await download_file(context, message.audio.file_id, orig)
        parts.append(f"![[{path.name}]]")

    if message.sticker:
        parts.append(f"[стикер: {message.sticker.emoji or ''}]")

    if message.location:
        loc = message.location
        parts.append(f"📍 [{loc.latitude}, {loc.longitude}]")

    return "\n".join(parts) if parts else "[медиа]"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    content = await build_content(message, context)
    if not content.strip():
        return

    # Reply to a known message → append to original file
    if message.reply_to_message:
        rid = message.reply_to_message.message_id
        target = msg_map.get(rid)
        if target and target.exists():
            ts = datetime.now().strftime("%H:%M")
            with open(target, "a", encoding="utf-8") as f:
                f.write(f"\n> **[{ts}] Ответ:** {content}\n")
            append_map(message.message_id, target)
            return

    # Close command
    if is_close(content):
        if user_id in sessions:
            close_session(user_id, context)
        return

    # Get or create session
    session = sessions.get(user_id) or open_session(user_id)
    _schedule_timeout(user_id, context)

    ts = datetime.now().strftime("%H:%M")
    session["file"].write(f"[{ts}] {content}\n")
    session["file"].flush()
    append_map(message.message_id, session["path"])


def main():
    load_map()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(
        filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.ALL |
        filters.VOICE | filters.VIDEO | filters.AUDIO | filters.Sticker.ALL | filters.LOCATION,
        handle_message,
    ))
    log.info("Secretary bot started. Notes dir: %s", NOTES_DIR)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
