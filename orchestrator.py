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
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from state_store import StateStore, today
import progress
from writing_status import is_deliverable, load_status

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "orchestrator.json")


def load_config():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def python_command(*args):
    """Build a child-process command with the interpreter running this app."""
    return [sys.executable, *(str(arg) for arg in args)]


def run_step(name, cmd, dry_run=False, timeout=1800):
    """跑一步，返回 (success, output_tail)。"""
    cmd = [str(part) for part in cmd]
    display_cmd = subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)
    print(f"  [{name}] {'(dry-run) ' if dry_run else ''}{display_cmd}", flush=True)
    if dry_run:
        return True, "(dry-run)"
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout)
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
    budget = int(cfg.get("limits", {}).get("daily_token_budget", 2_000_000))
    used = int(m.get("tokens", 0) or 0)
    if used >= budget:
        return False, f"今日 token 预算已用完（已用 {used} / 上限 {budget}）"
    return True, ""


def radar_command(cfg):
    rcfg = cfg.get("radar", {})
    sources = rcfg.get("sources", "inkitt_browser,wattpad,amazon")
    limit = max(1, int(rcfg.get("list", 20)))
    recommend_count = max(1, min(20, int(rcfg.get("recommend_count", 4))))
    portfolio = rcfg.get("portfolio", "weighted")
    if portfolio not in ("weighted", "natural"):
        portfolio = "weighted"
    command = python_command(
        "radar.py", "--sources", sources, "--list", limit,
        "--recommend-count", recommend_count, "--portfolio", portfolio,
    )
    if rcfg.get("no_chapters", True):
        command.append("--no-chapters")
    return command


def final_editor_run_id():
    return "final_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def final_editor_command(story_id, review_out, run_id, final_cfg=None, mode="auto", artifact_dir=None):
    final_cfg = final_cfg or {}
    roles = {
        "dossier": final_cfg.get("dossier_role", "final_editor"),
        "plan": final_cfg.get("plan_role", "final_editor"),
        "editor": final_cfg.get("editor_role", "final_editor"),
        "review": final_cfg.get("review_role", "reviewer"),
    }
    remediation_rounds = max(0, min(6, int(final_cfg.get("unresolved_remediation_rounds", 2))))
    command = python_command(
        "final_editor.py", "--story-id", story_id, "--run-id", run_id,
    )
    if mode == "auto":
        command.append("--auto")
    command.extend([
        "--mode", mode,
        "--review-report", review_out or "",
        "--dossier-role", roles["dossier"],
        "--plan-role", roles["plan"],
        "--editor-role", roles["editor"],
        "--review-role", roles["review"],
        "--unresolved-remediation-rounds", str(remediation_rounds),
    ])
    if artifact_dir:
        command.extend(["--out", artifact_dir])
    return command


def run_recommend(cfg, dry_run=False):
    """只跑抓取、理解和推荐，不采纳、不写文章。"""
    progress.update("radar", "多平台抓取 + LLM 推荐")
    ok, tail = run_step("radar", radar_command(cfg), dry_run)
    if not ok:
        print(f"  radar failed: {tail}")
        return {"status": "radar_failed", "detail": tail}
    print(f"  RECOMMENDATIONS READY\n{tail}")
    return {"status": "recommendations_ready", "detail": tail}


