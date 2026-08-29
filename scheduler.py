import asyncio
import logging

from site_importer import import_site


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | scheduler | %(message)s"
)

logger = logging.getLogger("scheduler")


INTERVAL_HOURS = 6


async def main():

    logger.info("Планировщик обновления запущен")

    while True:

        try:

            logger.info("Начинаем обновление базы")

            pages = await import_site()

            logger.info(
                "Обновление завершено. Получено страниц: %s",
                len(pages)
            )

        except Exception as error:

            logger.exception(
                "Ошибка обновления: %s",
                error
            )

        logger.info(
            "Следующее обновление через %s часов",
            INTERVAL_HOURS
        )

        await asyncio.sleep(
            INTERVAL_HOURS * 60 * 60
        )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Планировщик остановлен"
        )