# 规范 · Git 提交与回归定位

- 建立日期：2026-10-28
- 基线标签：`v1.0-baseline` → `f76fe9b`（375 项测试通过，约 3.4s，存在既有 `ResourceWarning`）
- 适用范围：Storymill 全部后续改动

> 这份规范的唯一目的：**当下次出现"以前是好的，现在坏了"时，能在 10 分钟内定位到具体提交，而不是靠记忆和肉眼 diff。**

---

## 事实前提（决定了规范的形态）

| 事实 | 含义 |
|---|---|
| 仓库当前仅 6 次提交，`engine.py` 只有 5 次提交历史 | **对已有历史做 bisect 基本没有价值**，只能定位到某个塞了上千行的提交 |
| `57a2e00`、`7fa1e2c` 两次提交里**零个测试文件** | 用任何现有测试做探针，这两次提交都是"不可判定" |
| `test_engine_json_mode.py` 直到 `e2e4f46` 才出现 | 拿它探早期的提交会得到"文件不存在"，那既不是 good 也不是 bad |
| 早期提交依赖的 `config.json` 键是后来才加的 | 回退到旧提交可能直接 `KeyError`，同样属于不可判定 |

结论：**bisect 的价值 100% 在未来，不在过去。** 建立基线 tag 是为了给未来的"坏"找一个可信的"好"。

---

## 三条规则

### 规则 1 · 每个提交只做一件可说明的事
一个提交对应一句能写进 commit message 的话。禁止把"并行调度 + 字数联动 + 分段大纲"塞进同一个提交。

拆分方式：
```bash
git add -p          # 逐块挑选，一次只提交一个逻辑改动
```

### 规则 2 · 新增回归测试与修复放在同一个提交
不要出现"修了 bug 的提交"和"补了测试的提交"分离的情况 —— 否则 bisect 走到修复提交时，测试还不存在，那一格是空的。

例外：测试本身很大、需要独立评审时，测试提交必须紧邻修复提交，且 commit message 注明 `fixes: <修复提交 hash>`。

### 规则 3 · 提交前跑一遍相关测试；跑不了的版本要能被"跳过"
不需要每个中间提交都全绿（bisect 本来就能穿过红色提交）。需要的是**可判定**：
- 能跑、有明确结论 → 0 / 1
- 跑不起来（文件不存在 / 导入失败 / 配置缺键）→ **125（skip）**

`git bisect run` 用退出码 125 表示跳过，不接受 `--skip` 参数。

---

## 操作手册

### 打基线（已验证过的版本才配打 tag）
```bash
py -3 -m unittest discover -p "test_*.py"     # 必须先确认全绿
git tag -a v1.1-baseline -m "375 tests green, <日期>, <一句话说明本版状态>"
git push origin v1.1-baseline                 # 有远端时同步，否则 tag 只在本机
```

### 定位回归
```bash
git bisect start
git bisect bad HEAD
git bisect good v1.0-baseline
git bisect run py -3 tools/bisect_probe.py test_engine_json_mode.py
```

`tools/bisect_probe.py` 负责把"测试文件不存在 / 导入期崩溃 / 收不到用例"这三种情况统一转成 125，交回给 bisect 跳过 —— 这正是直接跑 `unittest` 会踩的坑。

### 只有一个提交可疑时（比 bisect 更快）
```bash
git log -p -S "word_min" -- engine.py      # 谁引入/删掉了这个符号
git log -p --follow -- engine.py | head    # 逐提交看 diff
git show 4806117 --stat                    # 单次提交改了什么
```
当前仓库只有 5 次提交，用 `-S`（pickaxe）比 bisect 直接得多。

---

## 基线声明的边界

`v1.0-baseline` 断言的是：
- ✅ `f76fe9b` 这个提交，在本机环境跑 `unittest discover` 是绿的（375 项）
- ✅ 该提交的**已跟踪文件**工作树无差异

它**不**断言：
- ❌ 更早的提交同样可测（事实上不是）
- ❌ 未跟踪的 23 个 `_` 脚手架脚本、59 个运行时目录属于基线范围
- ❌ 端到端产出质量合格（那是 `README` 里"稿件不等于可发布"的另一件事）
