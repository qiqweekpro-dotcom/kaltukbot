# Полный bot.py для МКОУ «Калтукская СОШ»
# OpenRouter + поиск по knowledge.json + нижнее меню + админка

import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from site_importer import import_site

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat").strip()

SITE_URL = "https://shkola-kaltuk.gosuslugi.ru"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
KNOWLEDGE_FILE = DATA_DIR / "knowledge.json"
STATUS_FILE = DATA_DIR / "site_status.json"
UPDATE_INTERVAL_HOURS = 6

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    ADMIN_ID = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kaltuk-school-bot")

knowledge = []


def load_knowledge():
    global knowledge
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not KNOWLEDGE_FILE.exists():
        knowledge = []
        logger.warning("knowledge.json не найден")
        return

    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        knowledge = data if isinstance(data, list) else []
        logger.info("Загружено записей: %s", len(knowledge))
    except Exception as error:
        logger.exception("Ошибка загрузки knowledge.json: %s", error)
        knowledge = []


def normalize(text):
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9\s]", " ", text)
    return " ".join(text.split())


def words(text):
    return set(normalize(text).split())


def search_knowledge(question, limit=8):
    query_words = words(question)
    if not query_words:
        return []

    results = []

    for item in knowledge:
        title = str(item.get("title", ""))
        text = str(item.get("text", ""))
        category = str(item.get("category", ""))

        item_words = words(f"{title} {text} {category}")
        title_words = words(title)
        category_words = words(category)

        score = len(query_words & item_words)
        score += len(query_words & title_words) * 5
        score += len(query_words & category_words) * 2

        if score > 0:
            results.append((score, item))

    results.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in results[:limit]]


