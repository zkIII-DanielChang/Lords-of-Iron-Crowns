# -*- coding: utf-8 -*-
"""DeepSeek DSML 工具调用兼容层（运行时适配，不修改框架包）。

现象：deepseek-chat 在工具调用场景下，经常把工具调用以 DSML 文本写在
message.content 里（如 <｜DSML｜ invoke name="game_state">…</invoke>），
而不是 OpenAI 原生 tool_calls。hello_agents 1.0.0 的 SimpleAgent 只认原生
tool_calls：拿不到就"无工具调用 → 直接返回内容"，导致智能体实际上无法行动。

本模块在 LLM 调用层做透明归一化：把 DSML 响应解析成等价的 tool_calls 结构，
再交回框架原有执行逻辑。原生 tool_calls 响应原样放行，互不干扰。
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from hello_agents import HelloAgentsLLM

# DSML 标签：｜ 为全角竖线（DeepSeek 官方格式），兼容半角 | 与大小写
_BAR = "[｜|]"
_INVOKE_RE = re.compile(
    rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\s+name\s*=\s*[\"']?([\w.-]+)[\"']?\s*>(.*?)"
    rf"</\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\s*>",
    re.DOTALL | re.IGNORECASE,
)
_PARAM_RE = re.compile(
    rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\s+name\s*=\s*[\"']?([\w.-]+)[\"']?\s*>(.*?)"
    rf"(?:</\s*parameter\s*>|</\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\s*>|(?=<\s*{_BAR}\s*DSML)|$)",
    re.DOTALL | re.IGNORECASE,
)
_BLOCK_RE = re.compile(
    rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*(?:function_)?calls?\s*>.*?</\s*{_BAR}\s*DSML\s*{_BAR}\s*(?:function_)?calls?\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _coerce(value: str) -> Any:
    """DSML 参数值都是字符串，按 int → float → JSON → str 顺序转类型。"""
    v = value.strip()
    if v == "":
        return ""
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    if v[0] in "[{":
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            pass
    return v


def parse_dsml_tool_calls(content: Optional[str]) -> Optional[List[Tuple[str, Dict[str, Any]]]]:
    """从文本提取 DSML 工具调用 [(name, params), ...]；不含 DSML 时返回 None。"""
    if not content or "DSML" not in content:
        return None
    calls: List[Tuple[str, Dict[str, Any]]] = []
    for m in _INVOKE_RE.finditer(content):
        name = m.group(1)
        body = m.group(2)
        params: Dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(body):
            params[pm.group(1)] = _coerce(pm.group(2))
        calls.append((name, params))
    return calls or None


def _normalize_response(response: Any) -> Any:
    """把 DSML 响应包装成带原生 tool_calls 的响应；其余原样返回。"""
    try:
        msg = response.choices[0].message
    except (AttributeError, IndexError):
        return response
    if getattr(msg, "tool_calls", None):
        return response   # 原生 tool_calls：放行
    content = msg.content or ""
    calls = parse_dsml_tool_calls(content)
    if not calls:
        return response   # 普通文本回复：放行

    # 构造等价结构（不修改 pydantic 对象，用命名空间包装）
    tool_calls = [
        SimpleNamespace(
            id=f"dsml_{i}",
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(params, ensure_ascii=False)),
        )
        for i, (name, params) in enumerate(calls)
    ]
    remainder = _BLOCK_RE.sub(" ", content).strip()
    new_msg = SimpleNamespace(
        role=msg.role,
        content=remainder or "",
        tool_calls=tool_calls,
        function_call=getattr(msg, "function_call", None),
    )
    wrapped = SimpleNamespace()
    wrapped.choices = [SimpleNamespace(message=new_msg, finish_reason="tool_calls", index=0)]
    wrapped.usage = getattr(response, "usage", None)
    wrapped.id = getattr(response, "id", "dsml")
    wrapped.model = getattr(response, "model", "")
    return wrapped


def install_dsml_compat() -> None:
    """猴子补丁 HelloAgentsLLM.invoke_with_tools：DSML → 原生 tool_calls 归一化。"""
    original = HelloAgentsLLM.invoke_with_tools

    def patched(self, messages, tools=None, tool_choice="auto", **kwargs):
        response = original(self, messages, tools=tools, tool_choice=tool_choice, **kwargs)
        try:
            return _normalize_response(response)
        except Exception:
            return response

    patched.__name__ = original.__name__
    patched.__doc__ = original.__doc__ + "\n\n[compat] 已启用 DSML 归一化（src.compat）。"
    HelloAgentsLLM.invoke_with_tools = patched
