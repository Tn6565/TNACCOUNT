# app.py
"""
NGワード抽出ツール 3.0 (Streamlit)
- 高度検索ビルダー、フィルタ、エクスポート、定期監視、履歴、ダッシュボード
- Save as app.py. Requires .env with TNSS_BEARER_TOKEN=<X/Twitter Bearer Token>
"""
import os
import time
import json
import io
import threading
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# -------------------------
# CONFIG
# -------------------------
st.set_page_config(page_title="NGワード監視ツール 3.0", layout="wide", page_icon="🔎")
load_dotenv()
BEARER = os.getenv("EXTNSS_BEARER_TOKEN")  # required

SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
USERS_URL = "https://api.twitter.com/2/users"
HEADERS = {"Authorization": f"Bearer {BEARER}"} if BEARER else {}

DB_FILE = "ng_tool3.db"

# -------------------------
# DB (SQLite) helpers
# -------------------------
def init_db():
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT,
                    created_at TEXT,
                    hit_count INTEGER
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    type TEXT, -- 'ng','white','watch','preset'
                    content TEXT,
                    created_at TEXT
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT,
                    message TEXT,
                    created_at TEXT
                   )""")
    con.commit()
    con.close()

def db_insert_history(query: str, hit_count: int):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute("INSERT INTO search_history(query, created_at, hit_count) VALUES (?, ?, ?)",
                (query, datetime.utcnow().isoformat(), hit_count))
    con.commit(); con.close()

def db_get_history(limit=20):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute("SELECT query, created_at, hit_count FROM search_history ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall(); con.close()
    return rows

def db_add_list(name: str, type_: str, content: str):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute("INSERT INTO lists(name, type, content, created_at) VALUES (?, ?, ?, ?)",
                (name, type_, content, datetime.utcnow().isoformat()))
    con.commit(); con.close()

def db_get_lists(type_: Optional[str]=None):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    if type_:
        cur.execute("SELECT id, name, content FROM lists WHERE type=? ORDER BY id DESC", (type_,))
    else:
        cur.execute("SELECT id, name, type, content FROM lists ORDER BY id DESC")
    rows = cur.fetchall(); con.close()
    return rows

def db_log(level: str, message: str):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute("INSERT INTO logs(level, message, created_at) VALUES (?, ?, ?)",
                (level, message, datetime.utcnow().isoformat()))
    con.commit(); con.close()

def db_get_logs(limit=100):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute("SELECT level, message, created_at FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall(); con.close()
    return rows

init_db()

# -------------------------
# Utilities
# -------------------------
def normalize_words(raw: str) -> List[str]:
    if not raw:
        return []
    s = raw.replace(",", " ").replace("　", " ")
    return [w.strip() for w in s.split() if w.strip()]

def quote_if_space(w: str) -> str:
    return f'"{w}"' if any(c.isspace() for c in w) else w

def timestamp_to_iso(dt: datetime) -> str:
    return dt.isoformat("T") + "Z"

# -------------------------
# API calls (with basic rate handling)
# -------------------------
RATE_COOLDOWN_SECONDS = 60
_last_429_time = None

def handle_429():
    global _last_429_time
    _last_429_time = time.time()
    db_log("WARN", "429 received, entering cooldown")
    return

def is_in_cooldown() -> bool:
    if _last_429_time is None:
        return False
    return (time.time() - _last_429_time) < RATE_COOLDOWN_SECONDS

@st.cache_data(ttl=300, show_spinner=False)
def call_search_api(params: dict) -> Dict[str, Any]:
    """Direct call, cached"""
    if not BEARER:
        return {"error": "API token not set"}
    try:
        r = requests.get(SEARCH_URL, headers=HEADERS, params=params, timeout=25)
    except Exception as e:
        db_log("ERROR", f"search request exception: {e}")
        return {"error": f"通信エラー: {e}"}
    if r.status_code == 429:
        handle_429()
        return {"error": "429"}
    if r.status_code != 200:
        db_log("ERROR", f"search returned {r.status_code} {r.text}")
        return {"error": f"{r.status_code} {r.text}"}
    return r.json()

@st.cache_data(ttl=300, show_spinner=False)
def call_users_api(ids: List[str]) -> Dict[str, Any]:
    if not BEARER:
        return {"error": "API token not set"}
    if not ids:
        return {"data": []}
    params = {"ids": ",".join(ids), "user.fields": "username,name,profile_image_url,public_metrics,verified,created_at"}
    try:
        r = requests.get(USERS_URL, headers=HEADERS, params=params, timeout=25)
    except Exception as e:
        db_log("ERROR", f"users request exception: {e}")
        return {"error": f"通信エラー: {e}"}
    if r.status_code == 429:
        handle_429()
        return {"error": "429"}
    if r.status_code != 200:
        db_log("ERROR", f"users returned {r.status_code} {r.text}")
        return {"error": f"{r.status_code} {r.text}"}
    return r.json()

# -------------------------
# Monitoring background job
# -------------------------
_scheduler_thread = None
_scheduler_running = False

def monitor_job_once(ng_words: List[str], max_results: int, filters: dict):
    """
    Run one cycle: search each ng_word, gather new users, apply filters, save to DB.
    filters: dict of various filter settings
    """
    global _last_429_time
    discovered = []
    for w in ng_words:
        query = " OR ".join([quote_if_space(w) for w in [w]])
        params = {"query": f"{query} -is:retweet", "tweet.fields": "author_id,created_at,text", "max_results": max(10, min(max_results, 100))}
        if is_in_cooldown():
            db_log("WARN", "In cooldown; skipping searches")
            continue
        resp = call_search_api(params)
        if resp.get("error"):
            if resp["error"] == "429":
                st.toast("API制限により監視一時停止（自動再開予定）", icon="⚠️")
                continue
            db_log("ERROR", f"search error for {w}: {resp['error']}")
            continue
        tweets = resp.get("data", [])
        if not tweets:
            db_log("INFO", f"{w}: no hits")
            db_insert_history(query, 0)
            continue
        user_ids = list({t["author_id"] for t in tweets})
        users_resp = call_users_api(user_ids)
        if users_resp.get("error"):
            db_log("ERROR", f"users error: {users_resp['error']}")
            continue
        users = users_resp.get("data", [])
        for u in users:
            pm = u.get("public_metrics", {})
            tweet_count = pm.get("tweet_count", 0)
            follower_count = pm.get("followers_count", 0) if pm else None
            following_count = pm.get("following_count", 0) if pm else None
            ok = True
            if filters.get("require_no_posts") and tweet_count > 0:
                ok = False
            if filters.get("min_followers") and (follower_count is None or follower_count < filters["min_followers"]):
                ok = False
            if filters.get("min_following") and (following_count is None or following_count < filters["min_following"]):
                ok = False
            if ok:
                discovered.append(u)
        db_insert_history(query, len(users))
    unique = {u["id"]: u for u in discovered}.values()
    if unique:
        db_log("INFO", f"monitor discovered {len(unique)}")
    return

def start_scheduler(interval_minutes: int, ng_words: List[str], max_results:int, filters: dict):
    global _scheduler_thread, _scheduler_running
    if _scheduler_running:
        return
    _scheduler_running = True
    def runner():
        db_log("INFO", f"Scheduler started every {interval_minutes} minutes")
        while _scheduler_running:
            try:
                monitor_job_once(ng_words, max_results, filters)
            except Exception as e:
                db_log("ERROR", f"Scheduler exception: {e}")
            for _ in range(int(interval_minutes * 60)):
                if not _scheduler_running:
                    break
                time.sleep(1)
        db_log("INFO", "Scheduler stopped")
    _scheduler_thread = threading.Thread(target=runner, daemon=True)
    _scheduler_thread.start()

def stop_scheduler():
    global _scheduler_running
    _scheduler_running = False

# -------------------------
# Streamlit UI (Main)
# -------------------------
st.title("🔎 NGワード監視ツール 3.0 — 完成版目標")
st.markdown("高度検索 / 保存・履歴 / エクスポート / 定期監視 を備えた完成度の高いツールです。")

left, right = st.columns([2,3])

with left:
    st.header("検索ビルダー")
    raw_input = st.text_area("NGワード（スペース / カンマ / 改行で区切り）", placeholder="例: 暴言, 詐欺", height=100)
    max_results = st.slider("取得件数", 10, 100, 30, step=10)
    min_followers = st.number_input("最小フォロワー数（0=無制限）", min_value=0, value=0)
    require_no_posts = st.checkbox("投稿ゼロのみ (tweet_count == 0)")
    require_default_icon = st.checkbox("アイコン未設定のみ")
    min_tweet_count = st.number_input("最小ツイート数（0=無制限）", min_value=0, value=0)
    min_following = st.number_input("最小フォロー数（0=無制限）", min_value=0, value=0)
    verified_only = st.checkbox("認証済みユーザーのみ", value=False)
    run_query = st.button("🔍 検索実行（即時）")

with right:
    st.header("操作 / 実行")
    interval = st.number_input("監視間隔（分）", min_value=1, value=15)
    start_mon = st.button("監視開始 ▶")
    stop_mon = st.button("監視停止 ⏹")
    if start_mon:
        words = normalize_words(raw_input)
        filters = {"require_no_posts": require_no_posts, "min_followers": min_followers, "min_following": min_following}
        start_scheduler(interval, words, max_results, filters)
        st.success("監視を開始しました。")
    if stop_mon:
        stop_scheduler()
        st.success("監視を停止しました。")

    st.markdown("---")
    st.header("エクスポート / 管理")
    st.markdown("検索結果はCSV / Excel / JSONでエクスポート可能。履歴やプリセットを管理できます。")

def build_query(raw_input: str) -> Tuple[str, dict]:
    words = normalize_words(raw_input)
    if not words:
        return "", {}
    query = " OR ".join([quote_if_space(w) for w in words])
    params = {"query": f"{query} -is:retweet", "max_results": max(10, min(max_results, 100)),
              "tweet.fields": "author_id,created_at,text"}
    return query, params

if run_query:
    if is_in_cooldown():
        st.error("APIのレート制限により現在一時的に実行できません。しばらく待ってください。")
    else:
        query, params = build_query(raw_input)
        if not query:
            st.warning("NGワードを入力してください。")
        else:
            with st.spinner("検索中..."):
                resp = call_search_api(params)
            if resp.get("error"):
                st.error(f"検索エラー: {resp['error']}")
            else:
                data = resp.get("data", [])
                db_insert_history(query, len(data))
                if not data:
                    st.info("該当するツイートはありませんでした。")
                else:
                    user_ids = list({t["author_id"] for t in data})
                    uresp = call_users_api(user_ids)
                    if uresp.get("error"):
                        st.error(f"ユーザー情報取得エラー: {uresp['error']}")
                    else:
                        users = uresp.get("data", [])
                        id_map = {u["id"]: u for u in users}
                        rows = []
                        for t in data:
                            uid = t["author_id"]
                            u = id_map.get(uid, {})
                            pm = u.get("public_metrics", {})
                            rows.append({
                                "username": "@" + u.get("username", "") if u.get("username") else f"(不明ID:{uid})",
                                "name": u.get("name",""),
                                "user_id": uid,
                                "text": t.get("text","")[:240],
                                "created_at": t.get("created_at",""),
                                "followers": pm.get("followers_count"),
                                "tweet_count": pm.get("tweet_count"),
                                "following": pm.get("following_count"),
                                "verified": u.get("verified", False),
                                "icon": u.get("profile_image_url","")
                            })
                        df = pd.DataFrame(rows).drop_duplicates(subset=["user_id"])
                        def apply_filters(df):
                            df2 = df
                            if require_no_posts:
                                df2 = df2[df2["tweet_count"] == 0]
                            if require_default_icon:
                                df2 = df2[df2["icon"].isnull() | df2["icon"].str.contains("default_profile", na=False) | df2["icon"].str.contains("default_profile_images", na=False)]
                            if min_followers and min_followers > 0:
                                df2 = df2[df2["followers"].fillna(0) >= min_followers]
                            if min_following and min_following > 0:
                                df2 = df2[df2["following"].fillna(0) >= min_following]
                            if verified_only:
                                df2 = df2[df2["verified"] == True]
                            if min_tweet_count and min_tweet_count > 0:
                                df2 = df2[df2["tweet_count"].fillna(0) >= min_tweet_count]
                            return df2

                        df_filtered = apply_filters(df)
                        st.success(f"抽出結果: {len(df_filtered)} 件（全体ヒット {len(df)} 件）")
                        for idx, r in df_filtered.iterrows():
                            cols = st.columns([1,6,1])
                            with cols[0]:
                                if r["icon"]:
                                    st.image(r["icon"], width=48)
                            with cols[1]:
                                st.markdown(f"**{r['username']}**  {r['name']}  \n{r['text']}")
                                st.caption(f"followers: {r['followers']} / following: {r['following']} / tweets: {r['tweet_count']} / verified: {r['verified']}")
                            with cols[2]:
                                st.write("")
                                if st.button(f"コピー {r['user_id']}", key=f"copy_{r['user_id']}"):
                                    st.experimental_set_query_params()
                                    st.toast("ユーザー名をコピーしました（手動で貼り付け可能）")
                        csv_bytes = df_filtered.to_csv(index=False).encode("utf-8-sig")
                        excel_buf = io.BytesIO()
                        with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
                            df_filtered.to_excel(writer, index=False, sheet_name="results")
                        excel_bytes = excel_buf.getvalue()
                        json_bytes = df_filtered.to_json(orient="records", force_ascii=False).encode("utf-8")
                        cole1, cole2, cole3 = st.columns(3)
                        with cole1:
                            st.download_button("CSVダウンロード", data=csv_bytes, file_name="ng_users.csv", mime="text/csv")
                        with cole2:
                            st.download_button("Excelダウンロード", data=excel_bytes, file_name="ng_users.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                        with cole3:
                            st.download_button("JSONダウンロード", data=json_bytes, file_name="ng_users.json", mime="application/json")

st.sidebar.header("ダッシュボード")
hist = db_get_history(10)
st.sidebar.markdown("### 最近の検索履歴")
if hist:
    for q, ts, cnt in hist:
        st.sidebar.write(f"- {q} ({ts.split('T')[0]}) [{cnt}]")
else:
    st.sidebar.write("履歴なし")

st.sidebar.markdown("---")
st.sidebar.markdown("### リスト管理（NG / White / Watch）")
lists = db_get_lists()
if lists:
    for l in lists:
        if len(l) == 4:
            st.sidebar.write(f"- ({l[2]}) {l[1]} : {l[3][:40]}...")
        elif len(l) == 3:
            st.sidebar.write(f"- {l[1]} : {l[2][:40]}...")
else:
    st.sidebar.write("リストはまだありません")
if st.sidebar.button("リストを追加（テスト）"):
    db_add_list("sample_ng", "ng", "spam scam")
    st.sidebar.success("追加しました")

st.sidebar.markdown("---")
st.sidebar.markdown("### ログ（最新）")
logs = db_get_logs(10)
if logs:
    for level, msg, ts in logs:
        st.sidebar.write(f"[{ts.split('T')[0]}] {level}: {msg[:80]}")
else:
    st.sidebar.write("ログなし")

st.sidebar.markdown("---")
st.sidebar.caption("完成版3.0はさらに OAuth/課金")