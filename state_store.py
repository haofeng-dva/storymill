# -*- coding: utf-8 -*-
"""
state_store.py — SQLite 状态库

生产线的编排状态唯一事实来源：trends（热点归档）、recommendations（题材推荐）、
stories（故事）、runs（运行记录）、metrics（日预算）。
选品层只用到 trends + recommendations；stories/runs/metrics 留给后续模块。

用法:
    from state_store import StateStore
    store = StateStore("state.db")
    store.archive_trends(day, source, entries)
    store.add_recommendation(...)
"""
import sqlite3
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "state.db")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class StateStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or DEFAULT_DB
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trends (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day TEXT NOT NULL,
          source TEXT NOT NULL,
          title TEXT NOT NULL,
          category TEXT,
          tags TEXT,
          rank INTEGER,
          validated TEXT DEFAULT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trends_day ON trends(day);
        CREATE INDEX IF NOT EXISTS idx_trends_title ON trends(title);

        CREATE TABLE IF NOT EXISTS recommendations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scan_file TEXT,
          genre TEXT,
          concept TEXT NOT NULL,
          confidence REAL,
          benchmark TEXT,
          status TEXT NOT NULL DEFAULT 'new',
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendations(status, created_at);

        CREATE TABLE IF NOT EXISTS stories (
          id TEXT PRIMARY KEY,
          title TEXT,
          direction TEXT,
          source TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          chapters INTEGER,
          words INTEGER,
          verdict TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          story_id TEXT,
          stage TEXT,
          model TEXT,
          status TEXT,
          tokens INTEGER,
          elapsed_s INTEGER,
          error TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS metrics (
          day TEXT PRIMARY KEY,
          stories INTEGER DEFAULT 0,
          tokens INTEGER DEFAULT 0,
          cost_yuan REAL DEFAULT 0
        );
        """)
        self.db.commit()

    # ---- trends（热点归档）----

    def archive_trends(self, source, entries, day=None):
        """按日归档榜单。entries: [{title, category, tags, rank}]"""
        day = day or today()
        ts = now_iso()
        n = 0
        for i, e in enumerate(entries):
            self.db.execute(
                "INSERT INTO trends (day, source, title, category, tags, rank, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (day, source, e.get("title", ""), e.get("category", ""),
                 json.dumps(e.get("tags", []), ensure_ascii=False), e.get("rank", i + 1), ts),
            )
            n += 1
        self.db.commit()
        return n

    def trend_history(self, title, days=30):
        """查某热点的历史（趋势回溯用）。"""
        cur = self.db.execute(
            "SELECT day, rank, source FROM trends WHERE title=? ORDER BY day DESC LIMIT ?",
            (title, days),
        )
        return [dict(r) for r in cur.fetchall()]

    def category_distribution(self, day=None):
        """统计某日榜单的题材分布（硬数字）。"""
        day = day or today()
        cur = self.db.execute(
            "SELECT category, COUNT(*) c FROM trends WHERE day=? AND category IS NOT NULL AND category!='' GROUP BY category ORDER BY c DESC",
            (day,),
        )
        return [(r["category"], r["c"]) for r in cur.fetchall()]

    def set_validated(self, title, validated):
        """回填题材验证结果（ok/fail），用于周级回灌。"""
        self.db.execute("UPDATE trends SET validated=? WHERE title=?", (validated, title))
        self.db.commit()

    def validated_failures(self):
        """查已验证失败的题材标题，回灌给 LLM 推荐降权。"""
        cur = self.db.execute(
            "SELECT DISTINCT title, category FROM trends WHERE validated='fail'"
        )
        return [(r["title"], r["category"]) for r in cur.fetchall()]

    # ---- recommendations（题材推荐）----

    def add_recommendation(self, rec):
        """推荐入库。rec: {genre, concept, confidence, benchmark, scan_file}"""
        self.db.execute(
            "INSERT INTO recommendations (scan_file, genre, concept, confidence, benchmark, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'new', ?)",
            (rec.get("scan_file", ""), rec.get("genre", ""), rec.get("concept", ""),
             rec.get("confidence"), json.dumps(rec.get("benchmarkTitles", []), ensure_ascii=False), now_iso()),
        )
        self.db.commit()

    def recent_recommendations(self, hours=24):
        """查最近 N 小时的推荐（去重用）。"""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self.db.execute(
            "SELECT genre, concept FROM recommendations WHERE created_at >= ?",
            (cutoff_s,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_recommendation(self, rec_id):
        """查单条推荐（含全部字段）。"""
        cur = self.db.execute(
            "SELECT * FROM recommendations WHERE id=?", (rec_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None

    def get_top_recommendation(self):
        """查待采纳（status='new'）里 confidence 最高的一条。"""
        cur = self.db.execute(
            "SELECT * FROM recommendations WHERE status='new' ORDER BY confidence DESC LIMIT 1"
        )
        r = cur.fetchone()
        return dict(r) if r else None

    def list_new_recommendations(self):
        """列出所有待采纳的推荐（status='new'）。"""
        cur = self.db.execute(
            "SELECT id, genre, concept, confidence FROM recommendations WHERE status='new' ORDER BY confidence DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def mark_adopted(self, rec_id):
        """标记推荐已采纳。"""
        self.db.execute(
            "UPDATE recommendations SET status='adopted' WHERE id=?", (rec_id,),
        )
        self.db.commit()

    def mark_skipped(self, rec_id):
        """标记推荐已跳过。"""
        self.db.execute(
            "UPDATE recommendations SET status='skipped' WHERE id=?", (rec_id,),
        )
        self.db.commit()

    # ---- metrics（日产能/预算计量）----

    def record_story(self, day=None):
        """今天完成故事数 +1。"""
        day = day or today()
        self.db.execute(
            "INSERT INTO metrics (day, stories) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET stories = stories + 1",
            (day,),
        )
        self.db.commit()

    def add_tokens(self, n, day=None):
        """今天 token 数 +n。"""
        day = day or today()
        self.db.execute(
            "INSERT INTO metrics (day, tokens) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET tokens = tokens + ?",
            (day, n, n),
        )
        self.db.commit()

    def metrics_today(self):
        """查今天的 metrics（stories/tokens/cost）。"""
        cur = self.db.execute("SELECT * FROM metrics WHERE day=?", (today(),))
        r = cur.fetchone()
        return dict(r) if r else {"day": today(), "stories": 0, "tokens": 0, "cost_yuan": 0}

    def close(self):
        self.db.close()


if __name__ == "__main__":
    # 自测：建表 + 写入 + 查询
    store = StateStore()
    print("tables ok:", sorted([r["name"] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]))
    n = store.archive_trends("test_source", [
        {"title": "Mother of Learning", "category": "timeloop", "tags": ["fantasy"]},
        {"title": "The Perfect Run", "category": "timeloop", "tags": ["scifi"]},
    ])
    print("archived:", n, "trends")
    print("distribution:", store.category_distribution())
    store.add_recommendation({"genre": "litrpg", "concept": "test concept", "confidence": 0.9})
    print("recent recs:", store.recent_recommendations(24))
    store.close()
    print("OK")
