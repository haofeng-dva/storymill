"""Read-only token usage report for the local state database."""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HERE, "state.db")


def main():
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    uri = f"file:{DB_PATH.replace(os.sep, '/')}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        today_tokens = db.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM llm_calls WHERE created_at LIKE ?",
            (day + "%",),
        ).fetchone()[0]
        month_tokens = db.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM llm_calls WHERE created_at LIKE ?",
            (month + "%",),
        ).fetchone()[0]
        runs = db.execute(
            "SELECT run_id, story_id, COALESCE(SUM(tokens), 0), COUNT(*) "
            "FROM llm_calls WHERE created_at LIKE ? "
            "GROUP BY run_id, story_id ORDER BY run_id",
            (month + "%",),
        ).fetchall()
    print(f"今日 tokens: {today_tokens}")
    print(f"本月 tokens: {month_tokens}")
    print("按 run:")
    for run_id, story_id, tokens, calls in runs:
        print(f"{run_id or '-'}\t{story_id or '-'}\t{tokens}\t{calls} calls")


if __name__ == "__main__":
    main()
