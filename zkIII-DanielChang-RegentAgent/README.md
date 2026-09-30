# 铁冠诸侯 · AI 执政官（zkIII-DanielChang-RegentAgent）

> 基于 HelloAgents 框架的智能体，自主操控网页大战略游戏《铁冠诸侯》(Lords of Iron Crowns)——发展经济、征兵作战、吞并邻国。

## 📝 项目简介

- **解决什么问题？** 大战略游戏状态复杂、动作多样，传统脚本 AI 难以通盘决策。本项目打通了"LLM 智能体 ↔ 网页游戏"的桥接链路：智能体以中文观测文本理解局势，通过函数调用执行游戏动作，像人类玩家一样逐回合决策。
- **有什么特色功能？**
  - 游戏侧新增 `window.GameAgent` 桥接 API（局势快照 / 动作分发 / 回合窗口守卫 / 事件弹窗处理），仅做 id→对象 解析，不重复实现任何游戏逻辑；
  - Python 侧以 Playwright 无头 Edge（工作线程内运行，Jupyter 与脚本通用；`channel="msedge"` 免下载浏览器）+ 本地静态服务驱动真实游戏页面，开箱即用；
  - 12 个强类型 HelloAgents 工具（`game_state` + 10 个动作工具 + `note_strategy` 战略笔记），每回合动作预算防刷屏；
  - DeepSeek DSML 兼容层：把 deepseek-chat 的 `<｜DSML｜ invoke>` 文本工具调用归一化为原生 tool_calls，框架零改动；
  - 战役全程留档：每回合 AI 决策原文、动作轨迹、国力指标写入 JSONL，定期截图，可完整复盘；
  - 确定性回放：地图种子固定 + 全逻辑 rng 播种（含撤退目的地），同种子同决策 = 同战局。
- **适用于什么场景？** Hello-Agents 课程毕业设计、游戏 AI 评测、回合制策略游戏的 LLM 基准测试。

## ✨ 核心功能

- [x] 智能体观察：每回合生成紧凑中文局势观测（国力/净收入/关系/军队含合法移动列表与战斗胜率预测/战争/事件/新消息差分/战略笔记）
- [x] 智能体行动：10 类游戏动作（宣战/征召/集结/移动/解散/建造/议和/强制吞并/事件选择/结束回合）全部可用
- [x] 事件处理：随机事件弹窗经 `pendingEvent` 快照暴露，智能体 `resolve_event` 选择；未处理自动选 primary 选项兜底
- [x] 战役主循环：SimpleAgent 每回合决策 → 工具行动 → 强制推进回合（LLM 失败自动切换策略基线），演示永不中断
- [x] 复盘产物：`outputs/campaign.jsonl` 轨迹 + `outputs/turn_*.png` 截图

## 🛠️ 技术栈

- HelloAgents 框架（`SimpleAgent` + `ToolRegistry` 函数调用范式）
- 桥接：Playwright（无头 Edge）+ Python `http.server` 本地静态服务
- 游戏：《铁冠诸侯》单文件 JS（内置 GameAgent 桥接模块）
- 其他依赖：`python-dotenv`；LLM 为 OpenAI 兼容接口（默认 DeepSeek）

## 🚀 快速开始

### 环境要求

- Python 3.10+
- 系统自带 Edge（Playwright 直接复用，无需 `playwright install`）；如需 Chrome 请把 `src/game_bridge.py` 中的 `channel="msedge"` 改为 `channel="chrome"`

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置 API 密钥

```bash
cp .env.example .env   # 编辑 .env，填入 LLM_API_KEY / LLM_MODEL_ID / LLM_BASE_URL
```

### 运行项目

```bash
jupyter lab            # 打开 main.ipynb 从头执行即可
```

也可以用脚本方式运行等价流程：

```python
from dotenv import load_dotenv; load_dotenv(".env")
from hello_agents import HelloAgentsLLM
from src.game_bridge import GameBridge
from src.game_agent import CampaignController

bridge = GameBridge().start(seed=20260901, player_id=0)   # 无头浏览器开新局
controller = CampaignController(bridge, HelloAgentsLLM(), max_tool_iterations=12)
rows = controller.run_campaign(max_turns=12, screenshot_every=6)   # 12 回合战役
bridge.close()
```

## 📖 使用示例

运行 `main.ipynb` 后，每回合输出形如：

```text
[回合 1/6] 1066年 2月 领地10省 国库3284 人力476 战争0场 动作5次
1066年 2月 | 领地10省 | 国库3284 | 人力476 | 战争0场 | 动作: build, build, raise_army, move_army, end_turn
1066年 4月 | 领地10省 | 国库3927 | 人力211 | 战争1场 | 动作: raise_army, move_army, declare_war, end_turn
...
```

`outputs/campaign.jsonl` 中每行包含 AI 该回合的决策原文（`ai_reply`）与全部动作轨迹，可逐回合复盘。

## 🎯 项目亮点

- **零侵入桥接**：游戏只新增一个 GameAgent 模块（约 300 行），全部游戏逻辑复用既有函数，人类游玩不受影响
- **回合窗口守卫**：游戏侧强制"每回合只能 end_turn 一次、动作上限 12 次"，杜绝模型重复结束回合
- **永不卡死的主循环**：end_turn / resolve_event 双重兜底 + LLM 失败自动切换策略基线，任何模型失误都不中断演示
- **规则拒绝 ≠ 工具错误**：游戏拒绝（国库不足等）作为正常反馈返回 success，避免框架熔断器误判

## 📊 性能评估

（2026-09-30 实测，deepseek-chat，6 回合战役）

- 单回合决策耗时：约 30~60 秒（含 4~6 次工具调用）
- 6 回合战役：约 5 分钟；12 回合预计 10 分钟左右
- 行动合法性：游戏侧校验兜底，非法动作 100% 被拒绝并反馈原因
- 零 API 路径：LLM 不可用时自动切换策略基线，6 回合约 20 秒

## 🔮 未来计划

- [ ] 多模态视觉输入：截图直接喂给 VLM 决策
- [ ] 长期记忆：战局复盘写入记忆，跨战役学习
- [ ] 范式对比：ReAct / Reflection / Plan-and-Solve 同局对战评测
- [ ] 玩家接管：`headless=False` 实景观看 + 中途切换人工操控

## 🤝 贡献指南

欢迎提出 Issue 和 Pull Request！

## 📄 许可证

MIT License

## 👤 作者

- GitHub: [@zkIII-DanielChang](https://github.com/zkIII-DanielChang)
- Email: [daniel-chang@live.cn]

## 🙏 致谢

感谢 Datawhale 社区和 Hello-Agents 项目！
