
import asyncio
import io
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from openai import AsyncOpenAI
from docx import Document

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quizbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
PORT = int(os.getenv("PORT", "10000"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
DB_PATH = os.getenv("DB_PATH", "quizbot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable is required")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID environment variable is required")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError("ADMIN_ID must be a Telegram numeric user ID")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
# Disable the SDK's built-in retries. We handle retries ourselves so one 429
# cannot silently turn into several extra requests and make the RPM limit worse.
OPENAI_TIMEOUT = max(15.0, float(os.getenv("OPENAI_TIMEOUT", "90")))
OPENAI_MAX_RETRIES = max(0, min(5, int(os.getenv("OPENAI_MAX_RETRIES", "3"))))
OPENAI_MAX_RETRY_WAIT = max(5.0, float(os.getenv("OPENAI_MAX_RETRY_WAIT", "75")))
ai = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    max_retries=0,
    timeout=OPENAI_TIMEOUT,
)

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")

db.execute("""
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    duration_sec INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_index INTEGER NOT NULL,
    FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    score INTEGER DEFAULT 0,
    total INTEGER NOT NULL,
    UNIQUE(test_id, user_id, started_at),
    FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS answers (
    attempt_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    selected_index INTEGER,
    correct INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(attempt_id, question_id),
    FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
    FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
)
""")
db.commit()

# Temporary admin creation flow. Test data is persisted only after creation.
admin_flow = {}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def now() -> int:
    return int(time.time())


def parse_duration(text: str) -> Optional[int]:
    """Parse any positive duration. Bare numbers are minutes for compatibility.
    Explicit units support 1 second and arbitrarily large values.
    """
    text = text.strip().lower()
    if not text:
        return None

    # Bare number = minutes (legacy/Telegram-QuizBot-like convenience).
    if text.isdigit():
        n = int(text)
        return n * 60 if n >= 1 else None

    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|soniya|sekund|m|min|mins|daqiqa|h|hr|soat|d|kun)", text)
    if not m:
        return None
    n = int(m.group(1))
    if n < 1:
        return None
    unit = m.group(2)
    if unit in {"s", "sec", "secs", "soniya", "sekund"}:
        return n
    if unit in {"m", "min", "mins", "daqiqa"}:
        return n * 60
    if unit in {"h", "hr", "soat"}:
        return n * 3600
    return n * 86400


def format_duration(sec: int) -> str:
    sec = max(1, int(sec))
    if sec < 60:
        return f"{sec} soniya"
    if sec % 3600 == 0:
        return f"{sec // 3600} soat"
    if sec % 60 == 0:
        return f"{sec // 60} daqiqa"
    return f"{sec // 60} daqiqa {sec % 60} soniya"


def clean_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t]+", " ", s).strip()


def normalize_question_blocks(text: str) -> list[str]:
    text = text.replace("\ufeff", "")
    # The supplied format uses long lines of = and + as separators.
    text = re.sub(r"(?m)^[ \t]*[=+_\-]{3,}[ \t]*$", "\n---SEPARATOR---\n", text)
    blocks = re.split(r"\n\s*---SEPARATOR---\s*\n", text)
    return [b.strip() for b in blocks if b.strip()]


def parse_block(block: str) -> Optional[tuple[str, list[str]]]:
    lines = [x.strip() for x in block.splitlines() if x.strip()]
    if len(lines) < 5:
        return None

    # Remove optional question ID line.
    if re.fullmatch(r'id\s*=\s*["\']?[^"\']+["\']?', lines[0], re.I):
        lines = lines[1:]
    if len(lines) < 5:
        return None

    question = lines[0]
    options = lines[1:]

    # Remove common decorative separator fragments.
    options = [
        re.sub(r"^[=+_\-]{3,}$", "", x).strip()
        for x in options
    ]
    options = [x for x in options if x]

    # Common MCQ labels are allowed, but labels are removed from the stored option.
    cleaned = []
    for opt in options:
        opt = re.sub(r"^(?:[A-Da-d]|[1-9]\d?)\s*[\)\.\:\-]\s*", "", opt)
        cleaned.append(opt.strip())

    if 2 <= len(cleaned) <= 8:
        return question, cleaned
    return None


