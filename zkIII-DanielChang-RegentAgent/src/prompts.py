# -*- coding: utf-8 -*-
"""系统提示词与观测文本格式化：把游戏局势快照压缩成 LLM 可读的中文文本。

设计原则（v1.2，与 GameAgent 1.2 / 12 扁平工具配套）：
- 动作通道 = 工具名本身，系统提示词不再罗列动作语法（避免与工具描述漂移）；
- 只教"已验证的游戏数值"，不编造；
- 对话历史每回合清空，观测文本自带：当前局势 + 最近 12 回合压缩日志 + 战略笔记；
- 观测紧凑，单回合输入控制在约 2000 字以内。
"""

from __future__ import annotations

from typing import Any, Dict, List

DEFAULT_GOAL = (
    "尽最大努力扩张领土、发展经济，避免被灭国。"
    "终极目标是吞并所有对手；优先攻击国力弱、关系差、无休战协议的邻国。"
)

SYSTEM_PROMPT = """你是《铁冠诸侯》(Lords of Iron Crowns) 的 AI 玩家，代表玩家国家参与一场回合制大战略游戏。你的所有动作都通过调用工具完成（工具名即动作通道）。

## 游戏规则速览（数值均为已验证的设定）
- 时间：1 回合 = 1 个月。每回合你可多次调用工具行动，最后必须调用 end_turn；有事件弹窗时必须先 resolve_event。
- 经济：省份税收 = 发展度×6×地形系数×(1+建筑加成)；国家月收入 = 税收合计×(0.6+稳定度/250)；军队维护费 = 总兵力×0.02/月（netIncome 已扣维护费）；国库见底则稳定度每月 -3，正常时每月 +0.5（上限 80）。
- 人力：征兵池（manpower）；动员缺口每月恢复 3%；征召兵数 = 本省可征额度（封臣领打 6 折）。
- 军队：每支军队每月只能移动一次（moved 标记）；只能移动到 moves 列表内的目标；进入敌国省份即占领（occupy，战争分数+5），要塞省份需围城 2 个月（siege）；遭遇敌军会会战（battle，分数 ±20/-10，预测含胜率）。
- 战争：宣战需无休战、未交战中；关系>-20 时需 300 金伪造宣称；你为进攻方时分数>=100 可 enforce_demands 吞并全部占领省份；white_peace 归还占领并休战 12 个月。
- 事件：每回合约 9% 概率弹出，必须处理否则游戏暂停；选项文本里写明后果。
- AI 对手不会主动宣战，但会防守反击、占领和强制执行；你的军队只能在自己的省份征兵/建造。

## 行动建议
1. 开局先攒钱建设（taxhouse/barracks 性价比高），同时在边境征兵备战；
2. 优先宣战实力最弱、关系最差的邻国，一次只打一场战争；
3. 进攻时沿 moves 列表推进：优先高胜率 battle 和直接 occupy 的目标；要塞省份提前围城；
4. 战争分数达标立即 enforce_demands，形势不利及时 white_peace 止损；
5. 用 note_strategy 记录跨回合计划（对话历史每回合清空，笔记会持续注入）；
6. end_turn 必须是你本回合的最后一个动作。

## 输出协议
- 通过调用工具完成所有行动；工具返回中文结果，被拒绝时会说明原因（可换方式重试）；
- 每回合工具调用有预算限制（移动≤4、宣战≤2、征兵≤1、建造≤2、总动作≤12），请规划好；
- 你的最终回答用 1-2 句中文概括本回合的决策与原因，不要复述状态数据。

目标：{goal}"""


def build_system_prompt(goal: str = DEFAULT_GOAL) -> str:
    return SYSTEM_PROMPT.format(goal=goal)


# ---------- 观测文本 ----------

