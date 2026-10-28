"""check.py — vibecoding 三闸门自检，一条命令跑完。

用法:
    py -3 tools/check.py

输出三个闸门，每个只有 [OK] / [!!] 两种结果，不需要读懂代码：
  1. 工作区闸门 —— 有没有未提交的半成品、未跟踪文件有没有堆积
  2. 测试闸门   —— 全量 unittest 绿不绿
  3. 提交闸门   —— 最近提交的信息还说不说得清
"""
import os
import re
import subprocess
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNTRACKED_WARN = 40  # 未跟踪文件超过这个数说明 tmp/脚手架又开始堆积


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True,
                          text=True, encoding="utf-8", errors="ignore")


def gate(name, ok, detail):
    print(f"[{'OK' if ok else '!!'}] {name}: {detail}")
    return ok


def main():
    fails = []

    # ---- 闸门 1：工作区 ----
    st = sh("git status --porcelain")
    lines = st.stdout.splitlines()
    dirty = [l for l in lines if not l.startswith("??")]
    untracked = [l for l in lines if l.startswith("??")]
    ok = gate("闸门1 工作区", not dirty,
              f"已跟踪文件未提交差异 {len(dirty)} 个"
              + (f"；未跟踪 {len(untracked)} 个" +
                 (f"（超过 {UNTRACKED_WARN}，该清理了）" if len(untracked) > UNTRACKED_WARN else ""))
              if not dirty else
              "；".join(dirty[:3]))
    if not ok:
        fails.append("工作区不干净")

    # ---- 闸门 2：测试 ----
    r = sh("py -3 -m unittest discover -p \"test_*.py\"")
    m = re.search(r"Ran (\d+) tests", r.stdout + r.stderr)
    n = m.group(1) if m else "?"
    ok = r.returncode == 0
    gate("闸门2 测试", ok,
         f"{n} 项，{'全部通过' if ok else '有失败/错误 —— 不许在这个状态上继续叠加改动'}")
    if not ok:
        fails.append("测试未全绿")

    # ---- 闸门 3：提交 ----
    log = sh("git log --oneline -5").stdout.strip()
    bad = [l for l in log.splitlines() if l and len(l.split(" ", 1)[-1]) < 8]
    ok = not bad
    gate("闸门3 提交", ok,
         "最近 5 条提交信息可读" if ok else f"提交信息过短说不清改动：{bad}")

    print("-" * 40)
    if fails:
        print(f"结果：{len(fails)} 个闸门未过 -> {', '.join(fails)}")
        print("处理顺序：先 git add/commit 或 restore 收拾工作区，再修测试，最后才继续新任务。")
        return 1
    print("结果：三闸门全过，可以继续。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