def parse_txt(text: str) -> list[tuple[str, list[str]]]:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")

    # The format in the user's sample uses a +++++ line to finish a question.
    # '=' lines are visual separators between the question/options and are ignored.
    blocks = re.split(r"(?m)^\s*\+{3,}\s*$", text)
    parsed: list[tuple[str, list[str]]] = []

    for block in blocks:
        lines = []
        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                continue
            if re.fullmatch(r"[=\-]{3,}", line):
                continue
            if re.fullmatch(r"\+{3,}", line):
                continue
            lines.append(line)

        if len(lines) < 3:
            continue

        if re.fullmatch(r'id\s*=\s*["\']?[^"\']+["\']?', lines[0], re.I):
            lines = lines[1:]
        if len(lines) < 3:
            continue

        question = lines[0]
        options = []
        for opt in lines[1:]:
            opt = re.sub(r"^(?:[A-Da-d]|[1-9]\d?)\s*[\)\.\:\-]\s*", "", opt)
            opt = opt.strip()
            if opt:
                options.append(opt)

        if 2 <= len(options) <= 8:
            parsed.append((question, options))

    # Fallback for simpler files with blank-line-separated questions.
    if not parsed:
        for chunk in re.split(r"\n\s*\n", text):
            item = parse_block(chunk)
            if item:
                parsed.append(item)

    return parsed


def parse_docx(path: str) -> list[tuple[str, list[str]]]:
    doc = Document(path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return parse_txt("\n".join(paragraphs))


class AIRequestLimiter:
    """Pace API calls so the bot does not flood low-RPM API projects."""
    def __init__(self, min_interval: float = 1.25):
        self.min_interval = max(0.0, min_interval)
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def wait_turn(self):
        async with self.lock:
            wait = self.min_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request = time.monotonic()


# A batch dramatically reduces API requests: 60 questions ~= 3 requests instead of 60.
# Set OPENAI_BATCH_SIZE in Render if desired. 40 is a good balance for normal MCQs.
OPENAI_BATCH_SIZE = max(1, min(50, int(os.getenv("OPENAI_BATCH_SIZE", "40"))))
OPENAI_MIN_REQUEST_INTERVAL = max(0.0, float(os.getenv("OPENAI_MIN_REQUEST_INTERVAL", "1.3")))
ai_limiter = AIRequestLimiter(OPENAI_MIN_REQUEST_INTERVAL)


def retry_after_seconds(exc: Exception) -> float | None:
    """Extract a useful retry delay from OpenAI error text when present."""
    text = str(exc)
    # Examples: "try again in 6s", "try again in 28m48s", "retry after 60 seconds"
    m = re.search(r"try again in\s+(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", text, re.I)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
    m = re.search(r"retry[- ]after[:=]?\s*(\d+(?:\.\d+)?)", text, re.I)
    if m:
        return float(m.group(1))
    return None


def is_transient_openai_error(exc: Exception) -> bool:
    """Return True only for errors worth retrying automatically."""
    text = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return True
    return any(x in text for x in (
        "429", "rate limit", "rate_limit", "too many requests",
        "500", "502", "503", "504", "timeout", "timed out",
        "temporarily unavailable", "connection error"
    ))


async def ai_answer_batch(items: list[tuple[str, list[str]]]) -> list[int]:
    """Ask the model for many answers in one request.

    Output must contain exactly one integer per question, in order. This is much
    more tolerant of low RPM limits than making one request per question.
    """
    blocks = []
    for n, (question, options) in enumerate(items, 1):
        numbered = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(options))
        blocks.append(f"QUESTION {n}:\n{question}\nOPTIONS:\n{numbered}")
    prompt = """Solve these multiple-choice questions.
Return ONLY the correct option number for each question, one integer per line, in exactly the same order.
Do not explain anything. Do not add numbering, bullets, or extra text.
If a question has N options, its answer must be between 1 and N.

""" + "\n\n---\n\n".join(blocks)

    response = await ai.responses.create(model=OPENAI_MODEL, input=prompt)
    text = (response.output_text or "").strip()
    answers = []
    for line in text.splitlines():
        m = re.fullmatch(r"\s*(?:answer\s*)?([1-8])\s*[.)]?\s*", line, re.I)
        if m:
            answers.append(int(m.group(1)) - 1)
    if len(answers) != len(items):
        nums = re.findall(r"(?<!\d)([1-8])(?!\d)", text)
        if len(nums) == len(items):
            answers = [int(x) - 1 for x in nums]
    if len(answers) != len(items):
        raise ValueError(
            f"AI returned {len(answers)} answers for {len(items)} questions: {text[:500]!r}"
        )
    for i, answer in enumerate(answers):
        if answer < 0 or answer >= len(items[i][1]):
            raise ValueError(
                f"AI answer {answer + 1} is outside option range for question {i + 1}"
            )
    return answers