def _relations_of(snap: Dict[str, Any], other_id: int) -> str:
    """关系矩阵是 'a_b' (a<b) 键的扁平对象，取出两国关系值。"""
    pid = snap["meta"]["playerId"]
    a, b = (pid, other_id) if pid < other_id else (other_id, pid)
    v = snap["relations"].get(f"{a}_{b}")
    if v is None:
        return "无"
    if v >= 60:
        label = "同盟"
    elif v >= 20:
        label = "友好"
    elif v > -20:
        label = "中立"
    elif v > -60:
        label = "敌对"
    else:
        label = "交恶"
    return f"{label}({v:+d})"


def _truce_of(snap: Dict[str, Any], other_id: int) -> str:
    pid = snap["meta"]["playerId"]
    for t in snap["truces"]:
        if {t["a"], t["b"]} == {pid, other_id}:
            return f"休战至第{t['until']}回合"
    return "无休战"


def _war_of(snap: Dict[str, Any], other_id: int) -> str:
    pid = snap["meta"]["playerId"]
    for w in snap["wars"]:
        if {w["attacker"], w["defender"]} == {pid, other_id}:
            side = "进攻" if w["attacker"] == pid else "防守"
            return f"交战中({side} 分数{w['score']:+d} {w['cb']})"
    return ""


def _moves_text(a: Dict[str, Any]) -> str:
    parts = []
    for mv in (a.get("moves") or [])[:8]:
        if mv["kind"] == "battle":
            fc = mv.get("forecast") or {}
            parts.append(f"→{mv['to']}战(胜率{fc.get('winPct', '?')})")
        else:
            parts.append(f"→{mv['to']}{mv['kind']}")
    return " ".join(parts)


def format_state(snap: Dict[str, Any]) -> str:
    """完整局势（工具 game_state 的输出）。"""
    m = snap["meta"]
    me = snap["player"]
    lines = [
        f"=== 局势快照（{m['year']}年 {m['month']}月 · 第 {m['turn']} 回合 · 玩家国家 #{m['playerId']}）==="
    ]
    if me:
        lines.append(
            f"【我国】{me['name']}({me['title']}) 国库:{me['treasury']}金 月收入:{me['income']} "
            f"维护费:{me.get('upkeep', 0)} 净收入:{me.get('netIncome', me['income']):+d} "
            f"人力:{me['manpower']} 稳定度:{me['stability']} 领地:{me['provs']}省"
        )
    lines.append("【各国】")
    for c in snap["countries"]:
        if c["id"] == m["playerId"]:
            continue
        rel = f" 关系:{_relations_of(snap, c['id'])} {_truce_of(snap, c['id'])} {_war_of(snap, c['id'])}"
        lines.append(
            f"- #{c['id']} {c['name']}({c['title']}): {c['provs']}省 "
            f"国库{c['treasury']} 人力{c['manpower']} 收入{c['income']} 稳定{c['stability']}{rel}"
        )
    lines.append("【我的省份】")
    for p in snap["myProvinces"]:
        bld = p["building"] or "无建筑"
        lines.append(
            f"- #{p['id']} {p['name']} 发展{p['dev']} 税收{p.get('tax', '?')} 防御×{p.get('defMult', 1)} "
            f"{bld} 可征{p['levy']}人"
        )
    mine = [a for a in snap["armies"] if a["owner"] == m["playerId"]]
    lines.append("【我军】" if mine else "【我军】无")
    for a in mine:
        moved = "已行动" if a["moved"] else "可行动"
        siege = f" 围城{a['siege']}/2" if a["siege"] else ""
        moves = _moves_text(a) if not a["moved"] else ""
        lines.append(f"- 军#{a['id']} {a['name']} {a['troops']}人 @省{a['provId']} {moved}{siege}")
        if moves:
            lines.append(f"  可移动: {moves}")
    lines.append("【战争】" if snap["wars"] else "【战争】无")
    for w in snap["wars"]:
        a_name = snap["countries"][w["attacker"]]["name"]
        d_name = snap["countries"][w["defender"]]["name"]
        lines.append(f"- 战#{w['id']} {a_name}→{d_name} 分数{w['score']:+d} ({w['cb']})")
    evt = snap.get("pendingEvent")
    lines.append("【待处理事件】" if evt else "【待处理事件】无")
    if evt:
        opts = " ".join(f"{i}){o['text']}" for i, o in enumerate(evt["options"]))
        lines.append(f"{evt['title']}: {evt['body']}\n选项: {opts}")
    return "\n".join(lines)


