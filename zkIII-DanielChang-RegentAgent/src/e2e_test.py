# -*- coding: utf-8 -*-
"""端到端冒烟测试：真实 LLM 智能体玩 6 回合。

手动运行：python src/e2e_test.py
（不做 pytest 测试：有 __main__ 守卫，导入本文件不会启动浏览器 / 触发付费 LLM 调用）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

    from hello_agents import HelloAgentsLLM

    from src.game_bridge import GameBridge
    from src.game_agent import CampaignController

    bridge = GameBridge().start(seed=20260901, player_id=0)
    try:
        controller = CampaignController(bridge, HelloAgentsLLM(), max_tool_iterations=12)
        rows = controller.run_campaign(max_turns=6, screenshot_every=3)
        print("\n=== 战果 ===")
        for r in rows:
            p = r["player"]
            acts = ", ".join((a["action"] + ("" if a["ok"] else "✗")) for a in r["actions"]) or "无"
            flags = "[兜底]" if (r.get("forced_end") or r.get("forced_event_choice")) else ""
            print(
                f"{r['year']}年{r['month']:>2}月 | 领地{p['provinces']:>2}省 | 国库{p['treasury']:>5} | "
                f"人力{p['manpower']:>5} | 战争{len(r['wars'])}场 | 动作: {acts} {flags}"
            )
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
