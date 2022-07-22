import argparse
import datetime
import os
import re
import time
import urllib.parse
import warnings
from dataclasses import dataclass
from string import Template
from typing import Tuple

import arxiv
import requests
import slackweb
import yaml
from bs4 import BeautifulSoup
from feedparser import FeedParserDict
from selenium.webdriver import Firefox
from selenium.webdriver.firefox.options import Options
from webdriver_manager.firefox import GeckoDriverManager

# setting
warnings.filterwarnings("ignore")


def get_config(rel_path: str) -> dict:
    file_abs_path = os.path.abspath(__file__)
    file_dir = os.path.dirname(file_abs_path)
    config_path = f"{file_dir}/{rel_path}"
    with open(config_path, "r") as yml:
        config = yaml.safe_load(yml)
    return config


def get_date_range() -> Tuple[datetime.datetime, datetime.datetime]:
    today = datetime.datetime.today()
    date_from = today - datetime.timedelta(days=2)
    date_to = today - datetime.timedelta(days=2)
    if today.weekday() == 0:  # 月曜日の場合
        date_from = today - datetime.timedelta(days=4)  # 木曜日
        date_to = date_from
    elif today.weekday() == 1:  # 火曜日の場合
        date_from = today - datetime.timedelta(days=4)  # 金曜日
        date_to = today - datetime.timedelta(days=2)  # 日曜日
    return date_from, date_to


def calc_score(abst: str, keywords: dict) -> Tuple[float, list]:
    abst = abst.lower().replace("-", " ")
    sum_score = 0.0
    hit_keywords = []

    for word in keywords.keys():
        score = keywords[word]
        if word.lower().replace("-", " ") in abst:
            sum_score += score
            hit_keywords.append(word)
    return sum_score, hit_keywords


def get_text_from_page_source(driver: Firefox) -> str:
    html = driver.page_source
    soup = BeautifulSoup(html, features="lxml")
    target_elem = soup.find(class_="lmt__translations_as_text__text_btn")
    text = target_elem.text
    return text


def get_text_from_driver(driver: Firefox) -> str:
    elem = driver.find_element_by_class_name("lmt__translations_as_text__text_btn")
    text = elem.get_attribute("innerHTML")
    return text


def get_translated_text(
    driver: Firefox, from_lang: str, to_lang: str, from_text: str
) -> str:
    """
    https://qiita.com/fujino-fpu/items/e94d4ff9e7a5784b2987
    """
    # urlencode
    from_text = re.sub(r"([/|\\])", r"\\\1", from_text)
    from_text = urllib.parse.quote(from_text)

    # url作成
    url = (
        "https://www.deepl.com/translator#"
        + from_lang
        + "/"
        + to_lang
        + "/"
        + from_text
    )

    driver.get(url)
    driver.implicitly_wait(10)  # 見つからないときは、10秒まで待つ

    for i in range(30):
        time.sleep(1)  # 指定時間待つ
        to_text = get_text_from_driver(driver)
        if to_text:
            break

    return to_text


def nice_str(obj) -> str:
    if isinstance(obj, list):
        if all(type(elem) is str for elem in obj):
            return ", ".join(obj)
    if type(obj) is str:
        return obj.replace("\n", " ")
    return str(obj)


def search_keyword(articles: list, keywords: dict, config: dict) -> list:
    lang = config.get("lang", "ja")  # optional
    max_posts = int(config.get("max_posts", "-1"))  # optional
    score_threshold = float(config.get("score_threshold", "0"))  # optional
    default_template = (
        "score: `${score}`\n"
        "hit keywords: `${words}`\n"
        "url: ${arxiv_url}\n"
        "title:    ${title_trans}\n"
        "abstract:\n"
        "\t ${summary_trans}\n"
    )
    template = config.get("template", default_template)  # optional
    nodollar = "nodollar" in config.get("flags", [])  # optional

    def with_score(article: FeedParserDict) -> Tuple[FeedParserDict, float, list]:
        score, words = calc_score(article["summary"], keywords)
        return article, score, words

    mapped = map(with_score, articles)
    filtered = filter(lambda x: x[1] >= score_threshold and x[1] != 0, mapped)
    sorted_articles = sorted(filtered, key=lambda x: x[1], reverse=True)

    # ヘッドレスモードでブラウザを起動
    options = Options()
    options.add_argument("--headless")
    # ブラウザーを起動
    driver = Firefox(executable_path=GeckoDriverManager().install(), options=options)

    def translate(data: Tuple[FeedParserDict, float, list]) -> str:
        article, score, words = data

        def format_text(text: str):
            if nodollar:
                text = text.replace("$", "")
            return text.replace("\n", " ")

        title = format_text(article["title"])
        title_trans = get_translated_text(driver, lang, "en", title)
        summary = format_text(article["summary"])
        summary_trans = get_translated_text(driver, lang, "en", summary)

        article_str = {key: nice_str(value) for key, value in article.items()}
        text = Template(template).substitute(
            article_str,
            score=score,
            words=nice_str(words),
            title_trans=title_trans,
            summary_trans=summary_trans,
        )
        return text

    if max_posts == -1:
        max_posts = len(sorted_articles)
    results = list(map(translate, sorted_articles[:max_posts]))
    # ブラウザ停止
    driver.quit()
    return results


def send2app(text: str, slack_id: str, line_token: str, console: bool) -> None:
    # slack
    if slack_id:
        slack = slackweb.Slack(url=slack_id)
        slack.notify(text=text)

    # line
    if line_token:
        line_notify_api = "https://notify-api.line.me/api/notify"
        headers = {"Authorization": f"Bearer {line_token}"}
        data = {"message": f"message: {text}"}
        requests.post(line_notify_api, headers=headers, data=data)

    # console
    if console:
        print(text)


def main() -> None:
    # debug用
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="../config.yaml")
    parser.add_argument("--slack_id", default="")
    parser.add_argument("--line_token", default="")
    args = parser.parse_args()
    config_path = args.config
    slack_id = os.getenv("SLACK_ID", default=args.slack_id)
    line_token = os.getenv("LINE_TOKEN", default=args.line_token)

    config = get_config(config_path)
    subject = config["subject"]  # required
    keywords = config["keywords"]  # required
    console = "console" in config.get("flags", [])  # optional

    date_from, date_to = get_date_range()
    date_from_str = date_from.strftime("%Y%m%d")
    date_to_str = date_to.strftime("%Y%m%d")
    # datetime format YYYYMMDDHHMMSS
    arxiv_query = (
        f"({subject}) AND "
        f"submittedDate:"
        f"[{date_from_str}000000 TO {date_to_str}235959]"
    )
    articles = arxiv.query(
        query=arxiv_query,
        max_results=1000,
        sort_by="submittedDate",
        iterative=False,
    )

    results = search_keyword(articles, keywords, config)

    default_front_template = "\t ${date}\t num of articles = ${num}\n"
    front_template = config.get("front_matter", default_front_template)  # optional
    front_dict = {"num": len(results), "date": date_to.strftime("%Y-%m-%d")}
    front_matter = Template(front_template).substitute(front_dict)
    if front_matter:
        send2app(front_matter, slack_id, line_token, console)
    for text in results:
        send2app(text, slack_id, line_token, console)


if __name__ == "__main__":
    main()
