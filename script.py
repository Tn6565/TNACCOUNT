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

# --- UIデザイン強化 ---
st.set_page_config(page_title="NGワード抽出ツール", page_icon="🔍", layout="centered")

st.title("🔍 NGワードからユーザー抽出")
st.markdown("特定のNGワードを含む投稿から **@ユーザーID** を抽出します。")

# 入力エリア
ng_word = st.text_input("NGワード（複数可、スペース区切り）", placeholder="例: spam scam bot")

max_results = st.slider("取得件数", min_value=10, max_value=100, value=30, step=10)

if st.button("検索開始 🚀"):
    with st.spinner("検索中..."):
        data, error = get_tweets(ng_word, max_results=max_results)
        if error:
            st.error(error)
        elif data and "data" in data:
            user_ids = list({tweet["author_id"] for tweet in data["data"]})
            username_map = get_usernames(user_ids)
            usernames = [username_map.get(uid, f"(不明ID: {uid})") for uid in user_ids]

            st.success(f"抽出されたユーザー: {len(usernames)} 件")
            st.write("\n".join(usernames))

            st.download_button(
                label="📥 ユーザー一覧をダウンロード",
                data="\n".join(usernames),
                file_name="ng_users.txt",
                mime="text/plain"
            )
        else:
            st.warning("該当するツイートはありませんでした。")

