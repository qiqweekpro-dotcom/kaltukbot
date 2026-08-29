import html
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from site_importer import import_site


# ============================================================
# НАСТРОЙКИ
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY",
    "",
).strip()

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "deepseek/deepseek-chat",
).strip()

SITE_URL = "https://shkola-kaltuk.gosuslugi.ru"

OPENROUTER_URL = (
    "https://openrouter.ai/api/v1/chat/completions"
)

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"

KNOWLEDGE_FILE = DATA_DIR / "knowledge.json"

STATUS_FILE = DATA_DIR / "site_status.json"

# Интервал автоматического обновления базы.
# 6 часов = 21600 секунд.
UPDATE_INTERVAL_SECONDS = 6 * 60 * 60


try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    ADMIN_ID = 0


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger("school-bot")


# ============================================================
# БАЗА ЗНАНИЙ
# ============================================================

knowledge = []

knowledge_lock = threading.Lock()


def load_knowledge():
    global knowledge

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not KNOWLEDGE_FILE.exists():
        logger.warning(
            "knowledge.json не найден"
        )

        with knowledge_lock:
            knowledge = []

        return

    try:
        with open(
            KNOWLEDGE_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, list):
            with knowledge_lock:
                knowledge = data
        else:
            with knowledge_lock:
                knowledge = []

        logger.info(
            "Загружено записей: %s",
            len(knowledge),
        )

    except Exception as error:
        logger.exception(
            "Ошибка загрузки knowledge.json: %s",
            error,
        )

        with knowledge_lock:
            knowledge = []


# ============================================================
# НОРМАЛИЗАЦИЯ
# ============================================================

def normalize(text):
    text = str(text or "").lower()

    text = text.replace(
        "ё",
        "е",
    )

    text = re.sub(
        r"[^а-яa-z0-9\s]",
        " ",
        text,
    )

    return " ".join(
        text.split()
    )


def words(text):
    return set(
        normalize(text).split()
    )


# ============================================================
# ПОИСК ПО БАЗЕ
# ============================================================

def search_knowledge(
    question,
    limit=8,
):
    query_words = words(question)

    if not query_words:
        return []

    with knowledge_lock:
        current_knowledge = list(knowledge)

    results = []

    for item in current_knowledge:

        if not isinstance(item, dict):
            continue

        title = str(
            item.get(
                "title",
                "",
            )
        )

        text = str(
            item.get(
                "text",
                "",
            )
        )

        category = str(
            item.get(
                "category",
                "",
            )
        )

        searchable = (
            title
            + " "
            + text
            + " "
            + category
        )

        item_words = words(
            searchable
        )

        title_words = words(
            title
        )

        category_words = words(
            category
        )

        score = len(
            query_words & item_words
        )

        score += (
            len(
                query_words & title_words
            )
            * 5
        )

        score += (
            len(
                query_words & category_words
            )
            * 2
        )

        if score > 0:
            results.append(
                (
                    score,
                    item,
                )
            )

    results.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return [
        item
        for _, item in results[:limit]
    ]


# ============================================================
# OPENROUTER
# ============================================================