def format_turn_observation(
    snap: Dict[str, Any],
    new_logs: List[Dict[str, Any]],
    journal: Optional[List[str]] = None,
    notes: Optional[List[str]] = None,
) -> str:
    """每回合投喂给智能体的观测文本（局势 + 战报 + 日志 + 笔记）。"""
    m = snap["meta"]
    me = snap["player"]
    head = [f"现在是 {m['year']}年 {m['month']}月（第 {m['turn']} 回合）。"]
    if me:
        head.append(
            f"我国：{me['name']}，国库 {me['treasury']} 金（净收入 {me.get('netIncome', 0):+d}/月），"
            f"人力 {me['manpower']}，稳定度 {me['stability']}，领地 {me['provs']} 省。"
        )
        buildable = [p["id"] for p in snap["myProvinces"] if not p["building"]]
        if buildable:
            head.append(f"可建造省份（无建筑）：{buildable}。")
    others = []
    for c in snap["countries"]:
        if c["id"] == m["playerId"]:
            continue
        others.append(
            f"#{c['id']} {c['name']}（{c['provs']}省 人力池{c['manpower']} 关系:{_relations_of(snap, c['id'])} "
            f"{_truce_of(snap, c['id'])} {_war_of(snap, c['id'])}）"
        )
    head.append("对手：" + "；".join(others) + "。")
    mine = [a for a in snap["armies"] if a["owner"] == m["playerId"]]
    if mine:
        parts = []
        for a in mine:
            moved = "已行动" if a["moved"] else "可行动"
            moves = _moves_text(a) if not a["moved"] else ""
            parts.append(f"军#{a['id']} {a['name']} {a['troops']}人@省{a['provId']} {moved}{(' 可移:' + moves) if moves else ''}")
        head.append("我军：" + "；".join(parts) + "。")
    else:
        head.append("我军：无。")
    if snap["wars"]:
        parts = []
        for w in snap["wars"]:
            a_name = snap["countries"][w["attacker"]]["name"]
            d_name = snap["countries"][w["defender"]]["name"]
            parts.append(f"战#{w['id']} {a_name}→{d_name} 分数{w['score']:+d}")
        head.append("战争：" + "；".join(parts) + "。")
    text = "\n".join(head)

    if notes:
        text += "\n【战略笔记】\n" + "\n".join(f"- {n}" for n in notes[-5:])
    if journal:
        text += "\n【近期回顾】\n" + "\n".join(journal[-12:])
    if new_logs:
        text += "\n【自上回合以来的新消息】\n" + "\n".join(
            f"- [{l['time']}] {l['title']}: {l['body']}" for l in new_logs[:8]
        )
    text += (
        "\n请规划并执行本回合行动：必要时先调用 game_state 查看完整局势，"
        "然后按需调用行动工具（征兵/集结/移动/宣战/建造等），"
        "最后调用 end_turn 结束回合。"
    )
    return text


def format_event_observation(snap: Dict[str, Any]) -> str:
    """事件弹窗出现时的观测文本（此时回合已结束，只需处理事件）。"""
    evt = snap.get("pendingEvent") or {}
    opts = "\n".join(f"  {i}) {o['text']}" for i, o in enumerate(evt.get("options", [])))
    return (
        f"游戏弹出了事件弹窗，当前暂停中。\n"
        f"【事件】{evt.get('title', '')}：{evt.get('body', '')}\n"
        f"选项：\n{opts}\n"
        f"请调用 resolve_event(index=序号) 选择（从 0 开始），然后用一句话说明你的选择理由。"
    )
