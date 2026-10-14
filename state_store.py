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
import threading
from datetime import datetime, timezone

from chapter_state import ACTIVE_STATUSES, FINISHED_STATUSES

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "state.db")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class StateStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or DEFAULT_DB
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
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
          run_id TEXT,
          story_id TEXT,
          stage TEXT,
          model TEXT,
          status TEXT,
          tokens INTEGER,
          elapsed_s INTEGER,
          error TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chapter_tasks (
          run_id TEXT NOT NULL,
          story_id TEXT NOT NULL,
          chapter_no INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempt INTEGER NOT NULL DEFAULT 0,
          quality REAL,
          words INTEGER,
          elapsed_s REAL,
          output_path TEXT,
          error TEXT,
          failure_type TEXT,
          warning TEXT,
          started_at TEXT,
          finished_at TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (run_id, chapter_no)
        );
        CREATE INDEX IF NOT EXISTS idx_chapter_tasks_story ON chapter_tasks(story_id, chapter_no);
        CREATE INDEX IF NOT EXISTS idx_chapter_tasks_status ON chapter_tasks(status, updated_at);

        CREATE TABLE IF NOT EXISTS llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT,
          story_id TEXT,
          chapter_no INTEGER,
          role TEXT NOT NULL,
          model TEXT,
          backend TEXT,
          status TEXT NOT NULL,
          elapsed_s REAL,
          tokens INTEGER,
          error TEXT,
          attempt INTEGER,
          purpose TEXT,
          retry_index INTEGER,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id, created_at);

        CREATE TABLE IF NOT EXISTS metrics (
          day TEXT PRIMARY KEY,
          stories INTEGER DEFAULT 0,
          tokens INTEGER DEFAULT 0,
          cost_yuan REAL DEFAULT 0
        );
        """)
        self.db.commit()
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(runs)").fetchall()}
        if "run_id" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN run_id TEXT")
            self.db.commit()
        task_columns = {r[1] for r in self.db.execute("PRAGMA table_info(chapter_tasks)").fetchall()}
        if "warning" not in task_columns:
            self.db.execute("ALTER TABLE chapter_tasks ADD COLUMN warning TEXT")
            self.db.commit()
        if "failure_type" not in task_columns:
            self.db.execute("ALTER TABLE chapter_tasks ADD COLUMN failure_type TEXT")
            self.db.commit()
        llm_columns = {r[1] for r in self.db.execute("PRAGMA table_info(llm_calls)").fetchall()}
        for column in ("attempt", "purpose", "retry_index"):
            if column not in llm_columns:
                self.db.execute(f"ALTER TABLE llm_calls ADD COLUMN {column} "
                                + ("INTEGER" if column != "purpose" else "TEXT"))
        self.db.commit()

    # ---- chapter task state ----

    def upsert_chapter_task(self, run_id, story_id, chapter_no, status="pending",
                            attempt=None, quality=None, words=None, elapsed_s=None,
                            output_path=None, error=None, warning=None, failure_type=None):
        """Persist one chapter's state; safe for engine worker threads."""
        now = now_iso()
        with self._lock:
            row = self.db.execute(
                "SELECT attempt FROM chapter_tasks WHERE run_id=? AND chapter_no=?",
                (run_id, chapter_no),
            ).fetchone()
            if attempt is None:
                attempt = int(row[0]) if row else 0
            # running/reviewing are retained for databases created by older builds.
            started = now if status in ACTIVE_STATUSES or status in ("running", "reviewing") else None
            finished = now if status in FINISHED_STATUSES else None
            self.db.execute(
                "INSERT INTO chapter_tasks "
                "(run_id, story_id, chapter_no, status, attempt, quality, words, elapsed_s, output_path, error, failure_type, warning, started_at, finished_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, chapter_no) DO UPDATE SET "
                "story_id=excluded.story_id, status=excluded.status, attempt=excluded.attempt, "
                "quality=COALESCE(excluded.quality, chapter_tasks.quality), "
                "words=COALESCE(excluded.words, chapter_tasks.words), "
                "elapsed_s=COALESCE(excluded.elapsed_s, chapter_tasks.elapsed_s), "
                "output_path=COALESCE(excluded.output_path, chapter_tasks.output_path), "
                # A completed chapter must not inherit a transient failure from
                # an earlier retry. Its terminal record is the current truth.
                "error=CASE WHEN excluded.status IN ('completed', 'writing') THEN excluded.error ELSE COALESCE(excluded.error, chapter_tasks.error) END, "
                "failure_type=CASE WHEN excluded.status='completed' THEN NULL ELSE COALESCE(excluded.failure_type, chapter_tasks.failure_type) END, "
                "warning=CASE WHEN excluded.status='completed' THEN excluded.warning ELSE COALESCE(excluded.warning, chapter_tasks.warning) END, "
                "started_at=COALESCE(excluded.started_at, chapter_tasks.started_at), "
                "finished_at=COALESCE(excluded.finished_at, chapter_tasks.finished_at), updated_at=excluded.updated_at",
                (run_id, story_id, chapter_no, status, attempt, quality, words, elapsed_s,
                 output_path, error, failure_type, warning, started, finished, now),
            )
            self.db.commit()

    def chapter_tasks(self, run_id):
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM chapter_tasks WHERE run_id=? ORDER BY chapter_no", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def record_llm_call(self, run_id, story_id, role, model, backend, status,
                        elapsed_s=None, tokens=None, error=None, chapter_no=None,
                        attempt=None, purpose=None, retry_index=None):
        with self._lock:
            self.db.execute(
                "INSERT INTO llm_calls "
                "(run_id, story_id, chapter_no, role, model, backend, status, elapsed_s, tokens, error, attempt, purpose, retry_index, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, story_id, chapter_no, role, model, backend, status, elapsed_s,
                 tokens, error, attempt, purpose, retry_index, now_iso()),
            )
            self.db.commit()

    def latest_completed_chapters(self, story_id):
        """Return completed chapter numbers for resumable story output."""
        with self._lock:
            rows = self.db.execute(
                "SELECT chapter_no, output_path FROM chapter_tasks "
                "WHERE story_id=? AND status='completed' ORDER BY chapter_no", (story_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def start_run(self, run_id, story_id, stage="writing"):
        with self._lock:
            existing = self.db.execute(
                "SELECT status FROM runs WHERE run_id=? AND story_id=? AND stage=? ORDER BY id DESC LIMIT 1",
                (run_id, story_id, stage),
            ).fetchone()
            if existing and existing[0] == "running":
                return False
            self.db.execute(
                "INSERT INTO runs (run_id, story_id, stage, status, created_at) VALUES (?, ?, ?, 'running', ?)",
                (run_id, story_id, stage, now_iso()),
            )
            self.db.commit()
            return True

    def run_status(self, run_id, story_id, stage="writing"):
        with self._lock:
            row = self.db.execute(
                "SELECT status FROM runs WHERE run_id=? AND story_id=? AND stage=? ORDER BY id DESC LIMIT 1",
                (run_id, story_id, stage),
            ).fetchone()
            return row[0] if row else None

    def finish_run(self, run_id, story_id, stage="writing", status="completed", elapsed_s=None, error=None):
        with self._lock:
            self.db.execute(
                "UPDATE runs SET status=?, elapsed_s=?, error=? "
                "WHERE run_id=? AND story_id=? AND stage=? AND status='running'",
                (status, elapsed_s, error, run_id, story_id, stage),
            )
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

    def latest_recommendation_id(self):
        """返回当前最大的推荐 ID，用作一次雷达运行的入库边界。"""
        row = self.db.execute("SELECT COALESCE(MAX(id), 0) AS id FROM recommendations").fetchone()
        return int(row["id"])

    def get_top_recommendation_after(self, rec_id):
        """只从指定 ID 之后的新推荐中取置信度最高的一条。"""
        cur = self.db.execute(
            "SELECT * FROM recommendations WHERE status='new' AND id > ? "
            "ORDER BY confidence DESC, id ASC LIMIT 1",
            (rec_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

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
