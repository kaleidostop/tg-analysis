from typing import Optional
import time
from datetime import datetime
import os
from urllib.parse import urljoin, urlparse
import requests
from lxml import html
import csv
import pandas as pd
import re
import langid

from config import (
    BASE_URL, CATALOG_URL,
    DATA_RAW_DIR, DATA_PROCESSED_DIR, DATA_FINAL_DIR,
    LOG_PATH, HEADERS, TIMEOUT, PAUSE_SEC
)

# Вспомогательные функции
def ensure_parent_dir(path: str):   # проверяет, что родительская директория существует
    os.makedirs(os.path.dirname(path), exist_ok=True)

def _t1(n, xp):
    v = (n.xpath(xp) or [""])[0]    # берет первый совпавший результат или пустую строку
    return (v if isinstance(v, str)         # если это строка, возвращает ее,
            else v.text_content()).strip()  # иначе возвращает текст внутри этого элемента


def _int(s: str):    # убирает пробелы и переводит в число
    try:
        return int(str(s).strip().replace(' ', '').replace(',', ''))
    except (TypeError, ValueError):
        return None


# Логирование
def log_message(message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def is_event_logged(message: str) -> bool:
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().endswith(message):
                    return True
    except FileNotFoundError:
        return False
    return False

# Сохранение и открытие html страниц
def fetch_page(url: str):
    time.sleep(PAUSE_SEC)
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def download_html_page(url: str, filename: str):
    ensure_parent_dir(filename)
    try:
        r = fetch_page(url)
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(r.text)
        log_message(f"downloaded: {url}")
        print(f"HTML {url} успешно сохранен в {filename}")
    except requests.exceptions.RequestException as e:
        log_message(f"error {url}: {e}")
        print(f"Ошибка при загрузке {url}: {e}")


def is_downloaded(url: str) -> bool:
    return is_event_logged(f"downloaded: {url}")


def open_downloaded_page(filename: str) -> str:
    if os.path.exists(filename):
        return open(filename, encoding="utf-8").read()
    else:
        raise FileNotFoundError(f"Нет файла: {filename}")

# Сохранение и открытие таблиц
def save_table_to_csv(csv_file: str, rows: list, names: list):
    try:
        ensure_parent_dir(csv_file)
        pd.DataFrame(rows, columns=names).drop_duplicates().to_csv(csv_file, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)

        log_message(f"saved table: {csv_file}")
        print(f"Таблица сохранена: {csv_file}")
    except Exception as e:
        log_message(f"error {csv_file}: {e}")
        print(f"Ошибка при сохранении {csv_file}: {e}")


def is_table_saved(path: str) -> bool:
    return is_event_logged(f"saved table: {path}")


def open_saved_table(csv_file: str) -> Optional[pd.DataFrame]:
    if os.path.exists(csv_file):
        return pd.read_csv(csv_file, encoding="utf-8-sig")
    else:
        raise FileNotFoundError(f"Нет файла: {csv_file}")


# Получение и обработка списка категорий
def parse_catalog_for_categories(source: str) -> list:
    """
    Возвращает list[tuple] со строками (url, name, size)
    """
    tree = html.fromstring(source)

    anchors = tree.xpath(
        "//a[contains(concat(' ', normalize-space(@class), ' '), ' category-mp-section__list-slide-item ')]")
    categories = []
    for a in anchors:
        url = urljoin(BASE_URL, a.get("href") or "")
        name = _t1(a, ".//div[contains(concat(' ', normalize-space(@class), ' '), ' category-mp-section__list-slide-name ')]/text()")
        size = _t1(a, ".//div[contains(concat(' ', normalize-space(@class), ' '), ' category-mp-section__list-slide-count ')]/text()")

        categories.append((name, url, size))
    return categories


def get_and_save_categories(categories_csv: str):
    catalog_url = urljoin(BASE_URL, CATALOG_URL)
    catalog_filename = os.path.join(DATA_RAW_DIR, "catalogs_list.html")

    if not is_downloaded(catalog_url):
        download_html_page(catalog_url, catalog_filename)
    else:
        print(f"Страница {catalog_url} уже загружена")

    catalog_html = open_downloaded_page(catalog_filename)
    categories = parse_catalog_for_categories(catalog_html)

    save_table_to_csv(categories_csv, categories, ["name", "url", "size"])

    print(f"Найдено категорий: {len(categories)}. Сохранено в {categories_csv}.")


def download_category_pages(df: pd.DataFrame, categories_dir: str):
    for i, row in df.iterrows():
        url = row["url"]
        name = row["name"]

        filename = os.path.join(categories_dir, name + ".html")

        download_html_page(url, filename)

    log_message("downloaded: all category pages")
    print("Все страницы категорий загружены")


def are_category_pages_downloaded():
    return is_event_logged("downloaded: all category pages")

# Обработка каналов на страницах категорий
def extract_key(tme_url: str) -> Optional[str]:
    """
    Возвращает key:
      - публичный handle: t.me/<handle>  -> <handle>
      - инвайт: t.me/joinchat/<code> -> invite_<code>
    """
    if not tme_url:
        return None

    p = urlparse(tme_url)
    host = (p.netloc or "").lower()

    if host == "t.me":
        segs = [s for s in p.path.split("/") if s]
        if segs:
            if segs[0] == "joinchat" and len(segs) >= 2:
                return f"invite_{segs[1]}"
            else:
                return segs[0].lstrip("@")

    return None

def parse_category_page(source: str, seen_keys: set):
    """
    Возвращает таблицу каналов и таблицу связей канал-категория:
        rows : list[tuple] (key, name, telemetr_url, tme_url, rank, avatar_url, subscribers, views_per_post)
        edges : list[tuple] (key, category)
    """
    tree = html.fromstring(source)

    trs = tree.xpath("//tr[@data-id]")
    rows, edges = [], []
    for tr in trs:
        name = _t1(tr, ".//a[contains(@class,'catalog-table-cell__about-link')]/@title") \
               or _t1(tr, ".//a[contains(@class,'catalog-table-cell__about-link')]/text()")
        telemetr_url = urljoin(BASE_URL, _t1(tr, ".//a[contains(@class,'catalog-table-cell__about-link')]/@href"))
        tme_url = _t1(tr, ".//a[contains(@class,'about-link-external')]/@href")
        avatar_url = _t1(tr, ".//img[contains(@class,'pic-image')]/@src")
        rank = _int(_t1(tr, ".//div[contains(@class,'pic-number')]/text()"))
        subscribers = _int(_t1(tr, ".//div[@data-cell-catalog='subscribers']/text()")) \
                      or _int(_t1(tr, ".//div[contains(@class,'about-link-pertice')]/text()"))
        views_per_post = _int(_t1(tr, ".//div[@data-cell-catalog='views_per_post']/text()"))

        key = extract_key(tme_url)
        if not key:
            log_message(f"warning: not supported key {tme_url}")
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)

        rows.append((key, name, telemetr_url, tme_url, rank, avatar_url, subscribers, views_per_post))

        cats = [c.strip() for c in tr.xpath(".//div[@data-cell-catalog='categories']//a/text()") if c.strip()]

        for c in cats:
            edges.append((key, c))

    return rows, edges

