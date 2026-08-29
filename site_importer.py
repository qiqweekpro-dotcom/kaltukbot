import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

SITE_URL = "https://shkola-kaltuk.gosuslugi.ru"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

KNOWLEDGE_FILE = DATA_DIR / "knowledge.json"
STATUS_FILE = DATA_DIR / "site_status.json"

# Максимального количества страниц нет.
# Импорт остановится, когда закончатся новые ссылки.

REQUEST_TIMEOUT = 15
BROWSER_TIMEOUT = 30000

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)


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

logger = logging.getLogger("site-importer")


# ============================================================
# СТАТИСТИКА
# ============================================================

stats = {
    "found": 0,
    "success": 0,
    "not_found": 0,
    "errors": 0,
    "skipped": 0,
}


# ============================================================
# СОХРАНЕНИЕ СТАТУСА
# ============================================================

def save_status(
    status,
    message="",
    pages=0,
):
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    data = {
        "status": status,
        "message": message,
        "pages": pages,
        "updated_at": datetime.now().isoformat(
            timespec="seconds"
        ),
        "site": SITE_URL,
        "statistics": stats.copy(),
    }

    try:
        with open(
            STATUS_FILE,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as error:
        logger.warning(
            "Не удалось сохранить статус: %s",
            error,
        )


# ============================================================
# НОРМАЛИЗАЦИЯ URL
# ============================================================

def normalize_url(url):
    if not url:
        return None

    try:
        url = url.strip()

        # Убираем якорь
        url, _ = urldefrag(url)

        parsed = urlparse(url)

        if parsed.scheme not in (
            "http",
            "https",
        ):
            return None

        if not parsed.netloc:
            return None

        path = parsed.path or "/"

        # Убираем последний /
        # кроме главной страницы
        if (
            path != "/"
            and path.endswith("/")
        ):
            path = path.rstrip("/")

        normalized = parsed._replace(
            path=path,
            fragment="",
        ).geturl()

        return normalized

    except Exception:
        return None


# ============================================================
# ПРОВЕРКА ДОМЕНА
# ============================================================

def is_same_domain(url):
    try:
        base = urlparse(SITE_URL)
        target = urlparse(url)

        return (
            target.scheme in (
                "http",
                "https",
            )
            and target.netloc.lower()
            == base.netloc.lower()
        )

    except Exception:
        return False


# ============================================================
# ПРОВЕРКА ССЫЛКИ
# ============================================================

def is_ignored_link(url):
    if not url:
        return True

    lowered = url.lower().strip()

    ignored_prefixes = (
        "javascript:",
        "mailto:",
        "tel:",
        "sms:",
        "data:",
        "blob:",
    )

    return lowered.startswith(
        ignored_prefixes
    )


# ============================================================
# ПРОВЕРКА ФАЙЛА
# ============================================================

def is_file_url(url):
    path = urlparse(url).path.lower()

    extensions = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".rar",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
    )

    return path.endswith(
        extensions
    )


# ============================================================
# ОЧИСТКА ТЕКСТА
# ============================================================

def clean_text(text):
    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# ============================================================
# ПРОВЕРКА 404
# ============================================================

def is_404_page(
    title,
    text,
):
    value = (
        f"{title} {text}"
    ).lower()

    markers = (
        "страница не найдена",
        "страница не существует",
        "ошибка 404",
        "404 not found",
        "page not found",
    )

    for marker in markers:
        if marker in value:
            return True

    return False


# ============================================================
# ОПРЕДЕЛЕНИЕ КАТЕГОРИИ
# ============================================================

