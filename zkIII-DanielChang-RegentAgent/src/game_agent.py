# -*- coding: utf-8 -*-
"""智能体装配与战役主循环（v2 循环协议）。

循环协议（四层防卡死，与游戏侧 GameAgent 1.2 回合窗口守卫配套）：
- 回合窗口：驱动器每回合 beginTurn → 智能体行动 → endTurn（智能体或驱动器兜底）。
- 事件弹窗：单选项弹窗（胜利通知等）由驱动器直接关闭；多选项事件交给智能体
  resolve_event，未处理则自动选择 primary 选项（AUTO_EVENT）。
- LLM 失败检测：SimpleAgent 吞掉底层异常返回空串 —— 空回复 = 失败，重试一次，
  仍失败则本回合由策略控制器兜底，连续 3 回合失败终止战役。
- 灭国（领地 0 省）即终止；回合数用尽为正常结束。

策略控制器（PolicyController）是零 LLM 的确定性基线：既作 --smoke 演示路径
（不花 API 钱），也作 LLM 失败时的兜底。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from hello_agents import Config, SimpleAgent, ToolRegistry

from .game_bridge import GameBridge
from .game_tools import BudgetTracker, build_tool_set
from .prompts import (
    DEFAULT_GOAL,
    build_system_prompt,
    format_event_observation,
    format_turn_observation,
)

ACTION_CATEGORY = {   # 动作 → 预算类别（供轨迹统计）
    "moveArmy": "move", "declareWar": "declare", "raiseArmy": "raise", "build": "build",
}


class PolicyController:
    """零 LLM 的确定性基线策略：建设 → 征兵 → 打最弱邻国 → 推进占领。"""

    def __init__(self, bridge: GameBridge) -> None:
        self.bridge = bridge

    def take_turn(self, snap: Dict[str, Any]) -> str:
        """按快照执行一回合确定性行动，返回本回合决策描述。"""
        m, me = snap["meta"], snap["player"]
        acts: List[str] = []

        # 1) 经济：国库充裕且有空省 → 建造（优先税务所，其次兵营）
        if me["treasury"] >= 900:
            for key in ("taxhouse", "barracks"):
                cand = next((p for p in snap["myProvinces"] if not p["building"]), None)
                if cand and me["treasury"] >= 900:
                    r = self.bridge.act("build", {"provId": cand["id"], "key": key})
                    if r.get("ok"):
                        acts.append(f"建造{key}于省{cand['id']}")
                        break

        # 2) 备战：人力充裕且无战争或战力劣势 → 在首都征兵
        at_war = any(w["attacker"] == m["playerId"] or w["defender"] == m["playerId"] for w in snap["wars"])
        if me["manpower"] >= 120 and (not at_war or len([a for a in snap["armies"] if a["owner"] == m["playerId"]]) == 0):
            cap = me["capital"]
            r = self.bridge.act("raiseArmy", {"provId": cap})
            if r.get("ok"):
                acts.append(f"征兵于省{cap}")

        # 3) 宣战：无战争时挑人力最少的无休战对手（关系<=-20 免费，否则需 600+ 金）
        if not at_war:
            targets = [
                c for c in snap["countries"]
                if c["id"] != m["playerId"]
                and not any(w["attacker"] == c["id"] or w["defender"] == c["id"] for w in snap["wars"])
                and not any({t["a"], t["b"]} == {m["playerId"], c["id"]} for t in snap["truces"])
            ]
            if targets:
                target = min(targets, key=lambda c: (c["manpower"], c["provs"]))
                rel_key = f"{min(m['playerId'], target['id'])}_{max(m['playerId'], target['id'])}"
                rel = snap["relations"].get(rel_key, 0)
                if rel <= -20 or me["treasury"] >= 600:
                    r = self.bridge.act("declareWar", {"defenderId": target["id"]})
                    if r.get("ok"):
                        acts.append(f"向#{target['id']}宣战")
                        at_war = True

        # 4) 战争中：每支可动军队沿 moves 列表推进（高胜率战斗 > 占领 > 围城 > 行军）
        if at_war:
            for _ in range(4):
                snap = self.bridge.get_state()   # 每次移动后刷新合法移动列表
                movers = [x for x in snap["armies"] if x["owner"] == m["playerId"] and not x["moved"] and (x.get("moves") or [])]
                if not movers:
                    break
                order = {"occupy": 0, "battle": 1, "siege": 2, "march": 3}

                def score(mv: Dict[str, Any]) -> tuple:
                    if mv["kind"] == "battle":
                        fc = mv.get("forecast") or {}
                        return (0 if fc.get("winPct", 0) >= 0.6 else 2, 0)
                    return (order.get(mv["kind"], 9), 0)

                a = movers[0]
                best = min(a["moves"], key=score)
                r = self.bridge.act("moveArmy", {"armyId": a["id"], "provId": best["to"]})
                if r.get("ok"):
                    acts.append(f"军{a['id']}→省{best['to']}({best['kind']})")
                else:
                    break

        # 5) 战争分数达标 → 强制执行
        for w in snap["wars"]:
            if w["attacker"] == m["playerId"] and w["score"] >= 100:
                r = self.bridge.act("enforceDemands", {"warId": w["id"]})
                if r.get("ok"):
                    acts.append(f"强制执行战#{w['id']}")
            elif w["defender"] == m["playerId"] and w["score"] <= -100:
                r = self.bridge.act("enforceDemands", {"warId": w["id"]})
                if r.get("ok"):
                    acts.append(f"强制执行战#{w['id']}")

        # 6) 结束回合
        self.bridge.act("endTurn")
        return "；".join(acts) if acts else "休整（结束回合）"


class CampaignController:
    """装配智能体并驱动 N 回合战役。llm=None 时使用策略控制器（零 API 成本）。"""

    def __init__(
        self,
        bridge: GameBridge,
        llm=None,
        goal: str = DEFAULT_GOAL,
        out_dir: str = "outputs",
        max_tool_iterations: int = 12,
    ) -> None:
        self.bridge = bridge
        self.llm = llm
        self.goal = goal
        # 相对路径一律锚定项目根，避免从别的 cwd 运行时产物散落
        self.out_dir = Path(out_dir)
        if not self.out_dir.is_absolute():
            self.out_dir = Path(__file__).resolve().parent.parent / self.out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_tool_iterations = max_tool_iterations

        self.budget = BudgetTracker()
        self.notes: List[str] = []
        self.journal: List[str] = []
        self.policy = PolicyController(bridge)

        registry = ToolRegistry()
        for tool in build_tool_set(bridge, self):
            registry.register_tool(tool)

        self.agent = None
        if llm is not None:
            config = Config(
                skills_auto_register=False,
                subagent_enabled=False,
                todowrite_enabled=False,
                devlog_enabled=False,
                trace_enabled=False,
                session_enabled=False,
                circuit_enabled=False,
            )
            self.agent = SimpleAgent(
                name="铁冠诸侯AI",
                llm=llm,
                system_prompt=build_system_prompt(goal),
                config=config,
                tool_registry=registry,
                max_tool_iterations=max_tool_iterations,
            )

        self._turn_actions: List[Dict[str, Any]] = []
        self._last_log_id = -1

    # ---------- 工具回调 ----------

    def record_action(self, action: str, params: Dict[str, Any], result: Dict[str, Any]) -> None:
        """动作工具每次执行后回调，用于轨迹落盘。"""
        self._turn_actions.append(
            {
                "action": action, "params": params,
                "ok": bool(result.get("ok")), "msg": result.get("msg", ""),
                "category": ACTION_CATEGORY.get(action, "other"),
            }
        )
        if action == "newGame":
            self._last_log_id = -1

    def add_note(self, content: str) -> None:
        self.notes.append(f"[{len(self.notes) + 1}] {content}")

    # ---------- 主循环 ----------

    def run_campaign(
        self,
        max_turns: int = 24,
        screenshot_every: int = 0,
        jsonl_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """运行战役：最多 max_turns 个回合，返回每回合记录。"""
        jsonl_path = jsonl_path or str(self.out_dir / "campaign.jsonl")
        rows: List[Dict[str, Any]] = []
        failures = 0
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for step in range(max_turns):
                snap = self.bridge.get_state()
                if snap["meta"].get("gameOver"):
                    print("[战役结束] 玩家失去全部领地，国家灭亡。")
                    break

                self._turn_actions = []
                self.budget.reset()
                new_logs = self._delta_logs(snap)
                kind = "turn"
                forced_end = False
                forced_event = False
                reply = ""

                evt = snap.get("pendingEvent")
                if evt and len(evt["options"]) > 1:
                    # 多选项事件：智能体决策（策略控制器也支持）
                    kind = "event"
                    reply = self._decide(format_event_observation(snap))
                    if self.bridge.get_state().get("pendingEvent"):
                        # 兜底：自动选择 primary 选项
                        primary = next((i for i, o in enumerate(evt["options"]) if o.get("primary")), 0)
                        self.bridge.act("eventChoice", {"index": primary})
                        forced_event = True
                    snap = self.bridge.get_state()
                    new_logs += self._delta_logs(snap)   # 累计事件处理期间的新消息，不覆盖
                else:
                    if evt:
                        # 单选项弹窗（胜利通知等）：直接关闭
                        self.bridge.act("eventChoice", {"index": 0})
                        snap = self.bridge.get_state()
                    self.bridge.act("beginTurn")
                    obs = format_turn_observation(snap, new_logs, journal=self.journal, notes=self.notes)
                    reply = self._decide(obs)
                    if self.agent is not None and reply == "":
                        # LLM 无有效回复：本回合由策略控制器兜底
                        reply = "[LLM无回复，策略兜底] " + self.policy.take_turn(snap)
                        forced_end = True
                    else:
                        after = self.bridge.get_state()
                        if not after["meta"].get("turnEnded"):
                            self.bridge.act("endTurn")
                            forced_end = True
                    snap = self.bridge.get_state()

                row = self._row(snap, kind, step, reply, new_logs, forced_end, forced_event)
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                self.journal.append(
                    f"{snap['meta']['year']}年{snap['meta']['month']:>2}月 领地{snap['player']['provs']}省 "
                    f"国库{snap['player']['treasury']} 人力{snap['player']['manpower']} 战争{len(snap['wars'])}场"
                )

                if reply == "" and self.agent is not None:
                    failures += 1
                    if failures >= 3:
                        print("[战役中止] LLM 连续 3 回合无有效回复，轨迹已保存。")
                        break
                else:
                    failures = 0

                if screenshot_every and (step + 1) % screenshot_every == 0:
                    self.bridge.screenshot(self.out_dir / f"turn_{snap['meta']['turn']:04d}.png")

                print(
                    f"[回合 {step + 1}/{max_turns}] {snap['meta']['year']}年{snap['meta']['month']:>2}月 "
                    f"领地{snap['player']['provs']:>2}省 国库{snap['player']['treasury']:>5} "
                    f"人力{snap['player']['manpower']:>5} 战争{len(snap['wars'])}场 "
                    f"动作{len(self._turn_actions)}次{'(兜底)' if forced_end or forced_event else ''}"
                )
        return rows

    def _decide(self, observation: str) -> str:
        """LLM 决策（空回复视为失败，重试一次）；无 LLM 时走策略控制器。"""
        if self.agent is None:
            snap = self.bridge.get_state()
            return self.policy.take_turn(snap) if not snap.get("pendingEvent") else ""
        try:
            self.agent.clear_history()
        except Exception:
            pass
        reply = self.agent.run(observation) or ""
        if reply.strip():
            return reply.strip()
        try:
            reply = self.agent.run(observation) or ""   # 重试一次
        except Exception:
            reply = ""
        return reply.strip()

    # ---------- 辅助 ----------

    def _delta_logs(self, snap: Dict[str, Any]) -> List[Dict[str, Any]]:
        """自上轮以来新增的游戏消息（按日志 id 差分；id 回退时兜底取最近几条）。"""
        logs = snap.get("logs", [])
        if not logs:
            return []
        if logs[-1]["id"] < self._last_log_id:
            fresh = logs[-6:]
        else:
            fresh = [l for l in logs if l["id"] > self._last_log_id]
        self._last_log_id = logs[-1]["id"]
        return fresh

    def _row(
        self,
        snap: Dict[str, Any],
        kind: str,
        step: int,
        reply: str,
        new_logs: List[Dict[str, Any]],
        forced_end: bool,
        forced_event: bool,
    ) -> Dict[str, Any]:
        m, me = snap["meta"], snap["player"]
        return {
            "step": step + 1,
            "kind": kind,
            "year": m["year"],
            "month": m["month"],
            "turn": m["turn"],
            "player": {
                "id": m["playerId"],
                "name": me["name"],
                "provinces": me["provs"],
                "treasury": me["treasury"],
                "manpower": me["manpower"],
                "stability": me["stability"],
            },
            "armies": [
                {"id": a["id"], "troops": a["troops"], "provId": a["provId"], "moved": a["moved"]}
                for a in snap["armies"]
                if a["owner"] == m["playerId"]
            ],
            "wars": [{"id": w["id"], "attacker": w["attacker"], "defender": w["defender"], "score": w["score"]} for w in snap["wars"]],
            "new_logs": [{"time": l["time"], "title": l["title"], "body": l["body"]} for l in new_logs],
            "actions": self._turn_actions,
            "ai_reply": reply[:500],
            "forced_end": forced_end,
            "forced_event_choice": forced_event,
        }