def run_cycle(store, cfg, dry_run=False):
    """跑一次生产循环。"""
    print(f"[cycle] {now_iso()}")
    verify_sample = int(cfg.get("quality", {}).get("verify_sample", 0) or 0)
    if verify_sample > 0:
        return {
            "status": "config_error",
            "detail": "quality.verify_sample cannot be used with the required full-book quality gate",
        }
    ok, reason = check_limits(store, cfg)
    if not ok:
        print(f"  SKIPPED: {reason}")
        return {"status": "skipped", "reason": reason}

    # 1. 雷达选品。记录边界，确保随后只采纳本轮新生成的推荐。
    recommendation_boundary = store.latest_recommendation_id()
    progress.update("radar", "抓榜 + LLM 推荐")
    ok, tail = run_step("radar", radar_command(cfg), dry_run)
    if not ok:
        print(f"  radar failed: {tail}")
        return {"status": "radar_failed", "detail": tail}

    # 2. 采纳推荐
    if not dry_run:
        rec = store.get_top_recommendation_after(recommendation_boundary)
        if not rec:
            print("  no recommendation to adopt, skip produce")
            return {"status": "no_recommendation"}
        rec_id = rec["id"]
    else:
        rec_id = 0
    # 2. 采纳推荐
    progress.update("adopt", f"rec {rec_id}")
    ok, tail = run_step(
        "adopt", python_command("direction_manager.py", "adopt", "--id", rec_id), dry_run,
    )
    if not ok:
        print(f"  adopt failed: {tail}")
        return {"status": "adopt_failed", "detail": tail}

    # 3. Write serially. Controlled-generation remains an explicit experiment
    # until its contract compiler has passed a separate reliability evaluation.
    story_id = f"story_{today().replace('-', '')}_{rec_id}"
    direction_file = os.path.join(HERE, "directions", f"rec_{rec_id}.txt")
    progress.update("writing", "串行写作", story_id)
    quality_min = cfg.get("quality", {}).get("quality_min", 4)
    ok, tail = run_step(
        "write",
        python_command(
            "engine.py", "--direction-file", direction_file, "--story-id", story_id,
            "--quality-gate", "--quality-min", quality_min,
        ),
        dry_run,
        timeout=7200,
    )
    if not ok:
        print(f"  write failed: {tail}")
        return {"status": "write_failed", "detail": tail}

    # A writer process can finish its Python work while leaving a partial run.
    # The persisted status is the gate for every downstream stage.
    status_path = os.path.join(HERE, "shorts", story_id, "writing_status.json")
    if not dry_run:
        try:
            writing_status = load_status(os.path.dirname(status_path))
        except (OSError, ValueError) as exc:
            print(f"  write status missing/invalid: {exc}")
            return {"status": "write_incomplete", "story_id": story_id, "detail": str(exc)}
        if not is_deliverable(writing_status):
            print(f"  write incomplete: {writing_status}")
            return {"status": "write_incomplete", "story_id": story_id, "detail": writing_status}

    # 4. 质检（默认全书；仅在配置显式设置 verify_sample 时抽样）
    progress.update("verifying", story_id)
    review_out = os.path.join("out", story_id, "native_check_result.json")
    ok, tail = run_step(
        "verify",
        python_command(
            "native_check.py", "--story-id", story_id, "--require-quality-gate",
            "--fail-on-editorial-findings", "--out", review_out,
        ),
        dry_run,
        timeout=3600,
    )
    if not ok:
        print(f"  verify failed (质量不过/审稿失败), 不包装: {tail}")
        cards_out = os.path.join("out", story_id, "repair_cards.json")
        cards_ok, cards_tail = run_step(
            "repair-cards",
            python_command(
                "repair_pipeline.py", "--story-id", story_id, "--report", review_out,
                "--out", cards_out,
            ),
            dry_run,
            timeout=900,
        )
        if not cfg.get("switches", {}).get("auto_replace_enabled", False):
            return {
                "status": "verify_failed", "story_id": story_id, "detail": tail,
                "repair_cards": cards_out if cards_ok else None,
                "repair_cards_error": None if cards_ok else cards_tail,
            }
        if not cards_ok:
            return {
                "status": "repair_cards_failed", "story_id": story_id, "detail": cards_tail,
                "repair_cards": cards_out,
            }
        progress.update("repairing", story_id)
        drafts_ok, drafts_tail = run_step(
            "repair-plan-draft",
            python_command(
                "repair_pipeline.py", "--story-id", story_id, "--report", review_out,
                "--out", cards_out, "--resume", "--plan", "--draft",
            ),
            dry_run,
            timeout=3600,
        )
        if not drafts_ok:
            return {"status": "repair_draft_failed", "story_id": story_id, "detail": drafts_tail,
                    "repair_cards": cards_out}
        apply_out = os.path.join("out", story_id, "repair_apply.json")
        applied_ok, applied_tail = run_step(
            "repair-apply",
            python_command(
                "apply_repair_drafts.py", "--story-id", story_id, "--cards", cards_out,
                "--out", apply_out,
            ),
            dry_run,
            timeout=900,
        )
        if not applied_ok:
            return {"status": "repair_apply_failed", "story_id": story_id, "detail": applied_tail,
                    "repair_cards": cards_out, "repair_apply": apply_out}
        # Packager consumes the canonical review path, so the successful full
        # recheck must replace the failed pre-repair review at that same path.
        recheck_out = review_out
        recheck_ok, recheck_tail = run_step(
            "repair-verify",
            python_command(
                "native_check.py", "--story-id", story_id, "--require-quality-gate",
                "--fail-on-editorial-findings", "--out", recheck_out,
            ),
            dry_run,
            timeout=3600,
        )
        if not recheck_ok:
            return {"status": "repair_recheck_failed", "story_id": story_id, "detail": recheck_tail,
                    "repair_cards": cards_out, "repair_apply": apply_out, "repair_review": recheck_out}
        review_out = recheck_out
        verify_ok = True
    else:
        verify_ok = ok

    # The native check above is the only open-ended review pass.  The final
    # editor receives its anchored findings once, then rechecks only the
    # planned repairs it made; it must never create a new full-book loop.
    package_story_id = story_id
    final_cfg = cfg.get("final_editor", {})
    if final_cfg.get("enabled", False):
        editor_timeout = int(final_cfg.get("timeout_seconds", 7200))
        editor_run_id = final_editor_run_id()
        editor_out = os.path.join("out", story_id, "final_editor", editor_run_id)
        progress.update("final-editing", "anchored repair and independent recheck", story_id)
        detect_ok, detect_tail = run_step(
            "ai-detect",
            final_editor_command(story_id, review_out, editor_run_id, final_cfg, mode="detect", artifact_dir=editor_out),
            dry_run,
            timeout=max(1, editor_timeout),
        )
        if not detect_ok:
            return {"status": "ai_detect_failed", "story_id": story_id, "detail": detect_tail,
                    "final_editor_artifacts": editor_out}
        repair_ok, repair_tail = run_step(
            "ai-repair",
            final_editor_command(story_id, "", editor_run_id, final_cfg, mode="repair", artifact_dir=editor_out),
            dry_run,
            timeout=max(1, editor_timeout),
        )
        if not repair_ok:
            return {"status": "ai_repair_failed", "story_id": story_id, "detail": repair_tail,
                    "final_editor_artifacts": editor_out}
        package_story_id = f"{story_id}_final_edit_{editor_run_id}"

    # 5. 包装
    progress.update("packaging", package_story_id)
    ok2, tail2 = run_step(
        "package",
        python_command("packager.py", "--story-id", package_story_id, "--require-quality-gate"),
        dry_run,
    )
    if not dry_run:
        store.record_story()
    if not ok2:
        print(f"  package failed: {tail2}")
        return {"status": "package_failed", "story_id": package_story_id, "detail": tail2}

    print(f"  DONE: story_id={package_story_id}, verify={'ok' if verify_ok else 'failed'}")
    progress.update("done", f"verify={'ok' if verify_ok else 'failed'}", package_story_id)
    result = {"status": "done", "story_id": package_story_id, "verify": verify_ok}
    if package_story_id != story_id:
        result["source_story_id"] = story_id
    return result


