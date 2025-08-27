import os
import requests
import streamlit as st
from dotenv import load_dotenv

# 環境変数読み込み
load_dotenv()
BEARER_TOKEN = os.getenv("TNSS_BEARER_TOKEN")

headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
search_url = "https://api.x.com/2/tweets/search/recent"
user_url = "https://api.x.com/2/users"

def get_tweets(ng_word, max_results=10):
    query = f"{ng_word} -is:retweet"
    params = {
        "query": query,
        "tweet.fields": "author_id",
        "max_results": max_results
    }
    response = requests.get(search_url, headers=headers, params=params)
    if response.status_code != 200:
        return None, f"APIエラー: {response.status_code} {response.text}"
    return response.json(), None

def get_usernames(user_ids):
    params = {
        "ids": ",".join(user_ids),
        "user.fields": "username"
    }
    response = requests.get(user_url, headers=headers, params=params)
    if response.status_code != 200:
        return {}
    users_data = response.json().get("data", [])
    return {u["id"]: f"@{u['username']}" for u in users_data}

# Streamlit UI
st.title("NGワードから@ユーザーID抽出")

ng_word = st.text_input("NGワードを入力（複数可、スペース区切り）")
if st.button("検索実行"):
    data, error = get_tweets(ng_word)
    if error:
        st.error(error)
    elif data and "data" in data:
        user_ids = list({tweet["author_id"] for tweet in data["data"]})
        username_map = get_usernames(user_ids)
        usernames = [username_map.get(uid, f"(不明ID: {uid})") for uid in user_ids]
        st.success("抽出されたユーザー:")
        st.write("\n".join(usernames))
    else:
        st.warning("該当するツイートはありませんでした。")
