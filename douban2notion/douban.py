import argparse
from email import feedparser
import json
import os
import re
from bs4 import BeautifulSoup
import pendulum
from retrying import retry
import requests
from douban2notion.notion_helper import NotionHelper
from douban2notion import utils
import tempfile

DOUBAN_API_HOST = os.getenv("DOUBAN_API_HOST", "frodo.douban.com")
DOUBAN_API_KEY = os.getenv("DOUBAN_API_KEY", "0ac44ae016490db2204ce0a042db2916")

from douban2notion.config import (
    movie_properties_type_dict,
    book_properties_type_dict,
    TAG_ICON_URL,
    USER_ICON_URL,
)
from douban2notion.utils import get_icon
from dotenv import load_dotenv

load_dotenv()

rating = {
    1: "⭐️",
    2: "⭐️⭐️",
    3: "⭐️⭐️⭐️",
    4: "⭐️⭐️⭐️⭐️",
    5: "⭐️⭐️⭐️⭐️⭐️",
}
movie_status = {
    "mark": "想看",
    "doing": "在看",
    "done": "看过",
}
book_status = {
    "mark": "想读",
    "doing": "在读",
    "done": "读过",
}
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

headers = {
    "host": DOUBAN_API_HOST,
    "authorization": f"Bearer {AUTH_TOKEN}" if AUTH_TOKEN else "",
    "user-agent": "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 15_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.16(0x18001023) NetType/WIFI Language/zh_CN",
    "referer": "https://servicewechat.com/wx2f9b06c1de1ccfca/84/page-frame.html",
}


IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Referer": "https://movie.douban.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _proxy_image_url(original_url: str) -> str:
    """兼容保留：wsrv.nl 图片代理（豆瓣已加 Referer 校验，仅作兜底，不再作为主链路）"""
    if not original_url:
        return ""
    return f"https://wsrv.nl/?url={original_url}"


def download_image_to_temp(url: str) -> str:
    """下载豆瓣封面到临时文件。

    豆瓣自 2026 年起对图片做 Referer 校验：不带 Referer 直接访问返回 418，
    Notion 服务端抓外链同理失败，因此必须由本进程下载后再上传。
    """
    if not url:
        return ""
    try:
        response = requests.get(url, headers=IMAGE_HEADERS, timeout=20)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            print(f"封面下载失败：非图片响应 ({response.status_code} {content_type})")
            return ""
        suffix = ".jpg"
        if "png" in content_type:
            suffix = ".png"
        elif "webp" in content_type:
            suffix = ".webp"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(response.content)
            return temp_file.name
    except Exception as e:
        print(f"下载图片失败: {e}")
        return ""


def cover_needs_fix(cover_value) -> bool:
    """判断 Notion 现有封面是否需要重抓。

    豆瓣已对图片做 Referer 校验：旧的外链封面（doubanio/douban 域名或 wsrv 代理）
    在 Notion 里必然显示为空白，因此这类值也要视为「需要修复」。
    """
    if not cover_value:
        return True
    value = str(cover_value)
    return (
        "doubanio.com" in value
        or "douban.com" in value
        or "wsrv.nl" in value
        or "weserv.nl" in value
    )


def upload_cover(notion_helper, pic_url: str):
    """下载封面并上传到 Notion，返回 (files 属性, file_upload_id)；任一步失败返回 (None, None)"""
    temp_file_path = download_image_to_temp(pic_url)
    if not temp_file_path:
        return None, None
    try:
        file_id = notion_helper.upload_file(temp_file_path)
    finally:
        try:
            os.remove(temp_file_path)
        except OSError:
            pass
    if not file_id:
        return None, None
    return {
        "files": [
            {"type": "file_upload", "file_upload": {"id": file_id}, "name": "cover.jpg"}
        ]
    }, file_id


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def fetch_subjects(user, type_, status):
    offset = 0
    page = 0
    url = f"https://{DOUBAN_API_HOST}/api/v2/user/{user}/interests"
    total = 0
    results = []
    while True:
        params = {
            "type": type_,
            "count": 50,
            "status": status,
            "start": offset,
            "apiKey": DOUBAN_API_KEY,
        }
        response = requests.get(url, headers=headers, params=params)

        if response.ok:
            response = response.json()
            interests = response.get("interests")
            if len(interests) == 0:
                break
            results.extend(interests)
            print(f"total = {total}")
            print(f"size = {len(results)}")
            page += 1
            offset = page * 50
    return results