def detect_category(
    url,
    title,
):
    value = (
        f"{url} {title}"
    ).lower()

    if (
        "news" in value
        or "новост" in value
        or "событ" in value
    ):
        return "Новости"

    if (
        "document" in value
        or "documents" in value
        or "документ" in value
        or ".pdf" in value
    ):
        return "Документы"

    if (
        "employee" in value
        or "employees" in value
        or "педагог" in value
        or "учител" in value
    ):
        return "Педагоги"

    if (
        "education" in value
        or "образован" in value
        or "program" in value
        or "программ" in value
    ):
        return "Образование"

    if (
        "contact" in value
        or "контакт" in value
        or "телефон" in value
        or "email" in value
        or "почт" in value
    ):
        return "Контакты"

    if (
        "sveden" in value
        or "сведен" in value
        or "образовательной-организации" in value
    ):
        return "Сведения об учреждении"

    if (
        "schedule" in value
        or "расписан" in value
    ):
        return "Расписание"

    return "Общее"


# ============================================================
# ИЗВЛЕЧЕНИЕ HTML
# ============================================================

def parse_html(
    html,
    url,
    status_code=None,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # --------------------------------------------------------
    # Сначала собираем все ссылки
    # --------------------------------------------------------

    links = []

    for a in soup.find_all(
        "a",
        href=True,
    ):
        href = a.get("href")

        if is_ignored_link(href):
            continue

        absolute = urljoin(
            url,
            href,
        )

        absolute = normalize_url(
            absolute
        )

        if not absolute:
            continue

        if not is_same_domain(
            absolute
        ):
            continue

        links.append(
            absolute
        )

    links = list(
        dict.fromkeys(links)
    )

    # --------------------------------------------------------
    # Удаляем технический HTML
    # --------------------------------------------------------

    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
        ]
    ):
        tag.decompose()

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    title = ""

    if soup.title:
        title = clean_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    # --------------------------------------------------------
    # H1
    # --------------------------------------------------------

    h1 = soup.find("h1")

    if h1:
        h1_text = clean_text(
            h1.get_text(
                " ",
                strip=True,
            )
        )

        if h1_text:
            title = h1_text

    # --------------------------------------------------------
    # ОСНОВНОЙ КОНТЕНТ
    # --------------------------------------------------------

    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("body")
    )

    if not main:
        return None, links

    text = clean_text(
        main.get_text(
            " ",
            strip=True,
        )
    )

    # --------------------------------------------------------
    # HTTP ОШИБКИ
    # --------------------------------------------------------

    if status_code == 404:
        return None, links

    if (
        status_code is not None
        and status_code >= 400
    ):
        return None, links

    # --------------------------------------------------------
    # ТЕКСТОВАЯ 404
    # --------------------------------------------------------

    if is_404_page(
        title,
        text,
    ):
        return None, links

    # --------------------------------------------------------
    # Слишком короткая страница
    # --------------------------------------------------------

    if len(text) < 30:
        return None, links

    # --------------------------------------------------------
    # СОЗДАЁМ ЗАПИСЬ
    # --------------------------------------------------------

    item = {
        "title": (
            title
            or "Страница школы"
        ),
        "text": text,
        "source": url,
        "category": detect_category(
            url,
            title,
        ),
        "status_code": status_code,
        "updated_at": datetime.now().isoformat(
            timespec="seconds"
        ),
    }

    return item, links


# ============================================================
# ЗАГРУЗКА ЧЕРЕЗ REQUESTS
# ============================================================

def load_requests(url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": (
            "ru-RU,ru;q=0.9"
        ),
        "Connection": "keep-alive",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            verify=True,
        )

        response.encoding = (
            response.apparent_encoding
            or response.encoding
        )

        logger.info(
            "REQUESTS [%s] %s",
            response.status_code,
            url,
        )

        return (
            response.text,
            response.status_code,
            normalize_url(
                response.url
            ),
        )

    except requests.exceptions.Timeout:
        logger.warning(
            "REQUESTS TIMEOUT: %s",
            url,
        )

        return None, None, None

    except Exception as error:
        logger.warning(
            "REQUESTS ERROR %s: %s",
            url,
            error,
        )

        return None, None, None


# ============================================================
# ЗАГРУЗКА ЧЕРЕЗ CHROMIUM
# ============================================================