async def solve_questions(
    items: list[tuple[str, list[str]]],
    progress_message: Optional[Message] = None,
) -> list[tuple[str, list[str], int]]:
    """Solve questions in batches without getting stuck forever on 429.

    Important behavior:
    - One API request handles many questions (default batch size: 40).
    - The OpenAI SDK's hidden retries are disabled, so we control request count.
    - Short temporary 429/5xx/timeouts are retried a few times.
    - If OpenAI asks us to wait longer than OPENAI_MAX_RETRY_WAIT, we STOP instead
      of leaving the Telegram handler hanging for 20-30+ minutes.
    """
    solved: list[tuple[str, list[str], int]] = []
    total = len(items)

    for start in range(0, total, OPENAI_BATCH_SIZE):
        batch = items[start:start + OPENAI_BATCH_SIZE]
        batch_start = start + 1
        last_exc: Exception | None = None

        for attempt in range(OPENAI_MAX_RETRIES + 1):
            try:
                await ai_limiter.wait_turn()
                indices = await ai_answer_batch(batch)
                solved.extend(
                    (q, opts, idx) for (q, opts), idx in zip(batch, indices)
                )
                done = min(start + len(batch), total)
                if progress_message:
                    try:
                        await progress_message.edit_text(
                            f"🧠 AI javoblarni aniqlayapti... <b>{done}/{total}</b>\n"
                            f"📦 {len(batch)} ta savol bitta AI so‘rovda tekshirildi.\n"
                            f"⏳ Botni yopmang."
                        )
                    except Exception:
                        pass
                break

            except Exception as exc:
                last_exc = exc
                if not is_transient_openai_error(exc):
                    raise

                suggested = retry_after_seconds(exc)
                delay = suggested if suggested is not None else min(
                    30.0, 3.0 * (2 ** min(attempt, 4))
                )

                # Never sit silently in Telegram for a long server-provided delay.
                if delay > OPENAI_MAX_RETRY_WAIT or attempt >= OPENAI_MAX_RETRIES:
                    raise RuntimeError(
                        "OpenAI hozircha so‘rovni qabul qilmayapti. "
                        f"Limit/reset vaqti: taxminan {format_wait(delay)}. "
                        "Bot cheksiz kutib qolmasligi uchun jarayon to‘xtatildi. "
                        "Bir ozdan keyin testni qayta yarating."
                    ) from exc

                if progress_message:
                    try:
                        await progress_message.edit_text(
                            f"⏳ <b>OpenAI vaqtinchalik limit berdi.</b>\n"
                            f"📦 Hozirgi qism: {batch_start}-{start + len(batch)} / {total}\n"
                            f"🔄 {format_wait(delay)} dan keyin avtomatik qayta urinaman...\n"
                            f"🔢 Urinish: {attempt + 1}/{OPENAI_MAX_RETRIES + 1}"
                        )
                    except Exception:
                        pass

                log.warning(
                    "AI transient error; retry %s/%s after %.1fs: %s",
                    attempt + 1, OPENAI_MAX_RETRIES + 1, delay, exc,
                )
                await asyncio.sleep(delay)
        else:
            raise RuntimeError(f"AI javoblarini aniqlash muvaffaqiyatsiz: {last_exc}")

    return solved


