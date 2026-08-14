# -*- coding: utf-8 -*-
"""
batch_produce.py — 量产（v1 正式版）

用改进后的引擎并行生产多部不同题材的完整短篇。
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

# 三个差异化题材（避免模板化，覆盖不同赛道）
PRODUCTS = [
    ("US-market domestic suspense: a forensic accountant discovers her husband faked his own death and left her holding the fraud", "v1_suspense"),
    ("Cozy fantasy slice of life: a young woman inherits a magical tea shop and brews potions for quirky mythical customers", "v1_cozy"),
    ("Progression fantasy: a disgraced noble's son exiled to a monster frontier discovers he can absorb the memories and skills of beasts he slays, at the cost of his sanity", "v1_progression"),
]


def main():
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    procs = []
    for direction, story_id in PRODUCTS:
        log = open(os.path.join(HERE, "logs", f"{story_id}.log"), "w", encoding="utf-8")
        p = subprocess.Popen(
            ["py", "-3", "engine.py", "--direction", direction,
             "--chapters", "12", "--words", "700", "--story-id", story_id],
            cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
        )
        procs.append((story_id, p, log))
        print(f"[start] {story_id} (pid {p.pid})", flush=True)

    for story_id, p, log in procs:
        p.wait()
        log.close()
        print(f"[done] {story_id} exit={p.returncode}", flush=True)

    print("\nALL DONE")


if __name__ == "__main__":
    main()