def ask_openrouter(
    question,
    results,
):
    if not OPENROUTER_API_KEY:
        logger.warning(
            "OPENROUTER_API_KEY не указан"
        )

        return None

    if not results:
        return None

    context_parts = []

    for index, item in enumerate(
        results,
        1,
    ):
        title = str(
            item.get(
                "title",
                "Без названия",
            )
        )

        text = str(
            item.get(
                "text",
                "",
            )
        )

        source = str(
            item.get(
                "source",
                SITE_URL,
            )
        )

        category = str(
            item.get(
                "category",
                "Общее",
            )
        )

        context_parts.append(
            f"""
ИСТОЧНИК {index}

Название:
{title}

Категория:
{category}

Ссылка:
{source}

Содержание:
{text}
"""
        )

    context = "\n".join(
        context_parts
    )

    system_prompt = """
Ты — информационный помощник
МКОУ «Калтукская СОШ».

Отвечай пользователям на вопросы
об образовательном учреждении.

СТРОГИЕ ПРАВИЛА:

1. Используй только информацию
из предоставленного контекста.

2. Не придумывай адреса, телефоны,
ФИО, электронные почты, расписание,
документы или другие сведения.

3. Если в предоставленной информации
нет ответа, напиши:

«В базе школы нет информации
по этому вопросу».

4. Не используй собственные знания
о школе.

5. Отвечай на русском языке.

6. Отвечай понятно и кратко.

7. Если в контексте есть полезная
ссылка на источник, можешь указать её.

8. Не говори пользователю,
что ты языковая модель.

9. Не угадывай информацию.

10. Если вопрос не относится
к МКОУ «Калтукская СОШ», сообщи,
что можешь помогать только
с информацией об образовательном
учреждении.

11. Не добавляй информацию,
которой нет в контексте.
"""

    user_prompt = f"""
ВОПРОС ПОЛЬЗОВАТЕЛЯ:

{question}

ИНФОРМАЦИЯ ИЗ БАЗЫ ШКОЛЫ:

{context}

Сформируй ответ только
на основании информации выше.
"""

    headers = {
        "Authorization":
            f"Bearer {OPENROUTER_API_KEY}",

        "Content-Type":
            "application/json",

        "HTTP-Referer":
            SITE_URL,

        "X-Title":
            "Kaltuk School Bot",
    }

    payload = {
        "model":
            OPENROUTER_MODEL,

        "messages": [
            {
                "role":
                    "system",

                "content":
                    system_prompt,
            },
            {
                "role":
                    "user",

                "content":
                    user_prompt,
            },
        ],

        "temperature":
            0.1,

        "max_tokens":
            1000,
    }

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if response.status_code != 200:

            logger.error(
                "OpenRouter HTTP %s: %s",
                response.status_code,
                response.text[:1000],
            )

            return None

        data = response.json()

        choices = data.get(
            "choices",
            [],
        )

        if not choices:
            logger.error(
                "OpenRouter не вернул choices"
            )

            return None

        message = choices[0].get(
            "message",
            {},
        )

        answer = message.get(
            "content",
            "",
        )

        if not answer:
            return None

        return answer.strip()

    except requests.Timeout:
        logger.error(
            "OpenRouter: timeout"
        )

        return None

    except Exception as error:
        logger.exception(
            "Ошибка OpenRouter: %s",
            error,
        )

        return None


# ============================================================
# ЭКРАНИРОВАНИЕ HTML
# ============================================================

def safe_html(text):
    return html.escape(
        str(text or ""),
        quote=False,
    )


# ============================================================
# ОТВЕТ
# ============================================================

def make_answer(question):

    results = search_knowledge(
        question
    )

    if not results:

        return (
            "🤔 <b>Не нашёл информацию "
            "по этому вопросу.</b>\n\n"

            "В базе школы нет подходящих "
            "сведений.\n\n"

            f'🌐 <a href="{SITE_URL}">'
            "Официальный сайт"
            "</a>"
        )

    answer = ask_openrouter(
        question,
        results,
    )

    if answer:
        return safe_html(
            answer
        )

    # --------------------------------------------------------
    # Резервный режим без ИИ
    # --------------------------------------------------------

    item = results[0]

    title = safe_html(
        item.get(
            "title",
            "Информация",
        )
    )

    text = safe_html(
        item.get(
            "text",
            "",
        )
    )

    source = str(
        item.get(
            "source",
            SITE_URL,
        )
    )

    return (
        f"📚 <b>{title}</b>\n\n"
        f"{text}\n\n"
        f'🔗 <a href="{source}">'
        "Источник"
        "</a>"
    )


# ============================================================
# НИЖНЕЕ МЕНЮ
# ============================================================

def bottom_keyboard():

    keyboard = [

        [
            "🏫 Об учреждении",
            "📞 Контакты",
        ],

        [
            "📰 Новости",
            "📚 Документы",
        ],

        [
            "👨‍🏫 Педагоги",
            "📅 Расписание",
        ],

        [
            "❓ Задать вопрос",
        ],

    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=(
            "Напишите вопрос..."
        ),
    )


# ============================================================
# ГЛАВНОЕ INLINE-МЕНЮ
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(

        [

            [
                InlineKeyboardButton(
                    "🏫 Об учреждении",
                    callback_data="about",
                ),

                InlineKeyboardButton(
                    "📞 Контакты",
                    callback_data="contacts",
                ),
            ],

            [
                InlineKeyboardButton(
                    "📰 Новости",
                    callback_data="news",
                ),

                InlineKeyboardButton(
                    "📚 Документы",
                    callback_data="documents",
                ),
            ],

            [
                InlineKeyboardButton(
                    "👨‍🏫 Педагоги",
                    callback_data="teachers",
                ),

                InlineKeyboardButton(
                    "📅 Расписание",
                    callback_data="schedule",
                ),
            ],

            [
                InlineKeyboardButton(
                    "❓ Задать вопрос",
                    callback_data="question",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🌐 Официальный сайт",
                    url=SITE_URL,
                ),
            ],

        ]
    )


