# app.py
import os
import pandas as pd
import requests
from datetime import datetime, timedelta
import streamlit as st
from dotenv import load_dotenv

# -------------------------------
# .env ファイルを読み込む
load_dotenv()

# API1からBearer Tokenを取得
BEARER_TOKEN = os.environ.get("TNSS_BEARER_TOKEN")
if BEARER_TOKEN is None:
    st.error("環境変数 API1 が設定されていません。")
    st.stop()

# -------------------------------
# X API検索関数
def search_tweets(query, max_results=10):
    # max_results調整（ユーザーには表示せず内部処理のみ）
    if max_results < 10:
        max_results = 10
    elif max_results > 100:
        max_results = 100

    url = "https://api.twitter.com/2/tweets/search/recent"
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
    params = {
        "query": query,
        "max_results": max_results,
        "tweet.fields": "author_id,created_at,text"
    }

    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 429:
        st.error("APIの呼び出し回数が多すぎます。数分待ってから再度実行してください。")
        return []
    elif response.status_code != 200:
        st.error(f"X APIエラー: {response.status_code} {response.text}")
        return []
    
    tweets = response.json().get("data", [])

    # 過去7日分のみ取得（無料プラン制限）
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    filtered_tweets = [
        t for t in tweets
        if datetime.strptime(t["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ") >= seven_days_ago
    ]

    return filtered_tweets

# -------------------------------
# キャッシュ付き検索関数
@st.cache_data(ttl=300)  # 同じクエリは5分間キャッシュ
def search_tweets_cached(query, max_results):
    return search_tweets(query, max_results)

# -------------------------------
# Streamlit UI
st.title("NGワード投稿ユーザー抽出ツール（無料プラン対応）")

# 注意文言はUI上に表示しないためコメントアウト
# st.info("無料プランの制限は内部で管理されています。")

# 入力
ng_words_input = st.text_area("NGワードをカンマ区切りで入力", "spam,scam")
max_results = st.number_input("取得件数", min_value=10, max_value=100, value=10)

if st.button("検索実行"):
    ng_words = [w.strip() for w in ng_words_input.split(",") if w.strip()]
    if not ng_words:
        st.warning("NGワードを1つ以上入力してください。")
        st.stop()
    
    # NGワードをまとめて1回の検索にする
    query = " OR ".join(ng_words)
    
    tweets = search_tweets_cached(query, max_results)
    
    if not tweets:
        st.info("該当投稿はありませんでした。")
        st.stop()
    
    # データフレーム作成
    data = [
        {"ユーザーID": t["author_id"], "投稿内容": t["text"], "作成日時": t["created_at"]}
        for t in tweets
    ]
    df = pd.DataFrame(data)
    
    # 表示
    st.subheader("抽出結果")
    st.dataframe(df)
    
    # CSV保存ボタン
    csv = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button("CSVとしてダウンロード", data=csv, file_name="ng_users.csv", mime="text/csv")