def parse_all_categories(categories_dir: str, channels_csv: str, edges_csv: str):
    channel_cols = ["key", "name", "telemetr_url", "tme_url", "rank", "avatar_url", "subscribers",
                    "views_per_post"]
    edge_cols = ["key", "category"]

    seen_keys = set()
    all_rows, all_edges = [], []

    for fname in os.listdir(categories_dir):
        if not fname.lower().endswith(".html"):
            continue
        path = os.path.join(categories_dir, fname)
        html_page = open_downloaded_page(path)

        rows, edges = parse_category_page(html_page, seen_keys)

        log_message(f"info: {fname} contains {len(rows)} new channels")

        all_rows.extend(rows)
        all_edges.extend(edges)

        print(f"Parsed {fname}")

    save_table_to_csv(channels_csv, all_rows, channel_cols)
    save_table_to_csv(edges_csv, all_edges, edge_cols)

# Обработка данных на страницах каналов
def download_channel_pages(df: pd.DataFrame, channels_dir: str, start_with: str = None):
    started = start_with is None

    for i, row in df.iterrows():
        url = row["telemetr_url"]
        key = row["key"]

        if not started:
            if key == start_with:
                started = True
            else:
                continue

        if is_downloaded(url):
           continue

        filename = os.path.join(channels_dir, key + ".html")

        download_html_page(url, filename)

    log_message("downloaded: all channel pages")
    print("Все страницы каналов загружены")


