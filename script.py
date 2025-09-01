# app.py
"""
NGワード抽出ツール 3.0 (Streamlit)
- 高度検索ビルダー、フィルタ、エクスポート、定期監視、履歴、ダッシュボード
- Save as app.py. Requires .env with API1=<X/Twitter Bearer Token>
"""
import os
import time
import json
import csv
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
BEARER = os.getenv("TNSS_BEARER_TOKEN")  # required

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
st.markdown("")