def daily_report(store, save=True):
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
               f"- auto_replace: {cfg['switches']['auto_replace_enabled']}",
               f"- final_editor: {cfg.get('final_editor', {}).get('enabled', False)}"]
    print("\n".join(lines))
    if save:
        out = os.path.join(HERE, "out", "reports", f"daily_{today()}.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w", encoding="utf-8").write("\n".join(lines))
        print(f"\n[saved] {out}")


def main():
    ap = argparse.ArgumentParser(description="无人值守调度 FR-5")
    ap.add_argument("--cycle", action="store_true", help="跑一次生产循环")
    ap.add_argument("--recommend", action="store_true", help="只抓榜并生成推荐，不写文章")
    ap.add_argument("--report", action="store_true", help="生成日结报告")
    ap.add_argument("--dry-run", action="store_true", help="只预览不真跑")
    args = ap.parse_args()

    store = StateStore()
    cfg = load_config()

    if args.report:
        daily_report(store)
    elif args.recommend:
        result = run_recommend(cfg, dry_run=args.dry_run)
        print(f"\n[result] {result.get('status')}")
    elif args.cycle or args.dry_run:
        result = run_cycle(store, cfg, dry_run=args.dry_run)
        print(f"\n[result] {result.get('status')}")
        if args.dry_run:
            daily_report(store, save=False)
    else:
        print("usage: --cycle | --report | --dry-run")

    store.close()


if __name__ == "__main__":
    main()