def are_channel_pages_downloaded():
    return is_event_logged("downloaded: all channel pages")


def detect_channel_language(key: str, last_post_content: str, description: str) -> str:
    threshold = 0.6
    if last_post_content:
        lang, prob = langid.rank(last_post_content)[0]
        return lang

    if description:
        lang, prob = langid.rank(description)[0]
        return lang

    log_message(f"warning: language for channel {key} couldn't be detected")
    return ""


def parse_channel_page(key: str, source: str):
    """
        Возвращает данные о канале, которых не было на странице категории:
        (key, subscribers, subs_delta_day, views_per_post, num_posts, num_messages, num_quotes, num_replies, num_likes, description,
        er_general, er_daily, posts_showing, last_post_content, last_post_datetime, language)
    """
    if source.strip() == "Контент заблокирован как нежелательный":
        log_message(f"warning: channel {key} is blocked")
        return
    tree = html.fromstring(source)

    inner_stat_divs = tree.xpath("//div[contains(@class, 'channel-inner__stat-item')]")

    nums = []
    for div in inner_stat_divs[:5]:
        val = div.xpath("text()")[-1].strip()
        nums.append(val)
    if len(nums) < 5:
        log_message(f"warning: channel {key} page doesn't have all inner stats")
        while len(nums) < 5:
            nums.append("")

    num_posts, num_messages, num_quotes, num_replies, num_likes = nums

    desc_nodes = tree.xpath("//div[contains(@class, 'channel-inner__about')]")
    if len(desc_nodes) < 1:
        log_message(f"warning: channel {key} page doesn't have description")
        description = ""
    else:
        description = desc_nodes[0].text_content().strip()

    stat_divs = tree.xpath("//div[contains(@class, 'stat-col__data')]")

    subscribers = _int(_t1(stat_divs[0],  ".//text()")) if len(stat_divs) > 0 else ""
    subs_delta_day = _int(_t1(stat_divs[1],  ".//text()")) if len(stat_divs) > 1 else ""
    views_per_post = _int(_t1(stat_divs[2],  ".//text()")) if len(stat_divs) > 2 else ""
    er_general = _t1(stat_divs[3], ".//text()") if len(stat_divs) > 3 else ""
    er_daily = _t1(stat_divs[4], ".//text()") if len(stat_divs) > 4 else ""
    if len(stat_divs) < 5:
        log_message(f"warning: channel {key} page doesn't have all stats")

    last_post_content = _t1(tree, "(//div[contains(@class, 'post-card__body-container')])[1]")
    if not last_post_content:
        log_message(f"warning: channel {key} page doesn't have posts")

    m = re.findall(r"Пост от (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})", source)
    posts_showing = len(m)
    last_post_datetime = m[0] if m else ""

    language = detect_channel_language(key, last_post_content, description)

    last_post_content = last_post_content.replace('\xa0', ' ')  # удаление неразрывного пробела
    description = description.replace('\xa0', ' ')

    return (key, subscribers, subs_delta_day, views_per_post, num_posts, num_messages, num_quotes, num_replies, num_likes, description,
            er_general, er_daily, posts_showing, last_post_content, last_post_datetime, language)


def parse_all_channels(channels_dir: str, channels_extra_csv: str):
    channel_cols = ["key", "subscribers", "subs_delta_day", "views_per_post", "num_posts", "num_messages", "num_quotes", "num_replies", "num_likes", "description", "er_general",
                    "er_daily", "posts_showing", "last_post_content", "last_post_datetime", "language"]

    rows = []
    for fname in os.listdir(channels_dir):
        if not fname.lower().endswith(".html"):
            continue
        path = os.path.join(channels_dir, fname)
        html_page = open_downloaded_page(path)

        key, _ = os.path.splitext(fname)
        row = parse_channel_page(key, html_page)

        rows += [row]

        print(f"Parsed {fname}")

    save_table_to_csv(channels_extra_csv, rows, channel_cols)