def format_wait(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f"{minutes} daqiqa {secs} soniya"
    return f"{secs} soniya"

def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c) for t, c in row]
        for row in rows
    ])


def admin_menu():
    return kb(
        (("📄 Fayldan test yaratish", "admin:create"),),
        (("📚 Testlarim", "admin:tests"),),
        (("🆔 Mening ID raqamim", "admin:myid"),),
        (("ℹ️ Yordam", "admin:help"),),
    )


def test_link(public_id: str, username: str) -> str:
    return f"https://t.me/{username}?start=test_{public_id}"


async def bot_username() -> str:
    me = await bot.get_me()
    return me.username


async def health(request):
    return web.json_response({"status": "ok", "bot": "running"})


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server listening on 0.0.0.0:%s", PORT)
    return runner


async def begin_new_quiz(user_id: int, message: Message):
    if not is_admin(user_id):
        await message.answer("❌ Faqat admin yangi test yarata oladi.")
        return
    admin_flow[user_id] = {"step": "file"}
    await message.answer(
        "📄 <b>Faylni yuboring</b>\n\n"
        "Qabul qilinadi: <code>.txt</code> yoki <code>.docx</code>.\n"
        "Fayldagi savol va variantlar o‘zgartirilmaydi."
    )


@dp.message(Command("newquiz"))
async def cmd_newquiz(message: Message):
    await begin_new_quiz(message.from_user.id, message)


@dp.message(Command("quizzes"))
async def cmd_quizzes(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("👋 Sizda hali testlar ro‘yxati yo‘q. Test havolasi orqali testni boshlashingiz mumkin.")
        return
    await show_admin_tests(message)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "📖 <b>Buyruqlar</b>\n\n"
            "/newquiz — yangi test yaratish\n"
            "/quizzes — testlaringiz\n"
            "/stop — faol testni to‘xtatish\n"
            "/lang — tilni tanlash\n"
            "/myid — Telegram ID\n"
            "/help — yordam"
        )
    else:
        await message.answer("📖 /start — testni ochish\n/stop — faol testni to‘xtatish\n/lang — til")


@dp.message(Command("lang"))
async def cmd_lang(message: Message):
    await message.answer("🌐 Hozircha asosiy til: <b>O‘zbekcha</b>.\nKo‘p tillilik keyingi bosqichda qo‘shilishi mumkin.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    row = db.execute(
        "SELECT a.id, t.title FROM attempts a JOIN tests t ON t.id=a.test_id "
        "WHERE a.user_id=? AND a.finished_at IS NULL ORDER BY a.id DESC LIMIT 1",
        (message.from_user.id,)
    ).fetchone()
    if not row:
        await message.answer("ℹ️ Faol test topilmadi.")
        return
    await finish_attempt(message.from_user.id, row["id"])
    await message.answer("🛑 Test to‘xtatildi.")


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"🆔 Sizning Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("start"))
async def start(message: Message, command: CommandObject):
    args = command.args or ""
    if args.startswith("test_"):
        public_id = args[5:]
        row = db.execute("SELECT * FROM tests WHERE public_id=?", (public_id,)).fetchone()
        if not row:
            await message.answer("❌ Test topilmadi yoki o‘chirilgan.")
            return
        total = db.execute("SELECT COUNT(*) c FROM questions WHERE test_id=?", (row["id"],)).fetchone()["c"]
        duration_text = format_duration(row["duration_sec"])
        await message.answer(
            f"📚 <b>{row['title']}</b>\n\n"
            f"❓ Savollar: <b>{total}</b>\n"
            f"⏱ Vaqt: <b>{duration_text}</b>\n\n"
            "Boshlash uchun tugmani bosing.",
            reply_markup=kb((("🚀 TESTNI BOSHLASH", f"quiz:start:{public_id}"),))
        )
        return

    if is_admin(message.from_user.id):
        await message.answer(
            "👑 <b>Quiz Bot Admin Panel</b>\n\n"
            "Bu botda testlarni faqat admin yaratadi.",
            reply_markup=admin_menu()
        )
    else:
        await message.answer("👋 Test linkini ochib, testni boshlashingiz mumkin.")


