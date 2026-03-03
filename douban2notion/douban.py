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


def _proxy_image_url(original_url: str) -> str:
    """
    把豆瓣图片外链转换为通过 wsrv.nl 图片代理访问的 URL
    示例：
      in:  https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2544866651.jpg
      out: https://wsrv.nl/?url=https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2544866651.jpg
    """
    if not original_url:
        return ""
    # 使用 wsrv.nl 图片代理服务（原 images.weserv.nl）
    return f"https://wsrv.nl/?url={original_url}"


def get_douban_image_url(photo_page_url: str) -> str:
    """
    从豆瓣图片页面提取实际图片URL
    示例：
      in:  https://movie.douban.com/photos/photo/2915119069/#title-anchor
      out: https://img1.doubanio.com/view/photo/s_ratio_poster/public/p2915119069.jpg
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://movie.douban.com/",
    }
    try:
        response = requests.get(photo_page_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        # 查找图片元素，豆瓣图片页面的图片通常在 <img> 标签中，class 可能为 "photo-img" 或类似
        img_tag = soup.find("img", {"class": "photo-img"})
        if img_tag and img_tag.get("src"):
            return img_tag["src"]
        # 如果找不到，尝试其他可能的标签
        img_tag = soup.find("img")
        if img_tag and img_tag.get("src"):
            return img_tag["src"]
    except Exception as e:
        print(f"提取图片URL失败: {e}")
    return ""


def download_image_to_temp(url: str) -> str:
    """
    下载图片到临时文件，返回临时文件路径
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        # 创建临时文件（保留文件名后缀，比如 .jpg）
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_file.write(response.content)
            return temp_file.name
    except Exception as e:
        print(f"下载图片失败: {e}")
        return ""


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
            if (
                notion_movive.get("日期") != movie.get("日期")
                or notion_movive.get("短评") != movie.get("短评")
                or notion_movive.get("状态") != movie.get("状态")
                or notion_movive.get("评分") != movie.get("评分")
                or not notion_movive.get("演员")
                or not notion_movive.get("IMDB")
            ):
                if not notion_movive.get("演员") and subject.get("actors"):
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
                if not notion_movive.get("IMDB"):
                    movie["IMDB"] = get_imdb(movie.get("豆瓣链接"))
                properties = utils.get_properties(movie, movie_properties_type_dict)
                print(movie.get("电影名"))
                notion_helper.get_date_relation(properties, create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties,
                )

        else:
            print(f"插入{movie.get('电影名')}")
            # === 关键修改：从豆瓣图片页面提取实际图片URL ===
            cover_url = subject.get("pic").get("normal")
            if cover_url:
                cover = get_douban_image_url(cover_url)
            else:
                cover = ""
            # 使用图片代理
            cover_proxied = _proxy_image_url(cover)
            # 下载图片到临时文件
            temp_file_path = download_image_to_temp(cover_proxied)
            if temp_file_path:
                # 上传到 Notion
                file_id = notion_helper.upload_file(temp_file_path)
                # 删除临时文件
                os.remove(temp_file_path)
                if file_id:
                    # 设置封面为文件（Notion 要求的格式）
                    properties["封面"] = {
                        "files": [
                            {
                                "type": "file",
                                "name": "cover.jpg",
                                "file": {
                                    "id": file_id
                                }
                            }
                        ]
                    }
                else:
                    # 上传失败，使用外部链接作为备用
                    properties["封面"] = {
                        "files": [
                            {
                                "type": "external",
                                "name": "cover.jpg",
                                "external": {
                                    "url": cover_proxied
                                }
                            }
                        ]
                    }
            else:
                # 下载失败，使用外部链接作为备用
                properties["封面"] = {
                    "files": [
                        {
                            "type": "external",
                            "name": "cover.jpg",
                            "external": {
                                "url": cover_proxied
                            }
                        }
                    ]
                }
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
            # icon 也用代理后的封面 URL，保证图标也能正常显示
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(cover_proxied)
            )


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
        # === 关键修改：从豆瓣图片页面提取实际图片URL ===
        cover_url = subject.get("pic").get("large")
        if cover_url:
            cover = get_douban_image_url(cover_url)
        else:
            cover = ""
        # 使用图片代理
        cover_proxied = _proxy_image_url(cover)
        # 下载图片到临时文件
        temp_file_path = download_image_to_temp(cover_proxied)
        if temp_file_path:
            # 上传到 Notion
            file_id = notion_helper.upload_file(temp_file_path)
            # 删除临时文件
            os.remove(temp_file_path)
            if file_id:
                # 设置封面为文件（Notion 要求的格式）
                properties["封面"] = {
                    "files": [
                        {
                            "type": "file",
                            "name": "cover.jpg",
                            "file": {
                                "id": file_id
                            }
                        }
                    ]
                }
            else:
                # 上传失败，使用外部链接作为备用
                properties["封面"] = {
                    "files": [
                        {
                            "type": "external",
                            "name": "cover.jpg",
                            "external": {
                                "url": cover_proxied
                            }
                        }
                    ]
                }
        else:
            # 下载失败，使用外部链接作为备用
            properties["封面"] = {
                "files": [
                    {
                        "type": "external",
                        "name": "cover.jpg",
                        "external": {
                            "url": cover_proxied
                        }
                    }
                ]
            }
        if result.get("rating"):
            book["评分"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            book["短评"] = result.get("comment")
        if notion_book_dict.get(book.get("豆瓣链接")):
            notion_movive = notion_book_dict.get(book.get("豆瓣链接"))
            if (
                notion_movive.get("封面") is None
                or notion_movive.get("封面") != book.get("封面")
                or notion_movive.get("日期") != book.get("日期")
                or notion_movive.get("短评") != book.get("短评")
                or notion_movive.get("状态") != book.get("状态")
                or notion_movive.get("评分") != book.get("评分")
            ):
                print(f"更新{book.get('书名')}")
                properties = utils.get_properties(book, book_properties_type_dict)
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
            # icon 也用代理后的封面 URL
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(cover_proxied)
            )


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
