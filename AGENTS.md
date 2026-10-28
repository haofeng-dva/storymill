# AGENTS.md — 给 AI 编码代理的强制规则

本仓库由人 + AI 协作开发（vibecoding）。AI 每次会话必须遵守以下规则。
这些规则存在的唯一目的：**每一次 AI 改动之后，人都能用一条命令回到改动之前。**

## 提交纪律

1. 动手改任何文件之前，先运行 `git status`。
   - 已跟踪文件有差异 → 先停下来，向用户报告差异，等用户决定（commit 或 stash），**不要**在脏工作区上叠加改动。
2. 一个任务 = 一个提交。完成且测试通过后立刻提交，不要攒。
3. 提交信息格式：`<type>: <一句话说清改了什么>`
   type 取 `feat / fix / chore / docs / test / refactor`。
4. 禁止创建 `tmp*`、`_backup*`、`xxx_old.py` 之类的手工备份文件/目录 —— 回滚由 git 负责。
5. 禁止把以下内容加入提交：`.env`、`keys.local.json`、`state.db`、`shorts/`、`out/`、`output/`、`logs/`、`_` 开头的脚手架脚本。
6. 只改任务涉及的文件。顺手重构、"顺便优化" 未被要求的代码 = 违规。

## 测试纪律

7. 改完必须运行与改动相关的测试，例如：
   ```
   py -3 -m unittest test_<相关模块>
   ```
   涉及 `engine.py` / `control_*` / `story_bible*` 时，跑全量（约 3.4 秒，不许嫌慢）：
   ```
   py -3 -m unittest discover -p "test_*.py"
   ```
8. 测试红了不许提交修复说明为"完成"。测试和修复放在同一个提交里。
9. 新增回归测试必须与修它的修复同提交（保证 bisect 可判定）。

## 回滚纪律

10. 实验性大改动（多文件重构、换协议、改核心解析）先开分支：
    ```
    git switch -c <experiment-name>
    ```
11. 用户要求撤销时，按场景选择，禁止用手工改回去的方式"撤销"：
    - 未提交：`git restore <file>`（整文件）或 `git stash`（暂存整套）
    - 已提交：`git revert <hash>`
12. 回滚到基线：`git diff v1.0-baseline` 先看差异，再决定 `git restore --source v1.0-baseline <file>`。

## 输出纪律

13. 每个任务结束必须报告：改了哪些文件（含新增/删除）、跑了哪些测试、结果、提交 hash。
14. 改动摘要必须包含 `git diff --stat` 的输出，不许只给自然语言描述。