def insert_movie(douban_name, notion_helper):
    notion_movies = notion_helper.query_all(
        database_id=notion_helper.movie_database_id
    )
    notion_movie_dict = {}
    for i in notion_movies:
        movie = {}
        for key, value in i.get("properties").items():
            movie[key] = utils.get_property_value(value)
        notion_movie_dict[movie.get("豆瓣链接")] = {
            "短评": movie.get("短评"),
            "状态": movie.get("状态"),
            "日期": movie.get("日期"),
            "评分": movie.get("评分"),
            "演员": movie.get("演员"),
            "IMDB": movie.get("IMDB"),
            "封面": movie.get("封面"),
            "page_id": i.get("id"),
        }
    results = []
    for i in movie_status.keys():
        results.extend(fetch_subjects(douban_name, "movie", i))
    for result in results:
        movie = {}
        if not result:
            print(result)
            continue
        subject = result.get("subject")
        movie["电影名"] = subject.get("title")
        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time, tz=utils.tz)
        # 时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)
        movie["日期"] = create_time.int_timestamp
        movie["豆瓣链接"] = subject.get("url")
        movie["状态"] = movie_status.get(result.get("status"))
        if result.get("rating"):
            movie["评分"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            movie["短评"] = result.get("comment")
        if notion_movie_dict.get(movie.get("豆瓣链接")):
            notion_movive = notion_movie_dict.get(movie.get("豆瓣链接"))
            # 只在「豆瓣确实有数据、而 Notion 缺」时才补，避免每次运行做无效重写
            # （豆瓣对「想看」条目通常不返回演员，对剧集通常没有 IMDb）
            missing_actors = bool(subject.get("actors")) and not notion_movive.get("演员")
            missing_imdb = (
                subject.get("type") in (None, "movie", "电影")
                and not notion_movive.get("IMDB")
            )
            if (
                notion_movive.get("日期") != movie.get("日期")
                or notion_movive.get("短评") != movie.get("短评")
                or notion_movive.get("状态") != movie.get("状态")
                or notion_movive.get("评分") != movie.get("评分")
                or missing_actors
                or missing_imdb
                or cover_needs_fix(notion_movive.get("封面"))
            ):
                if missing_actors:
                    l = []
                    actors = subject.get("actors")[0:5]
                    for actor in actors:
                        if actor.get("name"):
                            if "/" in actor.get("name"):
                                l.extend(actor.get("name").split("/"))
                            else:
                                l.append(actor.get("name"))
                    movie["演员"] = [
                        notion_helper.get_relation_id(
                            x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                        )
                        for x in actors
                    ]
                if missing_imdb:
                    movie["IMDB"] = get_imdb(movie.get("豆瓣链接"))
                properties = utils.get_properties(movie, movie_properties_type_dict)
                if cover_needs_fix(notion_movive.get("封面")):
                    cover_prop, _ = upload_cover(
                        notion_helper, (subject.get("pic") or {}).get("normal")
                    )
                    if cover_prop:
                        properties["封面"] = cover_prop
                print(movie.get("电影名"))
                notion_helper.get_date_relation(properties, create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties,
                )

        else:
            print(f"插入{movie.get('电影名')}")
            movie["类型"] = subject.get("type")
            if subject.get("genres"):
                movie["分类"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in subject.get("genres")
                ]
            if subject.get("actors"):
                l = []
                actors = subject.get("actors")[0:5]
                for actor in actors:
                    if actor.get("name"):
                        if "/" in actor.get("name"):
                            l.extend(actor.get("name").split("/"))
                        else:
                            l.append(actor.get("name"))
                movie["演员"] = [
                    notion_helper.get_relation_id(
                        x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                    )
                    for x in actors
                ]
            if subject.get("directors"):
                movie["导演"] = [
                    notion_helper.get_relation_id(
                        x.get("name"), notion_helper.director_database_id, USER_ICON_URL
                    )
                    for x in subject.get("directors")[0:5]
                ]
            properties = utils.get_properties(movie, movie_properties_type_dict)
            notion_helper.get_date_relation(properties, create_time)
            parent = {
                "database_id": notion_helper.movie_database_id,
                "type": "database_id",
            }
            cover_prop, cover_file_id = upload_cover(
                notion_helper, (subject.get("pic") or {}).get("normal")
            )
            if cover_prop:
                properties["封面"] = cover_prop
            icon = {"type": "file_upload", "file_upload": {"id": cover_file_id}} if cover_file_id else None
            notion_helper.create_page(parent=parent, properties=properties, icon=icon)


def get_imdb(link):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36'
    }
    response = requests.get(link, headers=headers)
    soup = BeautifulSoup(response.content)
    info = soup.find(id='info')
    if info:
        for span in info.find_all('span', {'class': 'pl'}):
            if ('IMDb:' == span.string):
                return span.next_sibling.string.strip()