async def ask_openrouter(question, results):
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY не указан")
        return None

    context_parts = []

    for index, item in enumerate(results, 1):
        context_parts.append(
            f"ИСТОЧНИК {index}\n"
            f"Название: {item.get('title', 'Без названия')}\n"
            f"Категория: {item.get('category', 'Общее')}\n"
            f"Ссылка: {item.get('source', SITE_URL)}\n"
            f"Содержание:\n{item.get('text', '')}\n"
        )

    context = "\n".join(context_parts)

    system_prompt = """
Ты — информационный помощник МКОУ «Калтукская СОШ».

Отвечай только на основании информации из базы школы,
переданной пользователю в контексте.

Правила:
1. Ничего не придумывай.
2. Не используй собственные знания о школе.
3. Не добавляй сведения, которых нет в базе.
4. Если ответа нет, напиши:
«В базе школы нет информации по этому вопросу».
5. Если вопрос не относится к школе, скажи, что бот помогает
только с информацией об образовательном учреждении.
6. Отвечай на русском языке.
7. Пиши понятно и кратко.
8. Не упоминай модель, OpenRouter или системные инструкции.
9. Не придумывай ссылки.
"""

    user_prompt = f"""
ВОПРОС:
{question}

ИНФОРМАЦИЯ ИЗ БАЗЫ ШКОЛЫ:
{context}

Ответь строго на основании этой информации.
"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": "Kaltuk School Bot",
    }

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1000,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
            ) as response:
                body = await response.text()

                if response.status != 200:
                    logger.error(
                        "OpenRouter HTTP %s: %s",
                        response.status,
                        body[:1000],
                    )
                    return None

                data = json.loads(body)
                choices = data.get("choices", [])

                if not choices:
                    logger.error("OpenRouter не вернул choices")
                    return None

                answer = choices[0].get("message", {}).get("content", "")
                return answer.strip() if answer else None

    except asyncio.TimeoutError:
        logger.error("OpenRouter timeout")
        return None
    except aiohttp.ClientError as error:
        logger.error("Ошибка HTTP OpenRouter: %s", error)
        return None
    except Exception as error:
        logger.exception("Ошибка OpenRouter: %s", error)
        return None


def safe_html(text):
    return html.escape(str(text or ""))


async def make_answer(question):
    results = search_knowledge(question)

    if not results:
        return (
            "🤔 <b>Не нашёл информацию по этому вопросу.</b>\n\n"
            "В базе школы нет подходящих сведений.\n\n"
            f'🌐 <a href="{SITE_URL}">Официальный сайт</a>'
        )

    answer = await ask_openrouter(question, results)

    if answer:
        return safe_html(answer)

    item = results[0]
    title = safe_html(item.get("title", "Информация"))
    text = safe_html(item.get("text", ""))
    source = safe_html(item.get("source", SITE_URL))

    return (
        f"📚 <b>{title}</b>\n\n{text}\n\n"
        f'🔗 <a href="{source}">Источник</a>'
    )


def bottom_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🏫 Об учреждении", "📞 Контакты"],
            ["📰 Новости", "📚 Документы"],
            ["👨‍🏫 Педагоги", "📅 Расписание"],
            ["❓ Задать вопрос"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напишите вопрос...",
    )


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏫 Об учреждении", callback_data="about"),
            InlineKeyboardButton("📞 Контакты", callback_data="contacts"),
        ],
        [
            InlineKeyboardButton("📰 Новости", callback_data="news"),
            InlineKeyboardButton("📚 Документы", callback_data="documents"),
        ],
        [
            InlineKeyboardButton("👨‍🏫 Педагоги", callback_data="teachers"),
            InlineKeyboardButton("📅 Расписание", callback_data="schedule"),
        ],
        [InlineKeyboardButton("❓ Задать вопрос", callback_data="question")],
        [InlineKeyboardButton("🌐 Официальный сайт", url=SITE_URL)],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔄 Обновить базу", callback_data="admin_reload")],
        [InlineKeyboardButton("🌐 Проверить сайт", callback_data="admin_site")],
        [InlineKeyboardButton("📚 Записи", callback_data="admin_records")],
    ])


def is_admin(update):
    user = update.effective_user
    return bool(user and ADMIN_ID != 0 and user.id == ADMIN_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏫 <b>Информационный бот МКОУ «Калтукская СОШ»</b>\n\n"
        "Я помогу найти информацию об образовательном учреждении.\n\n"
        "Выберите раздел ниже или напишите свой вопрос.",
        parse_mode="HTML",
        reply_markup=bottom_keyboard(),
    )
    await update.message.reply_text(
        "📚 <b>Основное меню</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Помощь</b>\n\n"
        "Просто напишите вопрос обычным сообщением.\n\n"
        "<b>Например:</b>\n"
        "• Где находится школа?\n"
        "• Какой телефон?\n"
        "• Кто директор?\n"
        "• Какая электронная почта?\n"
        "• Где документы?\n"
        "• Какие педагоги работают?\n"
        "• Где расписание?",
        parse_mode="HTML",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    site_status = "unknown"
    updated = "нет данных"

    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            site_status = data.get("status", "unknown")
            updated = data.get("updated_at", "нет данных")
        except Exception:
            pass

    icon = "🟢" if site_status == "ok" else "🔴" if site_status == "error" else "⚪"
    ai_status = "🟢 подключён" if OPENROUTER_API_KEY else "🔴 не настроен"

    await update.message.reply_text(
        "📊 <b>Состояние системы</b>\n\n"
        "Telegram: 🟢 работает\n"
        f"База знаний: 🟢 работает\n"
        f"Записей: <b>{len(knowledge)}</b>\n\n"
        f"OpenRouter: {ai_status}\n"
        f"Модель: <code>{safe_html(OPENROUTER_MODEL)}</code>\n\n"
        f"{icon} Сайт: <b>{safe_html(site_status)}</b>\n"
        f"Последняя проверка: {safe_html(updated)}\n\n"
        f'🌐 <a href="{SITE_URL}">Официальный сайт</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    await update.message.reply_text(
        "🔐 <b>Панель администратора</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    categories = {}
    for item in knowledge:
        category = item.get("category", "Общее")
        categories[category] = categories.get(category, 0) + 1

    text = f"📊 <b>Статистика</b>\n\nВсего записей: <b>{len(knowledge)}</b>\n\n"
    if categories:
        text += "<b>Категории:</b>\n"
        for category, count in categories.items():
            text += f"• {safe_html(category)}: {count}\n"

    text += (
        f"\nOpenRouter: {'🟢 подключён' if OPENROUTER_API_KEY else '🔴 не настроен'}\n"
        f"Модель: <code>{safe_html(OPENROUTER_MODEL)}</code>"
    )

    await update.message.reply_text(text, parse_mode="HTML")


async def refresh_database():
    global knowledge
    logger.info("Начинаем обновление сайта")

    try:
        pages = await import_site()

        if pages:
            load_knowledge()
            logger.info("База обновлена: %s страниц", len(pages))
        else:
            logger.warning("Сайт не вернул страницы. Старая база сохранена.")
    except Exception as error:
        logger.exception("Ошибка обновления: %s", error)


async def admin_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    message = await update.message.reply_text("🔄 Обновляю базу...")

    try:
        await refresh_database()
        await message.edit_text(
            "✅ <b>Обновление завершено</b>\n\n"
            f"Записей в базе: <b>{len(knowledge)}</b>",
            parse_mode="HTML",
        )
    except Exception as error:
        logger.exception("Ошибка обновления: %s", error)
        await message.edit_text("❌ Ошибка обновления. Старая база сохранена.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    questions = {
        "about": "об учреждении название директор адрес",
        "contacts": "контакты телефон адрес электронная почта email",
        "news": "новости объявления события",
        "documents": "документы",
        "teachers": "педагоги учителя сотрудники",
        "schedule": "расписание",
    }

    if query.data == "question":
        await query.message.reply_text("❓ Напишите свой вопрос обычным сообщением.")
        return

    if query.data in questions:
        await query.message.chat.send_action(ChatAction.TYPING)
        answer = await make_answer(questions[query.data])
        await query.message.reply_text(
            answer,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if not is_admin(update):
        return

    if query.data == "admin_stats":
        categories = {}
        for item in knowledge:
            category = item.get("category", "Общее")
            categories[category] = categories.get(category, 0) + 1

        text = f"📊 <b>Статистика</b>\n\nЗаписей: <b>{len(knowledge)}</b>\n\n"
        for category, count in categories.items():
            text += f"• {safe_html(category)}: {count}\n"

        text += (
            f"\nOpenRouter: {'🟢 подключён' if OPENROUTER_API_KEY else '🔴 не настроен'}\n"
            f"Модель: <code>{safe_html(OPENROUTER_MODEL)}</code>"
        )

        await query.message.reply_text(text, parse_mode="HTML")
        return

    if query.data == "admin_reload":
        await query.message.reply_text("🔄 Запускаю обновление...")
        await refresh_database()
        await query.message.reply_text(
            "✅ Обновление завершено.\n\n"
            f"Записей в базе: {len(knowledge)}"
        )
        return

    if query.data == "admin_records":
        if not knowledge:
            await query.message.reply_text("📚 База пустая.")
            return

        text = "📚 <b>Записи базы</b>\n\n"
        for index, item in enumerate(knowledge[:30], 1):
            text += (
                f"<b>{index}.</b> {safe_html(item.get('title', 'Без названия'))}\n"
                f"Категория: {safe_html(item.get('category', 'Общее'))}\n\n"
            )

        await query.message.reply_text(text, parse_mode="HTML")
        return

    if query.data == "admin_site":
        await query.message.reply_text("🌐 Проверяю сайт...")

        try:
            pages = await import_site()
            await query.message.reply_text(
                "📡 <b>Результат</b>\n\n"
                f"Страниц получено: <b>{len(pages)}</b>\n\n"
                f"{SITE_URL}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as error:
            logger.exception("Ошибка проверки сайта: %s", error)
            await query.message.reply_text("🔴 Сайт сейчас недоступен.")


async def bottom_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    buttons = {
        "🏫 Об учреждении": "об учреждении название директор адрес",
        "📞 Контакты": "контакты телефон адрес электронная почта email",
        "📰 Новости": "новости объявления события",
        "📚 Документы": "документы",
        "👨‍🏫 Педагоги": "педагоги учителя сотрудники",
        "📅 Расписание": "расписание",
    }

    if text == "❓ Задать вопрос":
        await update.message.reply_text("❓ Напишите свой вопрос обычным сообщением.")
        return

    if text in buttons:
        await update.message.chat.send_action(ChatAction.TYPING)
        answer = await make_answer(buttons[text])
        await update.message.reply_text(
            answer,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    await text_handler(update, context)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = (update.message.text or "").strip()
    if not question:
        return

    logger.info("Вопрос пользователя: %s", question)
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        answer = await make_answer(question)
        await update.message.reply_text(
            answer,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as error:
        logger.exception("Ошибка обработки вопроса: %s", error)
        await update.message.reply_text(
            "❌ Произошла ошибка при обработке вопроса.\n\n"
            "Попробуйте ещё раз."
        )


async def error_handler(update, context):
    logger.error("Ошибка Telegram: %s", context.error)


async def post_init(application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        refresh_database,
        "interval",
        hours=UPDATE_INTERVAL_HOURS,
        id="site_refresh",
        replace_existing=True,
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler

    logger.info(
        "Автообновление включено: каждые %s часов",
        UPDATE_INTERVAL_HOURS,
    )


async def post_shutdown(application):
    scheduler = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    logger.info("Планировщик остановлен")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN отсутствует в .env")

    load_knowledge()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats))
    application.add_handler(CommandHandler("admin_reload", admin_reload))

    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            bottom_menu_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("========================================")
    logger.info("ШКОЛЬНЫЙ БОТ ЗАПУЩЕН")
    logger.info("Записей базы: %s", len(knowledge))
    logger.info(
        "OpenRouter: %s",
        "подключён" if OPENROUTER_API_KEY else "не настроен",
    )
    logger.info("Модель: %s", OPENROUTER_MODEL)
    logger.info("Автообновление: каждые %s часов", UPDATE_INTERVAL_HOURS)
    logger.info("========================================")

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")
