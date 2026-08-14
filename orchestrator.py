# -*- coding: utf-8 -*-
"""
orchestrator.py — 无人值守调度 + 运维面板（FR-5）

把整条生产线串起来跑一次生产循环，加限流、报告、告警。
由 Windows 计划任务定时触发。

用法:
    py -3 orchestrator.py --cycle    # 跑一次生产循环（雷达→采纳→写作→质检→包装）
    py -3 orchestrator.py --report   # 生成日结报告
    py -3 orchestrator.py --dry-run  # 只看会做什么，不真跑
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from state_store import StateStore, today
import progress

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "orchestrator.json")


def load_config():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_step(name, cmd, dry_run=False, timeout=1800):
    """跑一步，返回 (success, output_tail)。"""
    print(f"  [{name}] {'(dry-run) ' if dry_run else ''}{cmd}", flush=True)
    if dry_run:
        return True, "(dry-run)"
    try:
        r = subprocess.run(cmd, shell=True, cwd=HERE, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0
        tail = (r.stdout or r.stderr or "").strip().splitlines()[-5:]
        return ok, "\n".join(tail)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def check_limits(store, cfg):
    """检查日产能上限 + token 预算，返回 (可以跑, 原因)。"""
    if not cfg["switches"].get("new_short_enabled", True):
        return False, "new_short_enabled switch off"
    m = store.metrics_today()
    if m["stories"] >= cfg["limits"]["max_stories_per_day"]:
        return False, f"daily cap reached ({m['stories']}/{cfg['limits']['max_stories_per_day']})"
    approx = m["tokens"] + cfg["limits"]["approx_tokens_per_story"]
    if approx > cfg["limits"]["daily_token_budget"]:
        return False, f"token budget would exceed ({approx}/{cfg['limits']['daily_token_budget']})"
    return True, ""


def run_cycle(store, cfg, dry_run=False):
    """跑一次生产循环。"""
    print(f"[cycle] {now_iso()}")
    ok, reason = check_limits(store, cfg)
    if not ok:
        print(f"  SKIPPED: {reason}")
        return {"status": "skipped", "reason": reason}

    # 1. 雷达选品
    progress.update("radar", "抓榜 + LLM 推荐")
    ok, tail = run_step("radar", "py -3 radar.py", dry_run)
    if not ok:
        print(f"  radar failed: {tail}")
        return {"status": "radar_failed", "detail": tail}

    # 2. 采纳推荐
    if not dry_run:
        rec = store.get_top_recommendation()
        if not rec:
            print("  no recommendation to adopt, skip produce")
            return {"status": "no_recommendation"}
        rec_id = rec["id"]
    else:
        rec_id = 0
    # 2. 采纳推荐
    progress.update("adopt", f"rec {rec_id}")
    ok, tail = run_step("adopt", f"py -3 direction_manager.py adopt --id {rec_id}", dry_run)
    if not ok:
        print(f"  adopt failed: {tail}")
        return {"status": "adopt_failed", "detail": tail}

    # 3. 写作（质量门槛：每章 GPT 盲评 naturalness <quality_min 带反馈重写；分段写慢，timeout 给足 2 小时）
    story_id = f"story_{today().replace('-', '')}_{rec_id}"
    direction_file = os.path.join(HERE, "directions", f"rec_{rec_id}.txt")
    progress.update("writing", "12 章", story_id)
    quality_min = cfg.get("quality", {}).get("quality_min", 4)
    ok, tail = run_step(
        "write",
        f'py -3 engine.py --direction-file "{direction_file}" --story-id {story_id} --quality-gate --quality-min {quality_min}',
        dry_run,
        timeout=7200,
    )
    if not ok:
        print(f"  write failed: {tail}")
        return {"status": "write_failed", "detail": tail}

    # 4. 质检（exit 2 = 质量不过，拦截不包装）
    progress.update("verifying", story_id)
    ok, tail = run_step("verify", f"py -3 native_check.py --story-id {story_id} --sample 3", dry_run, timeout=3600)
    if not ok:
        print(f"  verify failed (质量不过/审稿失败), 不包装: {tail}")
        return {"status": "verify_failed", "story_id": story_id, "detail": tail}
    verify_ok = ok

    # 5. 包装
    progress.update("packaging", story_id)
    ok2, tail2 = run_step("package", f"py -3 packager.py --story-id {story_id}", dry_run)

    # 6. 记录 metrics
    if not dry_run:
        store.record_story()
        store.add_tokens(cfg["limits"]["approx_tokens_per_story"])

    print(f"  DONE: story_id={story_id}, verify={'ok' if verify_ok else 'failed'}")
    progress.update("done", f"verify={'ok' if verify_ok else 'failed'}", story_id)
    return {"status": "done", "story_id": story_id, "verify": verify_ok}


def daily_report(store):
    """生成日结报告。"""
    m = store.metrics_today()
    cfg = load_config()
    lines = [
        f"# 日结报告 {today()}",
        f"生成时间: {now_iso()}",
        "",
        "## 今日产量",
        f"- 完成故事: {m['stories']} / 上限 {cfg['limits']['max_stories_per_day']}",
        f"- 估算 token: {m['tokens']} / 预算 {cfg['limits']['daily_token_budget']}",
        "",
        "## 待采纳推荐",
    ]
    for r in store.list_new_recommendations():
        lines.append(f"- [{r['id']}] {r['genre']} (conf {r['confidence']}) :: {r['concept'][:50]}...")
    if not store.list_new_recommendations():
        lines.append("- (无)")
    lines += ["", "## 开关状态", f"- new_short: {cfg['switches']['new_short_enabled']}",
              f"- serial: {cfg['switches']['serial_enabled']}",
              f"- auto_replace: {cfg['switches']['auto_replace_enabled']}"]
    out = os.path.join(HERE, "out", "reports", f"daily_{today()}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines))
    print(f"\n[saved] {out}")


def main():
    ap = argparse.ArgumentParser(description="无人值守调度 FR-5")
    ap.add_argument("--cycle", action="store_true", help="跑一次生产循环")
    ap.add_argument("--report", action="store_true", help="生成日结报告")
    ap.add_argument("--dry-run", action="store_true", help="只预览不真跑")
    args = ap.parse_args()

    store = StateStore()
    cfg = load_config()

    if args.report:
        daily_report(store)
    elif args.cycle or args.dry_run:
        result = run_cycle(store, cfg, dry_run=args.dry_run)
        print(f"\n[result] {result.get('status')}")
        if args.dry_run:
            daily_report(store)
    else:
        print("usage: --cycle | --report | --dry-run")

    store.close()


if __name__ == "__main__":
    main()
