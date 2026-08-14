# -*- coding: utf-8 -*-
"""
direction_manager.py — 题材方向管理（FR-2）

把雷达推荐转成写作引擎可用的方向文件，串起「选品 → 写作」闭环。

用法:
    py -3 direction_manager.py list              # 列出待采纳的推荐
    py -3 direction_manager.py adopt             # 自动采纳 confidence 最高的一条
    py -3 direction_manager.py adopt --id 3      # 采纳指定推荐
    py -3 direction_manager.py skip --id 3       # 跳过某条
"""
import argparse
import os

from state_store import StateStore

HERE = os.path.dirname(os.path.abspath(__file__))
DIR_DIR = os.path.join(HERE, "directions")


def build_direction(rec):
    """把推荐转成方向文本（genre + concept，供写作引擎读取并自动检测题材）。"""
    return f"{rec['genre']}: {rec['concept']}"


def adopt(store, rec_id=None):
    """采纳推荐：生成方向文件 + 标记 adopted。返回 (path, direction_text)。"""
    rec = store.get_recommendation(rec_id) if rec_id else store.get_top_recommendation()
    if not rec:
        print("no recommendation to adopt (run radar.py first)")
        return None
    direction_text = build_direction(rec)
    os.makedirs(DIR_DIR, exist_ok=True)
    path = os.path.join(DIR_DIR, f"rec_{rec['id']}.txt")
    open(path, "w", encoding="utf-8").write(direction_text)
    store.mark_adopted(rec["id"])
    print(f"[adopted] rec {rec['id']}: {rec['genre']} (confidence {rec['confidence']})")
    print(f"    direction -> {path}")
    print(f"    text: {direction_text[:120]}...")
    return path, direction_text


def main():
    ap = argparse.ArgumentParser(description="题材方向管理 FR-2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出待采纳的推荐")
    p_adopt = sub.add_parser("adopt", help="采纳推荐（默认 confidence 最高）")
    p_adopt.add_argument("--id", type=int, help="指定推荐 id")
    p_skip = sub.add_parser("skip", help="跳过某条推荐")
    p_skip.add_argument("--id", type=int, required=True)
    args = ap.parse_args()

    store = StateStore()
    if args.cmd == "list":
        recs = store.list_new_recommendations()
        if not recs:
            print("no pending recommendations (run radar.py first)")
        for r in recs:
            print(f"  [{r['id']}] {r['genre']} (conf {r['confidence']}) :: {r['concept'][:70]}...")
    elif args.cmd == "adopt":
        adopt(store, args.id)
    elif args.cmd == "skip":
        store.mark_skipped(args.id)
        print(f"[skipped] rec {args.id}")
    store.close()


if __name__ == "__main__":
    main()