# Объединение данных каналов, полученных обработкой страниц категорий и каналов
def merge_tables(first_csv: str, second_csv: str, out_csv: str):
    first = open_saved_table(first_csv)
    second = open_saved_table(second_csv)

    first = first.drop(columns=["rank"])

    merged = second.merge(first, on="key", how="left", suffixes=('', '_first'))

    for col in merged.columns:
        if col.endswith('_first'):
            orig_col = col[:-6]  # убираем '_first'
            # если оригинальная колонка существует, заполняем пропуски значениями из первой таблицы
            if orig_col in merged.columns:
                merged[orig_col].fillna(merged[col], inplace=True)
            else:
                # если оригинальной колонки нет, просто переименовываем
                merged = merged.rename(columns={col: orig_col})
            merged.drop(columns=[col], inplace=True)

    cols = ['key'] + [c for c in merged.columns if c != 'key']
    merged = merged[cols]

    rows = merged.to_dict(orient="records")
    save_table_to_csv(out_csv, rows, cols)


def download_jpg(url: str, filename: str):
    ensure_parent_dir(filename)
    try:
        r = fetch_page(url)

        with open(filename, "wb") as f:
            f.write(r.content)

        log_message(f"downloaded: {url}")
        print(f"Картинка {url} успешно сохранена в {filename}")
    except requests.exceptions.RequestException as e:
        log_message(f"error {url}: {e}")
        print(f"Ошибка при загрузке {url}: {e}")


def download_pictures(df: pd.DataFrame, img_dir: str, start_with: str = None):
    started = start_with is None

    for i, row in df.iterrows():
        url = row["avatar_url"]
        key = row["key"]

        if not started:
            if key == start_with:
                started = True
            else:
                continue

        if is_downloaded(url):
            continue

        filename = os.path.join(img_dir, key + ".jpg")

        download_jpg(url, filename)

    log_message("downloaded: all pictures")
    print("Все картинки загружены")


def are_pictures_downloaded():
    return is_event_logged("downloaded: all pictures")


def main():
    categories_dir = os.path.join(DATA_RAW_DIR, "category")
    channels_dir = os.path.join(DATA_RAW_DIR, "channel")
    img_dir = os.path.join(DATA_RAW_DIR, "img")

    categories_csv = os.path.join(DATA_PROCESSED_DIR, "categories.csv")
    channels_csv = os.path.join(DATA_PROCESSED_DIR, "channels.csv")
    edges_csv = os.path.join(DATA_FINAL_DIR, "edges.csv")
    channels_extra_csv = os.path.join(DATA_PROCESSED_DIR, "channels_extra.csv")

    channels_final_csv = os.path.join(DATA_FINAL_DIR, "channels_final.csv")

    if not are_category_pages_downloaded():
        if not is_table_saved(categories_csv):
            get_and_save_categories(categories_csv)
        else:
            print("Таблица с категориями уже сохранена")

        df = open_saved_table(categories_csv)
        download_category_pages(df, categories_dir)
    else:
        print("Страницы категорий уже загружены")

    if not (is_table_saved(channels_csv) and is_table_saved(edges_csv)):
        parse_all_categories(categories_dir, channels_csv, edges_csv)
    else:
        print("Таблица с каналами уже сохранена")

    if not are_channel_pages_downloaded():
        df = open_saved_table(channels_csv)
        download_channel_pages(df, channels_dir, start_with=None)
    else:
        print("Страницы каналов уже загружены")

    if not is_table_saved(channels_extra_csv):
        parse_all_channels(channels_dir, channels_extra_csv)
    else:
        print("Таблица с данными из страниц каналов уже сохранена")

    if not are_pictures_downloaded():
        df = open_saved_table(channels_csv)
        download_pictures(df, img_dir, start_with=None)
    else:
        print("Картинки уже загружены")

    if not is_table_saved(channels_final_csv):
        merge_tables(channels_csv, channels_extra_csv, channels_final_csv)
    else:
        print("Финальная таблица с каналами уже сохранена")


if __name__ == "__main__":
    main()