# ============================================================
# АДМИН-МЕНЮ
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup(

        [

            [
                InlineKeyboardButton(
                    "📊 Статистика",
                    callback_data="admin_stats",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🔄 Обновить базу",
                    callback_data="admin_reload",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🌐 Проверить сайт",
                    callback_data="admin_site",
                ),
            ],

            [
                InlineKeyboardButton(
                    "📚 Записи",
                    callback_data="admin_records",
                ),
            ],

        ]
    )


# ============================================================
# ПРОВЕРКА АДМИНА
# ============================================================

def is_admin(update):

    if not update.effective_user:
        return False

    return (
        ADMIN_ID != 0
        and update.effective_user.id == ADMIN_ID
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(

        "🏫 <b>Информационный бот "
        "МКОУ «Калтукская СОШ»</b>\n\n"

        "Я помогу найти информацию "
        "об образовательном учреждении.\n\n"

        "Выберите раздел ниже "
        "или напишите свой вопрос.",

        parse_mode="HTML",

        reply_markup=bottom_keyboard(),

    )

    await update.message.reply_text(

        "📚 <b>Основное меню</b>",

        parse_mode="HTML",

        reply_markup=main_keyboard(),

    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(

        "❓ <b>Помощь</b>\n\n"

        "Вы можете нажать кнопку "
        "в нижнем меню или написать "
        "свой вопрос.\n\n"

        "<b>Например:</b>\n\n"

        "• Где находится школа?\n"
        "• Какой телефон?\n"
        "• Кто директор?\n"
        "• Какая электронная почта?\n"
        "• Где документы?\n"
        "• Какие педагоги работают?\n"
        "• Где расписание?",

        parse_mode="HTML",

    )


# ============================================================
# STATUS
# ============================================================

def read_site_status():

    site_status = "unknown"

    updated = "нет данных"

    if not STATUS_FILE.exists():
        return site_status, updated

    try:

        with open(
            STATUS_FILE,
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(file)

        site_status = data.get(
            "status",
            "unknown",
        )

        updated = data.get(
            "updated_at",
            "нет данных",
        )

    except Exception:
        pass

    return site_status, updated


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    site_status, updated = (
        read_site_status()
    )

    icon = "⚪"

    if site_status == "ok":
        icon = "🟢"

    elif site_status == "error":
        icon = "🔴"

    ai_status = (
        "🟢 подключён"
        if OPENROUTER_API_KEY
        else "🔴 не настроен"
    )

    with knowledge_lock:
        records_count = len(
            knowledge
        )

    await update.message.reply_text(

        "📊 <b>Состояние системы</b>\n\n"

        "Telegram: 🟢 работает\n"

        "База знаний: 🟢 работает\n"

        f"Записей: "
        f"<b>{records_count}</b>\n\n"

        f"OpenRouter: {ai_status}\n"

        f"Модель: "
        f"<code>{safe_html(OPENROUTER_MODEL)}</code>\n\n"

        f"{icon} Сайт: "
        f"<b>{safe_html(site_status)}</b>\n"

        f"Последняя проверка: "
        f"{safe_html(updated)}\n\n"

        f'🌐 <a href="{SITE_URL}">'
        "Официальный сайт"
        "</a>",

        parse_mode="HTML",

        disable_web_page_preview=True,

    )


# ============================================================
# ADMIN
# ============================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not is_admin(update):

        await update.message.reply_text(
            "⛔ Доступ запрещён."
        )

        return

    await update.message.reply_text(

        "🔐 <b>Панель администратора</b>\n\n"
        "Выберите действие:",

        parse_mode="HTML",

        reply_markup=admin_keyboard(),

    )


# ============================================================
# ADMIN STATS
# ============================================================

def build_stats_text():

    categories = {}

    with knowledge_lock:
        current_knowledge = list(
            knowledge
        )

    for item in current_knowledge:

        if not isinstance(item, dict):
            continue

        category = item.get(
            "category",
            "Общее",
        )

        categories[category] = (
            categories.get(
                category,
                0,
            )
            + 1
        )

    text = (
        "📊 <b>Статистика</b>\n\n"

        f"Всего записей: "
        f"<b>{len(current_knowledge)}</b>\n\n"
    )

    if categories:

        text += "<b>Категории:</b>\n"

        for category, count in (
            categories.items()
        ):

            text += (
                f"• "
                f"{safe_html(category)}: "
                f"{count}\n"
            )

    ai_status = (
        "🟢 подключён"
        if OPENROUTER_API_KEY
        else "🔴 не настроен"
    )

    text += (
        "\n"
        f"OpenRouter: {ai_status}\n"
        f"Модель: "
        f"<code>"
        f"{safe_html(OPENROUTER_MODEL)}"
        f"</code>"
    )

    return text


async def admin_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not is_admin(update):

        await update.message.reply_text(
            "⛔ Доступ запрещён."
        )

        return

    await update.message.reply_text(
        build_stats_text(),
        parse_mode="HTML",
    )


# ============================================================
# ОБНОВЛЕНИЕ БАЗЫ
# ============================================================

async def refresh_database():

    logger.info(
        "Начинаем обновление сайта..."
    )

    try:

        pages = await import_site()

        if pages:

            load_knowledge()

            logger.info(
                "База обновлена. "
                "Получено страниц: %s",
                len(pages),
            )

        else:

            logger.warning(
                "Сайт не вернул страницы. "
                "Старая база сохранена."
            )

    except Exception as error:

        logger.exception(
            "Ошибка обновления базы: %s",
            error,
        )


# ============================================================
# ФОНОВОЕ АВТООБНОВЛЕНИЕ
# БЕЗ APSCHEDULER
# ============================================================

def auto_update_worker():

    logger.info(
        "Фоновое автообновление запущено."
    )

    while True:

        try:

            time.sleep(
                UPDATE_INTERVAL_SECONDS
            )

            logger.info(
                "Запускаем автоматическое "
                "обновление базы."
            )

            # asyncio.run здесь не используем,
            # потому что основной Telegram event loop
            # работает отдельно.
            import asyncio

            asyncio.run(
                refresh_database()
            )

        except Exception as error:

            logger.exception(
                "Ошибка фонового обновления: %s",
                error,
            )


def start_auto_update():

    thread = threading.Thread(
        target=auto_update_worker,
        daemon=True,
        name="database-updater",
    )

    thread.start()

    logger.info(
        "Автоматическое обновление: "
        "каждые %s часов.",
        UPDATE_INTERVAL_SECONDS // 3600,
    )


# ============================================================
# ADMIN RELOAD
# ============================================================

async def admin_reload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not is_admin(update):

        await update.message.reply_text(
            "⛔ Доступ запрещён."
        )

        return

    message = await update.message.reply_text(
        "🔄 Обновляю базу..."
    )

    try:

        await refresh_database()

        with knowledge_lock:
            count = len(knowledge)

        await message.edit_text(

            "✅ <b>Обновление завершено</b>\n\n"

            f"Записей в базе: "
            f"<b>{count}</b>",

            parse_mode="HTML",

        )

    except Exception as error:

        logger.exception(
            "Ошибка обновления: %s",
            error,
        )

        await message.edit_text(
            "❌ Ошибка обновления. "
            "Старая база сохранена."
        )


# ============================================================
# КНОПКИ INLINE
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    questions = {

        "about":
            "об учреждении название "
            "директор адрес",

        "contacts":
            "контакты телефон адрес "
            "электронная почта email",

        "news":
            "новости объявления события",

        "documents":
            "документы",

        "teachers":
            "педагоги учителя сотрудники",

        "schedule":
            "расписание",
    }

    # --------------------------------------------------------
    # Задать вопрос
    # --------------------------------------------------------

    if query.data == "question":

        await query.message.reply_text(

            "❓ Напишите свой вопрос "
            "обычным сообщением."

        )

        return

    # --------------------------------------------------------
    # Пользовательские разделы
    # --------------------------------------------------------

    if query.data in questions:

        await query.message.chat.send_action(
            ChatAction.TYPING
        )

        answer = make_answer(
            questions[
                query.data
            ]
        )

        await query.message.reply_text(

            answer,

            parse_mode="HTML",

            disable_web_page_preview=True,

        )

        return

    # --------------------------------------------------------
    # Проверка администратора
    # --------------------------------------------------------

    if not is_admin(update):
        return

    # --------------------------------------------------------
    # Статистика
    # --------------------------------------------------------

    if query.data == "admin_stats":

        await query.message.reply_text(

            build_stats_text(),

            parse_mode="HTML",

        )

        return

    # --------------------------------------------------------
    # Обновление
    # --------------------------------------------------------

    if query.data == "admin_reload":

        await query.message.reply_text(
            "🔄 Запускаю обновление..."
        )

        await refresh_database()

        with knowledge_lock:
            count = len(knowledge)

        await query.message.reply_text(

            "✅ Обновление завершено.\n\n"

            f"Записей в базе: "
            f"{count}"

        )

        return

    # --------------------------------------------------------
    # Записи
    # --------------------------------------------------------

    if query.data == "admin_records":

        with knowledge_lock:
            current_knowledge = list(
                knowledge
            )

        if not current_knowledge:

            await query.message.reply_text(
                "📚 База пустая."
            )

            return

        text = (
            "📚 <b>Записи базы</b>\n\n"
        )

        for index, item in enumerate(
            current_knowledge[:30],
            1,
        ):

            if not isinstance(item, dict):
                continue

            title = safe_html(
                item.get(
                    "title",
                    "Без названия",
                )
            )

            category = safe_html(
                item.get(
                    "category",
                    "Общее",
                )
            )

            text += (
                f"<b>{index}.</b> "
                f"{title}\n"
                f"Категория: "
                f"{category}\n\n"
            )

        await query.message.reply_text(

            text,

            parse_mode="HTML",

        )

        return

    # --------------------------------------------------------
    # Проверка сайта
    # --------------------------------------------------------

    if query.data == "admin_site":

        await query.message.reply_text(
            "🌐 Проверяю сайт..."
        )

        try:

            pages = await import_site()

            await query.message.reply_text(

                "📡 <b>Результат</b>\n\n"

                f"Страниц получено: "
                f"<b>{len(pages)}</b>\n\n"

                f'<a href="{SITE_URL}">'
                "Официальный сайт"
                "</a>",

                parse_mode="HTML",

                disable_web_page_preview=True,

            )

        except Exception as error:

            logger.exception(
                "Ошибка проверки сайта: %s",
                error,
            )

            await query.message.reply_text(

                "🔴 Сайт сейчас "
                "недоступен."

            )


# ============================================================
# НИЖНЕЕ МЕНЮ
# ============================================================

async def bottom_menu_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    buttons = {

        "🏫 Об учреждении":
            "об учреждении название "
            "директор адрес",

        "📞 Контакты":
            "контакты телефон адрес "
            "электронная почта email",

        "📰 Новости":
            "новости объявления события",

        "📚 Документы":
            "документы",

        "👨‍🏫 Педагоги":
            "педагоги учителя сотрудники",

        "📅 Расписание":
            "расписание",
    }

    if text == "❓ Задать вопрос":

        await update.message.reply_text(

            "❓ Напишите свой вопрос "
            "обычным сообщением."

        )

        return

    if text in buttons:

        await update.message.chat.send_action(
            ChatAction.TYPING
        )

        answer = make_answer(
            buttons[text]
        )

        await update.message.reply_text(

            answer,

            parse_mode="HTML",

            disable_web_page_preview=True,

        )

        return

    # Обычный пользовательский вопрос

    await text_handler(
        update,
        context,
    )


# ============================================================
# TEXT
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    question = (
        update.message.text
        or ""
    ).strip()

    if not question:
        return

    logger.info(
        "Вопрос пользователя: %s",
        question,
    )

    await update.message.chat.send_action(
        ChatAction.TYPING
    )

    try:

        answer = make_answer(
            question
        )

        await update.message.reply_text(

            answer,

            parse_mode="HTML",

            disable_web_page_preview=True,

        )

    except Exception as error:

        logger.exception(
            "Ошибка формирования ответа: %s",
            error,
        )

        await update.message.reply_text(

            "❌ Произошла ошибка "
            "при обработке вопроса.\n\n"

            "Попробуйте ещё раз."

        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.error(
        "Ошибка Telegram: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN отсутствует в .env"
        )

    load_knowledge()

    logger.info(
        "========================================"
    )

    logger.info(
        "ШКОЛЬНЫЙ БОТ ЗАПУСКАЕТСЯ"
    )

    logger.info(
        "Записей базы: %s",
        len(knowledge),
    )

    logger.info(
        "OpenRouter: %s",
        (
            "подключён"
            if OPENROUTER_API_KEY
            else "не настроен"
        ),
    )

    logger.info(
        "Модель: %s",
        OPENROUTER_MODEL,
    )

    logger.info(
        "========================================"
    )

    # --------------------------------------------------------
    # Фоновое обновление
    # --------------------------------------------------------

    start_auto_update()

    # --------------------------------------------------------
    # Telegram Application
    # --------------------------------------------------------

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Команды
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin_stats",
            admin_stats,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin_reload",
            admin_reload,
        )
    )

    # --------------------------------------------------------
    # Inline-кнопки
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_handler,
        )
    )

    # --------------------------------------------------------
    # Текст
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            bottom_menu_handler,
        )
    )

    # --------------------------------------------------------
    # Ошибки
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Telegram polling запускается..."
    )

    # --------------------------------------------------------
    # Запуск
    # --------------------------------------------------------

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        logger.info(
            "Бот остановлен пользователем."
        )

    except Exception as error:

        logger.exception(
            "Критическая ошибка: %s",
            error,
        )
