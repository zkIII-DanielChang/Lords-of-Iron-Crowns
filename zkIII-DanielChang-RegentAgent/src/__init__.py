# -*- coding: utf-8 -*-
"""《铁冠诸侯》AI 操控项目（hello-agents 毕业设计）

src.game_bridge  —— Playwright 无头浏览器桥（驱动游戏页面的 window.GameAgent API）
src.game_tools   —— hello_agents 自定义工具（game_state / game_action / game_screenshot）
src.game_agent   —— 智能体装配与战役主循环
src.prompts      —— 系统提示词与观测文本格式化
"""

import sys

# Windows 控制台默认 GBK，hello_agents 框架会打印 ✅ 等 emoji（UnicodeEncodeError）。
# 导入本包时统一把 stdout/stderr 重配为 UTF-8，errors='replace' 兜底。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# DeepSeek DSML 工具调用归一化（deepseek-chat 常以 DSML 文本而非原生 tool_calls 返回）
from .compat import install_dsml_compat as _install_dsml_compat

_install_dsml_compat()