async def load_browser(
    page,
    url,
):
    logger.info(
        "CHROMIUM: %s",
        url,
    )

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=BROWSER_TIMEOUT,
        )

        await page.wait_for_timeout(
            1500
        )

        # Пытаемся дождаться JS
        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=5000,
            )
        except Exception:
            pass

        html = await page.content()

        status_code = (
            response.status
            if response
            else None
        )

        final_url = normalize_url(
            page.url
        )

        logger.info(
            "CHROMIUM [%s] %s",
            status_code,
            final_url,
        )

        return (
            html,
            status_code,
            final_url,
        )

    except PlaywrightTimeoutError:
        logger.warning(
            "CHROMIUM TIMEOUT: %s",
            url,
        )

        return None, None, None

    except Exception as error:
        logger.warning(
            "CHROMIUM ERROR %s: %s",
            url,
            error,
        )

        return None, None, None


# ============================================================
# ОСНОВНОЙ ИМПОРТ
# ============================================================

async def import_site():

    # Сбрасываем статистику
    stats["found"] = 0
    stats["success"] = 0
    stats["not_found"] = 0
    stats["errors"] = 0
    stats["skipped"] = 0

    logger.info(
        "========================================"
    )

    logger.info(
        "НАЧАЛО ИМПОРТА САЙТА"
    )

    logger.info(
        "Сайт: %s",
        SITE_URL,
    )

    logger.info(
        "Режим: автоматический обход"
    )

    logger.info(
        "Лимит страниц: НЕТ"
    )

    logger.info(
        "========================================"
    )

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    found = {}

    # Очередь страниц
    queue = []

    # Уже посещённые URL
    visited = set()

    # Добавляем только главную
    start_url = normalize_url(
        SITE_URL
    )

    queue.append(
        start_url
    )

    # ========================================================
    # PLAYWRIGHT
    # ========================================================

    async with async_playwright() as playwright:

        browser = None

        try:
            browser = await playwright.chromium.launch(
                headless=True
            )

            context = await browser.new_context(
                user_agent=USER_AGENT,
            )

            page = await context.new_page()

            # ------------------------------------------------
            # ОБХОД САЙТА
            # ------------------------------------------------

            while queue:

                url = queue.pop(0)

                url = normalize_url(
                    url
                )

                if not url:
                    continue

                if url in visited:
                    continue

                if not is_same_domain(
                    url
                ):
                    continue

                # Не открываем файлы как HTML
                if is_file_url(url):

                    stats["skipped"] += 1

                    visited.add(url)

                    logger.info(
                        "Файл пропущен: %s",
                        url,
                    )

                    continue

                visited.add(url)

                stats["found"] += 1

                logger.info(
                    "----------------------------------------"
                )

                logger.info(
                    "СТРАНИЦА №%s",
                    len(visited),
                )

                logger.info(
                    "ОЧЕРЕДЬ: %s",
                    len(queue),
                )

                logger.info(
                    "%s",
                    url,
                )

                # =================================================
                # Сначала REQUESTS
                # =================================================

                (
                    html,
                    status_code,
                    final_url,
                ) = load_requests(
                    url
                )

                item = None
                links = []

                if html:

                    target_url = (
                        final_url
                        or url
                    )

                    item, links = parse_html(
                        html,
                        target_url,
                        status_code,
                    )

                # =================================================
                # Если requests не справился —
                # используем Chromium
                # =================================================

                if item is None:

                    (
                        browser_html,
                        browser_status,
                        browser_url,
                    ) = await load_browser(
                        page,
                        url,
                    )

                    if browser_html:

                        target_url = (
                            browser_url
                            or url
                        )

                        item, browser_links = parse_html(
                            browser_html,
                            target_url,
                            browser_status,
                        )

                        links.extend(
                            browser_links
                        )

                # =================================================
                # Уникальные ссылки
                # =================================================

                links = list(
                    dict.fromkeys(
                        links
                    )
                )

                # =================================================
                # Добавляем новые страницы
                # =================================================

                new_links = 0

                for link in links:

                    link = normalize_url(
                        link
                    )

                    if not link:
                        continue

                    if not is_same_domain(
                        link
                    ):
                        continue

                    if link in visited:
                        continue

                    if link in queue:
                        continue

                    # Файлы не открываем
                    if is_file_url(link):

                        stats["skipped"] += 1

                        logger.info(
                            "Найден файл: %s",
                            link,
                        )

                        continue

                    queue.append(
                        link
                    )

                    new_links += 1

                logger.info(
                    "Новых ссылок найдено: %s",
                    new_links,
                )

                # =================================================
                # Сохраняем только настоящую страницу
                # =================================================

                if item:

                    source = normalize_url(
                        item["source"]
                    )

                    item["source"] = source

                    found[source] = item

                    stats["success"] += 1

                    logger.info(
                        "OK: %s",
                        item["title"],
                    )

                else:

                    # Проверяем HTTP 404
                    if status_code == 404:

                        stats["not_found"] += 1

                        logger.info(
                            "404: %s",
                            url,
                        )

                    elif (
                        status_code is not None
                        and status_code >= 400
                    ):

                        stats["errors"] += 1

                        logger.warning(
                            "HTTP %s: %s",
                            status_code,
                            url,
                        )

                    else:

                        stats["skipped"] += 1

                        logger.info(
                            "Страница пропущена: %s",
                            url,
                        )

        finally:

            if browser:
                await browser.close()

    # ========================================================
    # ФИНАЛЬНЫЙ СПИСОК
    # ========================================================

    pages = list(
        found.values()
    )

    pages.sort(
        key=lambda item: (
            item.get(
                "category",
                "Общее",
            ),
            item.get(
                "title",
                "",
            ),
        )
    )

    # ========================================================
    # ЕСЛИ НИЧЕГО НЕ НАШЛИ
    # ========================================================

    if not pages:

        logger.warning(
            "========================================"
        )

        logger.warning(
            "НЕ УДАЛОСЬ ПОЛУЧИТЬ СТРАНИЦЫ"
        )

        logger.warning(
            "Старая база НЕ будет удалена."
        )

        save_status(
            "error",
            "Не удалось получить страницы сайта",
            0,
        )

        return []

    # ========================================================
    # СОХРАНЕНИЕ БАЗЫ
    # ========================================================

    with open(
        KNOWLEDGE_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            pages,
            file,
            ensure_ascii=False,
            indent=2,
        )

    save_status(
        "ok",
        "Сайт успешно импортирован",
        len(pages),
    )

    # ========================================================
    # ИТОГОВЫЙ ЛОГ
    # ========================================================

    logger.info(
        "========================================"
    )

    logger.info(
        "ИМПОРТ ЗАВЕРШЁН"
    )

    logger.info(
        "========================================"
    )

    logger.info(
        "Всего посещено URL: %s",
        stats["found"],
    )

    logger.info(
        "Успешных страниц: %s",
        stats["success"],
    )

    logger.info(
        "404: %s",
        stats["not_found"],
    )

    logger.info(
        "Ошибок: %s",
        stats["errors"],
    )

    logger.info(
        "Пропущено: %s",
        stats["skipped"],
    )

    logger.info(
        "Сохранено страниц: %s",
        len(pages),
    )

    return pages


# ============================================================
# MAIN
# ============================================================

async def main():

    pages = await import_site()

    print()
    print("=" * 70)
    print("РЕЗУЛЬТАТ ИМПОРТА")
    print("=" * 70)

    print(
        f"Посещено URL: {stats['found']}"
    )

    print(
        f"Успешных страниц: {stats['success']}"
    )

    print(
        f"404: {stats['not_found']}"
    )

    print(
        f"Ошибок: {stats['errors']}"
    )

    print(
        f"Пропущено: {stats['skipped']}"
    )

    print(
        f"Сохранено страниц: {len(pages)}"
    )

    print()
    print("-" * 70)

    if pages:

        for item in pages:

            print(
                f"- {item.get('title', 'Без названия')}"
            )

            print(
                f"  {item.get('source', '')}"
            )

            print(
                f"  Категория: "
                f"{item.get('category', 'Общее')}"
            )

            print()

    else:

        print(
            "Страницы не найдены."
        )


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Импорт остановлен пользователем."
        )