@dp.callback_query(F.data == "admin:myid")
async def admin_myid(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q", show_alert=True)
        return
    await call.message.answer(f"🆔 Admin ID: <code>{call.from_user.id}</code>")
    await call.answer()


@dp.callback_query(F.data == "admin:help")
async def admin_help(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q", show_alert=True)
        return
    await call.message.answer(
        "📖 <b>Admin buyruqlari</b>\n\n"
        "/newquiz — yangi test yaratish\n"
        "/quizzes — barcha testlar\n"
        "/stop — faol testni to‘xtatish\n"
        "/lang — til\n"
        "/myid — Telegram ID\n"
        "/help — yordam"
    )
    await call.answer()


@dp.callback_query(F.data == "admin:create")
async def admin_create(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q", show_alert=True)
        return
    await begin_new_quiz(call.from_user.id, call.message)
    await call.answer()


@dp.message(F.document)
async def document_handler(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Sizga test yaratish ruxsati berilmagan.")
        return
    flow = admin_flow.get(message.from_user.id)
    if not flow or flow.get("step") != "file":
        await message.answer("Avval 👑 Admin paneldan «Fayldan test yaratish»ni tanlang.")
        return

    name = (message.document.file_name or "").lower()
    if not (name.endswith(".txt") or name.endswith(".docx")):
        await message.answer("❌ Faqat .txt yoki .docx fayl yuboring.")
        return

    tmp = Path("/tmp") / f"quiz_{uuid.uuid4().hex}{Path(name).suffix}"
    try:
        tg_file = await bot.get_file(message.document.file_id)
        await bot.download(tg_file, destination=tmp)

        if name.endswith(".docx"):
            items = parse_docx(str(tmp))
        else:
            text = tmp.read_text(encoding="utf-8-sig", errors="replace")
            items = parse_txt(text)

        if not items:
            await message.answer(
                "❌ Fayldan savollar topilmadi.\n\n"
                "Format rasmda yuborganingizdek savol + variantlar + separator bo‘lishi kerak."
            )
            return

        flow.update({
            "step": "count",
            "items": items,
            "filename": message.document.file_name,
        })
        await message.answer(
            f"✅ Fayl o‘qildi.\n"
            f"📚 Topilgan savollar: <b>{len(items)}</b>\n\n"
            "Bir test nechta savoldan iborat bo‘lsin?\n"
            "Masalan: <code>50</code>"
        )
    except Exception:
        log.exception("File parsing failed")
        await message.answer("❌ Faylni o‘qishda xatolik yuz berdi.")
    finally:
        tmp.unlink(missing_ok=True)


@dp.message(F.text)
async def text_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    flow = admin_flow.get(message.from_user.id)
    if not flow:
        return

    if flow.get("step") == "count":
        if not message.text.isdigit():
            await message.answer("❌ Faqat son kiriting. Masalan: <code>50</code>")
            return
        count = int(message.text)
        total = len(flow["items"])
        if count < 1 or count > total:
            await message.answer(f"❌ 1 dan {total} gacha son kiriting.")
            return
        flow["chunk"] = count
        flow["step"] = "duration"
        await message.answer(
            "⏱ <b>Test vaqtini kiriting</b>.\n\n"
            "<code>1s</code> — 1 sekund\n"
            "<code>30s</code> — 30 sekund\n"
            "<code>60s</code> — 60 sekund\n"
            "<code>5m</code> — 5 daqiqa\n"
            "<code>1h</code> — 1 soat\n"
            "Istalgan katta <code>Ns</code> ham qabul qilinadi.\n"
            "Faqat son kiritsangiz, daqiqa deb olinadi."
        )
        return

    if flow.get("step") == "duration":
        duration = parse_duration(message.text)
        if duration is None:
            await message.answer("❌ Vaqt noto‘g‘ri. Masalan: <code>1s</code>, <code>30s</code>, <code>5m</code> yoki <code>1h</code>.")
            return

        flow["duration"] = duration
        flow["step"] = "title"
        await message.answer("📝 Test nomini yuboring.\nMasalan: <code>Kimyo — 1-qism</code>")
        return

    if flow.get("step") == "title":
        title = message.text.strip()[:200]
        if not title:
            await message.answer("❌ Test nomi bo‘sh bo‘lmasin.")
            return

        items = flow["items"]
        chunk = flow["chunk"]
        duration = flow["duration"]
        await message.answer(
            f"🧠 {len(items)} ta savol uchun AI javoblarni aniqlayapti.\n"
            "Bu biroz vaqt olishi mumkin. Botni yopmang."
        )

        try:
            solved = await solve_questions(items, progress_message=message)
        except Exception as exc:
            log.exception("AI solving failed")
            await message.answer(
                "❌ <b>AI javoblarni aniqlashda muammo yuz berdi.</b>\n\n"
                "Agar OpenAI limiti bo‘lsa, bot endi cheksiz kutib qolmaydi. "
                "Bir ozdan keyin qayta urinib ko‘ring.\n\n"
                f"<code>{str(exc)[:500]}</code>"
            )
            admin_flow.pop(message.from_user.id, None)
            return

        tests_created = []
        for start in range(0, len(solved), chunk):
            part = solved[start:start + chunk]
            part_title = title if len(solved) <= chunk else f"{title} — {start // chunk + 1}-qism"
            public_id = uuid.uuid4().hex[:10]
            cur = db.execute(
                "INSERT INTO tests(public_id,title,duration_sec,created_by,created_at) VALUES(?,?,?,?,?)",
                (public_id, part_title, duration, ADMIN_ID, now())
            )
            test_id = cur.lastrowid

            for pos, (q, opts, correct) in enumerate(part, 1):
                import json
                db.execute(
                    "INSERT INTO questions(test_id,position,question,options_json,correct_index) VALUES(?,?,?,?,?)",
                    (test_id, pos, q, json.dumps(opts, ensure_ascii=False), correct)
                )
            db.commit()
            tests_created.append((part_title, public_id, len(part)))

        admin_flow.pop(message.from_user.id, None)
        username = await bot_username()
        text = ["✅ <b>Testlar tayyor!</b>", ""]
        for t, pid, n in tests_created:
            text.append(f"📚 <b>{t}</b> — {n} ta savol")
            text.append(test_link(pid, username))
            text.append("")
        await message.answer("\n".join(text))
        return


async def show_admin_tests(message: Message):
    rows = db.execute(
        "SELECT * FROM tests WHERE created_by=? ORDER BY id DESC LIMIT 30",
        (ADMIN_ID,)
    ).fetchall()
    if not rows:
        await message.answer("📚 Hali testlar yo‘q.")
        return
    username = await bot_username()
    lines = ["📚 <b>Testlaringiz</b>\n"]
    for r in rows:
        total = db.execute("SELECT COUNT(*) c FROM questions WHERE test_id=?", (r["id"],)).fetchone()["c"]
        attempts = db.execute("SELECT COUNT(*) c FROM attempts WHERE test_id=?", (r["id"],)).fetchone()["c"]
        lines.append(f"• <b>{r['title']}</b> — {total} ta savol — {attempts} ta urinish")
        lines.append(test_link(r["public_id"], username))
    await message.answer("\n".join(lines))


@dp.callback_query(F.data == "admin:tests")
async def admin_tests(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q", show_alert=True)
        return
    await show_admin_tests(call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("quiz:start:"))
async def quiz_start(call: CallbackQuery):
    public_id = call.data.split(":", 2)[2]
    test = db.execute("SELECT * FROM tests WHERE public_id=?", (public_id,)).fetchone()
    if not test:
        await call.answer("Test topilmadi", show_alert=True)
        return

    total = db.execute("SELECT COUNT(*) c FROM questions WHERE test_id=?", (test["id"],)).fetchone()["c"]
    # One user can start multiple attempts; each attempt has its own started_at.
    cur = db.execute(
        "INSERT INTO attempts(test_id,user_id,username,full_name,started_at,total) VALUES(?,?,?,?,?,?)",
        (
            test["id"],
            call.from_user.id,
            call.from_user.username,
            call.from_user.full_name,
            now(),
            total,
        )
    )
    db.commit()
    attempt_id = cur.lastrowid

    await call.message.edit_text(
        f"🚀 <b>{test['title']}</b>\n\n"
        f"❓ {total} ta savol\n"
        f"⏱ {format_duration(test['duration_sec'])}\n\n"
        "Test boshlandi!"
    )
    await send_question(call.from_user.id, attempt_id, 1)
    await call.answer()


async def send_question(user_id: int, attempt_id: int, position: int):
    row = db.execute("""
        SELECT q.*, a.started_at, a.total, t.duration_sec, t.title
        FROM questions q
        JOIN attempts a ON a.test_id=q.test_id
        JOIN tests t ON t.id=q.test_id
        WHERE a.id=? AND q.position=?
    """, (attempt_id, position)).fetchone()

    if not row:
        await finish_attempt(user_id, attempt_id)
        return

    import json
    options = json.loads(row["options_json"])
    buttons = [
        [InlineKeyboardButton(text=f"{i+1}. {opt[:80]}", callback_data=f"ans:{attempt_id}:{row['id']}:{i}")]
        for i, opt in enumerate(options)
    ]
    buttons.append([InlineKeyboardButton(text="🏁 Testni tugatish", callback_data=f"finish:{attempt_id}")])

    elapsed = now() - row["started_at"]
    left = row["duration_sec"] - elapsed
    if left <= 0:
        await finish_attempt(user_id, attempt_id)
        return

    mins, secs = divmod(left, 60)
    await bot.send_message(
        user_id,
        f"📝 <b>{row['title']}</b>\n"
        f"❓ <b>{position}/{row['total']}</b>\n"
        f"⏱ Qolgan vaqt: <b>{mins:02d}:{secs:02d}</b>\n\n"
        f"{row['question']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("ans:"))
async def answer_question(call: CallbackQuery):
    try:
        _, attempt_s, question_s, selected_s = call.data.split(":")
        attempt_id = int(attempt_s)
        question_id = int(question_s)
        selected = int(selected_s)
    except ValueError:
        await call.answer("Noto‘g‘ri javob", show_alert=True)
        return

    attempt = db.execute("""
        SELECT a.*, t.duration_sec
        FROM attempts a JOIN tests t ON t.id=a.test_id
        WHERE a.id=? AND a.user_id=? AND a.finished_at IS NULL
    """, (attempt_id, call.from_user.id)).fetchone()
    if not attempt:
        await call.answer("Bu test yopilgan.", show_alert=True)
        return

    if now() - attempt["started_at"] >= attempt["duration_sec"]:
        await finish_attempt(call.from_user.id, attempt_id)
        await call.answer("⏰ Vaqt tugagan.", show_alert=True)
        return

    q = db.execute("SELECT * FROM questions WHERE id=? AND test_id=?", (question_id, attempt["test_id"])).fetchone()
    if not q:
        await call.answer("Savol topilmadi.", show_alert=True)
        return

    correct = int(selected == q["correct_index"])
    db.execute(
        "INSERT OR REPLACE INTO answers(attempt_id,question_id,selected_index,correct) VALUES(?,?,?,?)",
        (attempt_id, question_id, selected, correct)
    )
    db.commit()

    answered = db.execute("SELECT COUNT(*) c FROM answers WHERE attempt_id=?", (attempt_id,)).fetchone()["c"]
    await call.answer("✅" if correct else "❌")
    await call.message.edit_reply_markup(reply_markup=None)

    if answered >= attempt["total"]:
        await finish_attempt(call.from_user.id, attempt_id)
    else:
        await send_question(call.from_user.id, attempt_id, answered + 1)


@dp.callback_query(F.data.startswith("finish:"))
async def finish_callback(call: CallbackQuery):
    attempt_id = int(call.data.split(":")[1])
    if not is_attempt_owner(attempt_id, call.from_user.id):
        await call.answer("Ruxsat yo‘q", show_alert=True)
        return
    await finish_attempt(call.from_user.id, attempt_id)
    await call.answer()


def is_attempt_owner(attempt_id: int, user_id: int) -> bool:
    row = db.execute("SELECT user_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    return bool(row and row["user_id"] == user_id)


async def finish_attempt(user_id: int, attempt_id: int, reason: str = "manual"):
    attempt = db.execute("""
        SELECT a.*, t.title FROM attempts a JOIN tests t ON t.id=a.test_id
        WHERE a.id=? AND a.user_id=? AND a.finished_at IS NULL
    """, (attempt_id, user_id)).fetchone()
    if not attempt:
        return

    score = db.execute(
        "SELECT COALESCE(SUM(correct),0) s FROM answers WHERE attempt_id=?",
        (attempt_id,)
    ).fetchone()["s"]
    answered = db.execute(
        "SELECT COUNT(*) c FROM answers WHERE attempt_id=?",
        (attempt_id,)
    ).fetchone()["c"]

    db.execute(
        "UPDATE attempts SET finished_at=?,score=? WHERE id=?",
        (now(), score, attempt_id)
    )
    db.commit()

    percent = round(score / attempt["total"] * 100, 1) if attempt["total"] else 0
    await bot.send_message(
        user_id,
        f"{'⏰ <b>VAQT TUGADI</b>' if reason == 'timeout' else '🏁 <b>TEST YAKUNLANDI</b>'}\n\n"
        f"📚 {attempt['title']}\n"
        f"✅ To‘g‘ri: <b>{score}</b>\n"
        f"❌ Noto‘g‘ri/bo‘sh: <b>{attempt['total'] - score}</b>\n"
        f"📊 Natija: <b>{percent}%</b>\n"
        f"📝 Javob berilgan: {answered}/{attempt['total']}"
    )


async def expire_attempts_loop():
    """Finish timed-out attempts even if the user sends no further message."""
    while True:
        try:
            rows = db.execute(
                "SELECT a.id, a.user_id FROM attempts a JOIN tests t ON t.id=a.test_id "
                "WHERE a.finished_at IS NULL AND (? - a.started_at) >= t.duration_sec",
                (now(),)
            ).fetchall()
            for row in rows:
                await finish_attempt(row["user_id"], row["id"], reason="timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Attempt expiry loop failed")
        await asyncio.sleep(1)


async def main():
    runner = await start_web_server()
    expiry_task = asyncio.create_task(expire_attempts_loop())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands([
            BotCommand(command="start", description="Botni boshlash"),
            BotCommand(command="newquiz", description="Yangi test yaratish"),
            BotCommand(command="quizzes", description="Testlarim"),
            BotCommand(command="stop", description="Faol testni to‘xtatish"),
            BotCommand(command="lang", description="Tilni tanlash"),
            BotCommand(command="help", description="Yordam"),
            BotCommand(command="myid", description="Telegram ID"),
        ])
        log.info("Starting polling")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            tasks_concurrency_limit=20,
        )
    finally:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        await runner.cleanup()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