def insert_book(douban_name, notion_helper):
    notion_books = notion_helper.query_all(
        database_id=notion_helper.book_database_id
    )
    notion_book_dict = {}
    for i in notion_books:
        book = {}
        for key, value in i.get("properties").items():
            book[key] = utils.get_property_value(value)
        notion_book_dict[book.get("豆瓣链接")] = {
            "短评": book.get("短评"),
            "状态": book.get("状态"),
            "日期": book.get("日期"),
            "评分": book.get("评分"),
            "封面": book.get("封面"),
            "page_id": i.get("id"),
        }
        print(i)
    print(f"notion {len(notion_book_dict)}")
    results = []
    for i in book_status.keys():
        results.extend(fetch_subjects(douban_name, "book", i))
    for result in results:
        book = {}
        if not result:
            continue
        subject = result.get("subject")
        book["书名"] = subject.get("title")
        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time, tz=utils.tz)
        # 时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)
        book["日期"] = create_time.int_timestamp
        book["豆瓣链接"] = subject.get("url")
        book["状态"] = book_status.get(result.get("status"))
        if result.get("rating"):
            book["评分"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            book["短评"] = result.get("comment")
        if notion_book_dict.get(book.get("豆瓣链接")):
            notion_movive = notion_book_dict.get(book.get("豆瓣链接"))
            if (
                cover_needs_fix(notion_movive.get("封面"))
                or notion_movive.get("日期") != book.get("日期")
                or notion_movive.get("短评") != book.get("短评")
                or notion_movive.get("状态") != book.get("状态")
                or notion_movive.get("评分") != book.get("评分")
            ):
                print(f"更新{book.get('书名')}")
                properties = utils.get_properties(book, book_properties_type_dict)
                if cover_needs_fix(notion_movive.get("封面")):
                    cover_prop, _ = upload_cover(
                        notion_helper, (subject.get("pic") or {}).get("large")
                    )
                    if cover_prop:
                        properties["封面"] = cover_prop
                notion_helper.get_date_relation(properties, create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties,
                )

        else:
            print(f"插入{book.get('书名')}")
            book["简介"] = subject.get("intro")
            press = []
            for i in subject.get("press"):
                press.extend(i.split(","))
            book["出版社"] = press
            book["类型"] = subject.get("type")
            if result.get("tags"):
                book["分类"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in result.get("tags")
                ]
            if subject.get("author"):
                book["作者"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.author_database_id, USER_ICON_URL
                    )
                    for x in subject.get("author")[0:100]
                ]
            properties = utils.get_properties(book, book_properties_type_dict)
            notion_helper.get_date_relation(properties, create_time)
            parent = {
                "database_id": notion_helper.book_database_id,
                "type": "database_id",
            }
            cover_prop, cover_file_id = upload_cover(
                notion_helper, (subject.get("pic") or {}).get("large")
            )
            if cover_prop:
                properties["封面"] = cover_prop
            icon = {"type": "file_upload", "file_upload": {"id": cover_file_id}} if cover_file_id else None
            notion_helper.create_page(parent=parent, properties=properties, icon=icon)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("type")
    options = parser.parse_args()
    type = options.type
    notion_helper = NotionHelper(type)
    is_movie = True if type == "movie" else False
    douban_name = os.getenv("DOUBAN_NAME", None)
    if is_movie:
        insert_movie(douban_name, notion_helper)
    else:
        insert_book(douban_name, notion_helper)


if __name__ == "__main__":
    main()
