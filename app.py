# ------------------------------------------------
# このプログラムは，JR神戸線遅延通知システムです．
# ------------------------------------------------
from flask import Flask, request, abort

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError, LineBotApiError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FollowEvent, TemplateSendMessage, MessageAction,
    ButtonsTemplate)

import os

import pandas as pd

import time
import datetime

import requests # WebからHTMLをダウンロードするため
import time # WebからHTMLをダウンロードする際に一定秒数待つため
import pandas as pd

import pytz
import gspread
from oauth2client.service_account import ServiceAccountCredentials

import re

import json
from bs4 import BeautifulSoup # BeautifulSoup を使えるようにする
import schedule
import threading


# ------------------------------------------------
# 以下は遅延取得関数(JRの公式ページから情報をとってくる)
# ------------------------------------------------
def get_delay_data():
    import time
    url = "https://trafficinfo.westjr.co.jp/kinki_history.html#1" # 取得したいウェブページのURL
    time.sleep(3) # 3秒待つ
    r = requests.get(url) # データを取得する
    print("got_url")
    r.encoding = r.apparent_encoding #文字コードを設定する
    soup = BeautifulSoup(r.content, "html.parser")  # htmlをBeautifulSoupで読み込む
    print("read_html")

    # 京阪神地区のみを取得する
    table1 = soup.find("caption", text="京阪神地区履歴一覧").find_parent("table")
    td_list = table1.find_all("td")
    delay_list = []
    for td in td_list:
        delay_list.append(td.text)
    delay_list
    d = {}
    for i in range(len(delay_list)):
        if re.findall("年|月", delay_list[i]):
            day = delay_list[i]
            time = "0時0分"
        elif re.findall("時|分", delay_list[i]):
            time = delay_list[i]
        else:
            d[day+time] = delay_list[i]


    # 取得したデータをデータフレームとして処理する
    df = pd.DataFrame(d.values(), index=d.keys())
    df = df.reset_index().rename(columns={"index":"date", 0:"content"})
    df["date"] = pd.to_datetime(df["date"], format="%Y年%m月%d日%H時%M分")
    df = df.set_index("date")
    # 神戸線または．京都線で遅延が発生していた場合にそのデータを抽出する
    df = df[(df["content"].str.contains("神戸線")) | (df["content"].str.contains("京都線"))]

    return df

# ---------------------------------------------------
# 以下はサーバー，LINEbotにアクセスする処理
# ---------------------------------------------------

app = Flask(__name__)

YOUR_CHANNEL_ACCESS_TOKEN = "hoge"
YOUR_CHANNEL_SECRET="hoge"

line_bot_api = LineBotApi(YOUR_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(YOUR_CHANNEL_SECRET)

# サーバーをスリープさせない処理
@app.route("/debug")
def hello_world():
    return "Hello World"

# 遅延通知送信テスト用
@app.route("/notification_test")
def test():
    text_messages = "これはテスト配信です。"\
    "\n1時間以内に遅延が発生した可能性があります。"\
    "\n詳しくは公式HPを参照して下さい。"\
    "\nhttps://trafficinfo.westjr.co.jp/kinki_history.html#1"
    try:
        line_bot_api.broadcast(TextSendMessage(text=text_messages))
    except LineBotApiError as e:
        print(e)

    print("test_finished")
    return "Hello World"


@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        # 署名の検証を行い，成功した場合にhandleされたメソッドを返す
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

# ---------------------------------------------------
# LINE MEssaging APIの処理
# フォロー時の処理とメッセージを受けた際の処理をする
# ---------------------------------------------------

# follow時の処理
@handler.add(FollowEvent)
def handle_follow(event):
    app.logger.info("Got Follow event:" + event.source.user_id)
    line_bot_api.reply_message(
        event.reply_token, TextSendMessage(
            text='フォローありがとうございます。'\
            '\nこのbotはJR神戸線の遅延情報を通知でお知らせします！'\
            '\n「遅延はある？」と送信すると．現在の運行状況をお届けします！'))
    print("finish follow event")


# repeat or get profile
@handler.add(MessageEvent, message=TextMessage)
def response_message(event):
    profile = line_bot_api.get_profile(event.source.user_id)
    message = event.message.text
    if message == "profile":
        messages = TemplateSendMessage(alt_text="Buttons template",
                                   template=ButtonsTemplate(
                                       thumbnail_image_url=profile.picture_url,
                                       title=profile.display_name,
                                       text=f"User Id: {profile.user_id[:5]}...\n"))
        line_bot_api.reply_message(event.reply_token, messages=messages)

    elif (message == "遅延はある？") or (message=="遅延はある?"):
        text_messages = reply_delay_message()
        line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=text_messages))

    else:
        my_text = "ごめんなさい。そのメッセージには現在対応していません。"
        my_text = event.message.text
        line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=my_text))

    print("repeat, get profile, or send delay message completed")

# --------------------------------------------------
# 取得したデータフレームの最新行が現時刻から4時間以内なら通知する
# --------------------------------------------------
# ---------------------------------------------------
"""
1. １0分おきに遅延情報を取得し，最新データを記録
2. 遅延があれば，通知する
"""
# ---------------------------------------------------

# 常に回り続けてデータを取得する関数
def my_func():
    while True:
        # 関数呼び出しでデータを取得
        df = get_delay_data()
        dt_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        # 日付の差分
        date_delta = abs(df.index[0].date() - dt_now.date())
        # 遅延発生時が今日の日付かどうかかで分岐
        if (df.index[0].day - dt_now.day) == 0:
            if abs(df.index[0].hour - dt_now.hour) <= 1:

                text_messages = "神戸線または京都線で遅延が発生しました。"\
                "\nhttps://trafficinfo.westjr.co.jp/kinki_history.html#1"\

                try:
                    line_bot_api.broadcast(TextSendMessage(text=text_messages))
                except LineBotApiError as e:
                    print(e)

        else:
            text_messages="現在遅れはありません."\
            "\n詳しくはこちら..."\
            "\nhttps://trafficinfo.westjr.co.jp/kinki_history.html#1"


        print("task_finished")
        time.sleep(900)

# 呼び出された際に動いてデータを取得する関数
def reply_delay_message():
    # 関数呼び出しでデータを取得
    df = get_delay_data()
    dt_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    # 日付の差分
    date_delta = abs(df.index[0].date() - dt_now.date())
    # 遅延発生時が今日の日付かどうかかで分岐
    if (df.index[0].day - dt_now.day) == 0:
        if abs(df.index[0].hour - dt_now.hour) <= 1:
            text_messages = "神戸線または京都線で遅延が発生しました。"\
            "\n詳しくは公式HPを参照して下さい。"\
            "\nhttps://trafficinfo.westjr.co.jp/kinki_history.html#1"
            print("ok")
        else:
            text_messages = "本日遅延が発生しました。"\
                "\n詳しくは公式HPを参照して下さい。"\
                "\nhttps://trafficinfo.westjr.co.jp/kinki_history.html#1"
    else:
        text_messages = "現在、遅延は発生していません。"\
        "\n詳しくは公式HPを参照して下さい..."\
        "\nhttps://trafficinfo.westjr.co.jp/kinki_history.html#1"

    print("replying delay message finished")
    return text_messages


# --------------------------------------------------
# サーバーを動かす処理
# --------------------------------------------------
if __name__ == "__main__":
    port = os.getenv("PORT")
    thread = threading.Thread(target=my_func)
    thread.start()
    app.run(host="0.0.0.0", port=port)
