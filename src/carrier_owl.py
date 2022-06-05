import argparse
import datetime
import os
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
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from webdriver_manager.firefox import GeckoDriverManager

# setting
warnings.filterwarnings("ignore")


@dataclass
class Result:
    article: FeedParserDict
    title_trans: str
    summary_trans: str
    words: list
    score: float = 0.0


def get_config() -> dict:
    file_abs_path = os.path.abspath(__file__)
    file_dir = os.path.dirname(file_abs_path)
    config_path = f"{file_dir}/../config.yaml"
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
    hit_kwd_list = []

    for word in keywords.keys():
        score = keywords[word]
        if word.lower().replace("-", " ") in abst:
            sum_score += score
            hit_kwd_list.append(word)
    return sum_score, hit_kwd_list


def get_text_from_page_source(html: str) -> str:
    soup = BeautifulSoup(html, features="lxml")
    target_elem = soup.find(class_="lmt__translations_as_text__text_btn")
    text = target_elem.text
    return text


def get_translated_text(
    driver: webdriver.Firefox, from_lang: str, to_lang: str, from_text: str
) -> str:
    """
    https://qiita.com/fujino-fpu/items/e94d4ff9e7a5784b2987
    """

    sleep_time = 1

    # urlencode
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
        # 指定時間待つ
        time.sleep(sleep_time)
        html = driver.page_source
        to_text = get_text_from_page_source(html)

        if to_text:
            break

    return to_text


def search_keyword(articles: list, keywords: dict, config: dict) -> list:
    lang = config.get("lang", "ja")  # optional
    max_posts = int(config.get("max_posts", "-1"))  # optional
    score_threshold = float(config.get("score_threshold", "0"))  # optional

    def convert(article: FeedParserDict) -> Tuple[FeedParserDict, list, float]:
        score, words = calc_score(article["summary"], keywords)
        return article, words, score

    converted = map(convert, articles)
    filtered = filter(lambda x: x[2] != 0 and x[2] >= score_threshold, converted)
    raw = sorted(filtered, key=lambda x: x[2], reverse=True)

    # ヘッドレスモードでブラウザを起動
    options = Options()
    options.add_argument("--headless")
    # ブラウザーを起動
    driver = webdriver.Firefox(
        executable_path=GeckoDriverManager().install(), options=options
    )

    def raw2result(raw_result: Tuple[FeedParserDict, list, float]) -> Result:
        article, words, score = raw_result
        title = article["title"].replace("/", "／").replace("$", "").replace("\n", " ")
        title_trans = get_translated_text(driver, lang, "en", title).replace("／", "/")
        summary = (
            article["summary"].replace("/", "／").replace("$", "").replace("\n", " ")
        )
        summary_trans = get_translated_text(driver, lang, "en", summary).replace(
            "／", "/"
        )
        # summary_trans = textwrap.wrap(summary_trans, 40)  # 40行で改行
        # summary_trans = '\n'.join(summary_trans)
        return Result(
            article=article,
            title_trans=title_trans,
            summary_trans=summary_trans,
            words=words,
            score=score,
        )

    result = list(map(raw2result, raw[:max_posts]))
    # ブラウザ停止
    driver.quit()
    return result


def send2app(text: str, slack_id: str, line_token: str) -> None:
    # slack
    if slack_id is not None:
        slack = slackweb.Slack(url=slack_id)
        slack.notify(text=text)

    # line
    if line_token is not None:
        line_notify_api = "https://notify-api.line.me/api/notify"
        headers = {"Authorization": f"Bearer {line_token}"}
        data = {"message": f"message: {text}"}
        requests.post(line_notify_api, headers=headers, data=data)


def nice_str(obj) -> str:
    if isinstance(obj, list):
        if all(type(elem) is str for elem in obj):
            return ", ".join(obj)
    if type(obj) is str:
        return obj.replace("\n", " ")
    return str(obj)


def notify(results: list, template: str, slack_id: str, line_token: str) -> None:
    # 通知
    star = "*" * 80
    for result in results:
        article = result.article
        article_str = {key: nice_str(article[key]) for key in article.keys()}
        title_trans = result.title_trans
        summary_trans = result.summary_trans
        words = nice_str(result.words)
        score = result.score

        text = Template(template).substitute(
            article_str,
            words=words,
            score=score,
            title_trans=title_trans,
            summary_trans=summary_trans,
            star=star,
        )

        send2app(text, slack_id, line_token)


def main() -> None:
    # debug用
    parser = argparse.ArgumentParser()
    parser.add_argument("--slack_id", default=None)
    parser.add_argument("--line_token", default=None)
    args = parser.parse_args()
    slack_id = os.getenv("SLACK_ID") or args.slack_id
    line_token = os.getenv("LINE_TOKEN") or args.line_token

    config = get_config()
    subject = config["subject"]  # required
    keywords = config["keywords"]  # required
    default_template = (
        "\n score: `${score}`"
        "\n hit keywords: `${words}`"
        "\n url: ${arxiv_url}"
        "\n title:    ${title_trans}"
        "\n abstract:"
        "\n \t ${summary_trans}"
        "\n ${star}"
    )
    template = config.get("template", default_template)  # optional

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

    narticles = len(results)
    date_to_str = date_to.strftime("%Y-%m-%d")
    text = f"{narticles} posts on {date_to_str}\n" + "￣" * 23
    send2app(text, slack_id, line_token)
    notify(results, template, slack_id, line_token)


if __name__ == "__main__":
    main()
