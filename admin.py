import json
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"

KNOWLEDGE_FILE = DATA_DIR / "knowledge.json"


def load_knowledge():

    if not KNOWLEDGE_FILE.exists():
        return []

    try:

        with open(
            KNOWLEDGE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if isinstance(data, list):
            return data

    except Exception:
        pass

    return []


def save_knowledge(data):

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        KNOWLEDGE_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )


def add_item(
    title,
    text,
    source,
    category="Общее"
):

    data = load_knowledge()

    item = {
        "title": title,
        "text": text,
        "source": source,
        "category": category
    }

    data.append(
        item
    )

    save_knowledge(
        data
    )


def remove_item(index):

    data = load_knowledge()

    if index < 0 or index >= len(data):
        return False

    data.pop(
        index
    )

    save_knowledge(
        data
    )

    return True


def get_stats():

    data = load_knowledge()

    categories = {}

    for item in data:

        category = item.get(
            "category",
            "Общее"
        )

        categories[category] = (
            categories.get(
                category,
                0
            ) + 1
        )

    return {
        "total": len(data),
        "categories": categories
    }