# -*- coding: utf-8 -*-
"""hello-agents 自定义工具：让智能体观察与操控《铁冠诸侯》。

设计要点（v1.2，与游戏侧 GameAgent 1.2 配套）：
- 12 个扁平工具，每个游戏动作一个工具、参数强类型（integer/string）。
  工具名本身就是动作通道——hello_agents 的 schema 不支持 enum，扁平工具
  比"一个通用 act(action, params_json)"对弱模型更可靠。
- 游戏规则性拒绝（国库不足/只能相邻移动等）是给智能体的反馈，不算工具错误，
  一律返回 success（文本以"失败："开头）——避免框架熔断器误判。
- 每回合预算（BudgetTracker）：移动≤4、宣战≤2、征兵≤1、建造≤2，
  防止弱模型把工具调用额度烧在同一类动作上。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse

from .game_bridge import GameBridge
from .prompts import format_state


class BudgetTracker:
    """每回合动作预算（驱动器在 beginTurn 时 reset）。"""

    LIMITS = {"move": 4, "declare": 2, "raise": 1, "build": 2}

    def __init__(self) -> None:
        self._used: Dict[str, int] = {}

    def reset(self) -> None:
        self._used = {}

    def consume(self, category: str) -> Optional[str]:
        """尝试占用一次预算；超限返回中文提示，未超限返回 None。"""
        limit = self.LIMITS.get(category)
        if limit is None:
            return None
        used = self._used.get(category, 0)
        if used >= limit:
            return f"本回合「{category}」类动作已达上限 {limit} 次，请考虑 end_turn"
        self._used[category] = used + 1
        return None


class _GameToolBase(Tool):
    """游戏工具公共基类：桥引用 + 轨迹回调 + 统一执行与结果封装。"""

    def __init__(self, name: str, description: str, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(name=name, description=description)
        self.bridge = bridge
        self.controller = controller

    def _run_action(self, action: str, params: Dict[str, Any]) -> ToolResponse:
        try:
            result = self.bridge.act(action, params)
        except Exception as e:
            return ToolResponse.error("GAME_BRIDGE_ERROR", f"游戏桥异常：{e}")
        if self.controller is not None:
            self.controller.record_action(action, params, result)
        if result.get("ok"):
            return ToolResponse.success(text=str(result.get("msg", "成功")), data=result)
        return ToolResponse.success(text=f"失败：{result.get('msg', '未知原因')}", data=result)


class GameStateTool(_GameToolBase):
    """查看游戏完整局势。"""

    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(
            "game_state",
            "查看《铁冠诸侯》当前完整局势：时间、玩家国家（国库/月净收入/人力/稳定度/领地）、"
            "所有国家的国力与外交关系、我的省份（发展/税收/可征兵/防御倍率）、"
            "我的军队（位置/兵力/是否已行动/合法移动列表含战斗胜率预测）、"
            "进行中的战争（分数）、待处理事件。",
            bridge, controller,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return []

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        try:
            snap = self.bridge.get_state()
            return ToolResponse.success(text=format_state(snap), data={"turn": snap["meta"]["turn"]})
        except Exception as e:
            return ToolResponse.error("GAME_STATE_ERROR", f"读取游戏状态失败：{e}")


class EndTurnTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(
            "end_turn",
            "结束当前回合进入下个月（经济/人口/战争/事件全部结算）。每回合只能调用一次，"
            "且必须是你本回合的最后一个动作；有事件弹窗时必须先用 resolve_event 处理。",
            bridge, controller,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return []

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        return self._run_action("endTurn", {})


class RaiseArmyTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None, budget: Optional[BudgetTracker] = None) -> None:
        super().__init__(
            "raise_army",
            "在指定己方省份征召一支军队（每省一次至少 20 人，受人力池限制，同省友军自动合并）。"
            "需要参数 provId（己方省份 id，见 game_state 的我的省份列表）。",
            bridge, controller,
        )
        self.budget = budget if budget is not None else (controller.budget if controller is not None else BudgetTracker())

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="provId", type="integer", description="己方省份 id", required=True)]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        hit = self.budget.consume("raise")
        if hit:
            return ToolResponse.success(text="失败：" + hit)
        return self._run_action("raiseArmy", {"provId": parameters.get("provId")})


class RallyAllTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(
            "rally_all",
            "全领土征召新兵并把所有部队集结到集结点（选中省份或首都），合并为主力军团。",
            bridge, controller,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return []

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        return self._run_action("rallyAll", {})


class MoveArmyTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None, budget: Optional[BudgetTracker] = None) -> None:
        super().__init__(
            "move_army",
            "将己方军队移动到相邻省份。只能移动 game_state 中该军队 moves 列表内的目标："
            "进入敌国省份会占领（occupy）/围城（siege，需 2 个月）/会战（battle，附胜率预测）。"
            "参数：armyId（军队 id）、provId（目标省份 id）。",
            bridge, controller,
        )
        self.budget = budget if budget is not None else (controller.budget if controller is not None else BudgetTracker())

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="armyId", type="integer", description="军队 id", required=True),
            ToolParameter(name="provId", type="integer", description="目标省份 id（须在该军队 moves 列表内）", required=True),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        hit = self.budget.consume("move")
        if hit:
            return ToolResponse.success(text="失败：" + hit)
        return self._run_action("moveArmy", {"armyId": parameters.get("armyId"), "provId": parameters.get("provId")})


class DisbandArmyTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(
            "disband_army",
            "解散己方军队（约半数士兵返回人力池，可省维护费）。参数 armyId（军队 id）。",
            bridge, controller,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="armyId", type="integer", description="军队 id", required=True)]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        return self._run_action("disbandArmy", {"armyId": parameters.get("armyId")})


class BuildTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None, budget: Optional[BudgetTracker] = None) -> None:
        super().__init__(
            "build",
            "在己方省份建造建筑（每省限一座）：taxhouse 税务所 300 金(+50%税)、barracks 兵营 400 金(+80%人力)、"
            "market 市场 500 金(+30%税+10%人力)、fortress 要塞 350 金(防御×1.5，围城需 2 月)。"
            "参数：provId（己方省份 id）、key（建筑代号）。",
            bridge, controller,
        )
        self.budget = budget if budget is not None else (controller.budget if controller is not None else BudgetTracker())

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="provId", type="integer", description="己方省份 id", required=True),
            ToolParameter(name="key", type="string", description="建筑代号：taxhouse/barracks/market/fortress", required=True),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        hit = self.budget.consume("build")
        if hit:
            return ToolResponse.success(text="失败：" + hit)
        return self._run_action("build", {"provId": parameters.get("provId"), "key": parameters.get("key")})


class DeclareWarTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None, budget: Optional[BudgetTracker] = None) -> None:
        super().__init__(
            "declare_war",
            "向目标国家宣战（战争分数从 0 开始）。关系>-20 时需 300 金伪造宣称，关系<=-20 免费。"
            "参数：defenderId（目标国家 id，见 game_state 的各国列表）。",
            bridge, controller,
        )
        self.budget = budget if budget is not None else (controller.budget if controller is not None else BudgetTracker())

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="defenderId", type="integer", description="目标国家 id", required=True)]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        hit = self.budget.consume("declare")
        if hit:
            return ToolResponse.success(text="失败：" + hit)
        return self._run_action("declareWar", {"defenderId": parameters.get("defenderId")})


class WhitePeaceTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(
            "white_peace",
            "对指定战争提出白色和平（归还占领、休战 12 个月、关系 +10）。参数 warId（战争 id）。",
            bridge, controller,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="warId", type="integer", description="战争 id", required=True)]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        return self._run_action("whitePeace", {"warId": parameters.get("warId")})


class EnforceDemandsTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(
            "enforce_demands",
            "强制执行战争需求（吞并所有已占领省份、休战 24 个月）。你为进攻方需分数>=100，防守方需<=-100。"
            "参数 warId（战争 id）。",
            bridge, controller,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="warId", type="integer", description="战争 id", required=True)]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        return self._run_action("enforceDemands", {"warId": parameters.get("warId")})


class ResolveEventTool(_GameToolBase):
    def __init__(self, bridge: GameBridge, controller: Optional[Any] = None) -> None:
        super().__init__(
            "resolve_event",
            "对当前事件弹窗选择第 index 个选项（index 从 0 开始，选项列表见 game_state 的待处理事件）。",
            bridge, controller,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="index", type="integer", description="选项序号（从 0 开始）", required=True)]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        return self._run_action("eventChoice", {"index": parameters.get("index")})


class NoteStrategyTool(Tool):
    """智能体的跨回合长期记忆（驱动器每回合清空对话历史，笔记会被持续注入）。"""

    def __init__(self, controller: Optional[Any] = None) -> None:
        super().__init__(
            name="note_strategy",
            description=(
                "记录跨回合的战略笔记（例如「先灭西边的奥斯王国，再转向东边」）。"
                "对话历史每回合会被清空，只有笔记会被持续注入到后续回合的局势观测中。"
                "参数 content（笔记内容，中文，一句话到三句话）。"
            ),
        )
        self.controller = controller

    def get_parameters(self) -> List[ToolParameter]:
        return [ToolParameter(name="content", type="string", description="笔记内容", required=True)]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        content = str(parameters.get("content") or "").strip()
        if not content:
            return ToolResponse.success(text="失败：笔记内容为空")
        if self.controller is not None:
            self.controller.add_note(content)
        return ToolResponse.success(text=f"笔记已记录（共 {len(self.controller.notes) if self.controller else 1} 条）")


def build_tool_set(bridge: GameBridge, controller: Optional[Any] = None) -> List[Tool]:
    """装配全部 12 个工具（controller 提供预算与笔记回调）。"""
    budget = controller.budget if controller is not None else BudgetTracker()
    return [
        GameStateTool(bridge, controller),
        EndTurnTool(bridge, controller),
        RaiseArmyTool(bridge, controller, budget),
        RallyAllTool(bridge, controller),
        MoveArmyTool(bridge, controller, budget),
        DisbandArmyTool(bridge, controller),
        BuildTool(bridge, controller, budget),
        DeclareWarTool(bridge, controller, budget),
        WhitePeaceTool(bridge, controller),
        EnforceDemandsTool(bridge, controller),
        ResolveEventTool(bridge, controller),
        NoteStrategyTool(controller),
    ]
