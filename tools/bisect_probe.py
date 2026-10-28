"""bisect_probe.py — 给 `git bisect run` 用的探针。

解决一个具体问题：历史提交里不一定存在某个测试文件，直接跑
`git bisect run python -m unittest xxx.py` 会因为"文件不存在"报错，
而这既不是 good 也不是 bad —— 它是**不可判定**。

git 的约定是退出码 125 = skip，本脚本据此把不可判定的提交交回给 bisect。

用法:
    git bisect start
    git bisect bad HEAD
    git bisect good v1.0-baseline
    git bisect run py -3 tools/bisect_probe.py test_engine_json_mode.py

退出码:
    0   通过
    1   失败
    125 不可判定（目标测试文件不存在 / 导入即失败），交由 bisect 跳过
"""
import os
import sys
import unittest

SKIP = 125

# 探针自身不该被 git bisect 检出旧版本时覆盖掉，所以放在 tools/ 下；
# 但如果 checkout 到引入 tools/ 之前的提交，脚本会消失，bisect 同样判为不可判定。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    os.chdir(ROOT)

    if target and not os.path.exists(os.path.join(ROOT, target)):
        print(f"[skip] {target} not present in this commit")
        return SKIP

    if target:
        loader = unittest.TestLoader()
        try:
            suite = loader.discover(ROOT, pattern=target)
        except Exception as exc:  # 导入期就炸的版本不可判定判定它
            print(f"[skip] cannot import {target}: {exc}")
            return SKIP
        if getattr(suite, "countTestCases", lambda: 0)() == 0:
            print(f"[skip] no tests collected from {target}")
            return SKIP
    else:
        suite = loader.discover(ROOT, pattern="test_*.py")

    result = unittest.TextTestRunner(verbosity=0).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
