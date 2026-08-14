# -*- coding: utf-8 -*-
"""
profiles.py — 模型 + prompt 策略封装（可插拔"包"）

每个模型对应一个 profile，含：
  backend      调用后端（gemini 官方 / ark 火山方舟 / relay GPT中转）
  mode         模型能力类型（thinking 强模型 / reasoning / lite 轻量）
  prompt_pack  写作用的 prompt 策略（minimal 精简 / full 完整约束）
  max_tokens   该模型的输出预算

路由原则（实测结论）：
  thinking 强模型（gemini-3.1-pro）→ minimal 包：约束反而拖累它，用精简 prompt
  轻量/普通模型（flash-lite / 3-flash-preview / deepseek）→ full 包：约束辅助完成
"""

WRITER_PROFILES = {
    # thinking 强模型：精简包（去掉 anchor/forbidden/情感缓冲等过度约束）
    "gemini-3.1-pro-preview": {
        "backend": "gemini",
        "mode": "thinking",
        "prompt_pack": "minimal",
        "max_tokens": 6000,
    },
    "gpt-5.5": {
        "backend": "relay",
        "mode": "thinking",
        "prompt_pack": "minimal",
        "max_tokens": 4000,
    },
    # 轻量模型：完整约束包（约束辅助它写出更好结果）
    "gemini-3.1-flash-lite": {
        "backend": "gemini",
        "mode": "lite",
        "prompt_pack": "full",
        "max_tokens": 4096,
    },
    "gemini-3-flash-preview": {
        "backend": "gemini",
        "mode": "lite",
        "prompt_pack": "full",
        "max_tokens": 4096,
    },
    # 火山方舟（deepseek 系列）
    "deepseek-v4-pro-260425": {
        "backend": "ark",
        "mode": "reasoning",
        "prompt_pack": "full",
        "max_tokens": 4000,
    },
    "deepseek-v4-flash-260425": {
        "backend": "ark",
        "mode": "lite",
        "prompt_pack": "full",
        "max_tokens": 2000,
    },
}

# 检测端（质检/盲评）profile：等 GPT 中转恢复后可接入 gpt-5.5
REVIEWER_PROFILES = {
    "gemini-3.1-pro-preview": {"backend": "gemini", "mode": "thinking"},
    "glm-5-2-260617": {"backend": "ark", "mode": "reasoning"},
    "gpt-5.5": {"backend": "relay", "mode": "thinking"},
    "gpt-4.1": {"backend": "relay", "mode": "thinking"},
}


def get_writer_profile(model):
    """识别 model 名 → 写作 profile。未收录则回退 full 包 + ark 后端。"""
    for key, prof in WRITER_PROFILES.items():
        if model == key or model.startswith(key):
            return prof
    return {"backend": "ark", "mode": "lite", "prompt_pack": "full", "max_tokens": 2000}


def get_reviewer_profile(model):
    """识别 model 名 → 检测 profile。"""
    for key, prof in REVIEWER_PROFILES.items():
        if model == key or model.startswith(key):
            return prof
    return {"backend": "ark", "mode": "reasoning"}
