# -*- coding: utf-8 -*-
"""
progress.py — 中间流程监测

各模块写入 logs/progress.json 记录当前进度，dashboard 读取实时显示。
步骤: radar / adopt / writing / verifying / packaging / done / idle
"""
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRESS_PATH = os.path.join(HERE, "logs", "progress.json")


def update(step, detail="", story_id=""):
    """写进度。step: radar/adopt/writing/verifying/packaging/done/idle"""
    os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    data = {
        "step": step,
        "detail": detail,
        "story_id": story_id,
        "updated_at": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }
    try:
        json.dump(data, open(PROGRESS_PATH, "w", encoding="utf-8"))
    except Exception:
        pass


def read():
    """读当前进度。"""
    if os.path.exists(PROGRESS_PATH):
        try:
            return json.load(open(PROGRESS_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {"step": "idle", "detail": "", "story_id": "", "updated_at": ""}
