# game/ —— 游戏打包副本

本目录是《铁冠诸侯》`index.html` 的**打包副本**，让本项目在提交到 hello-agents
协作仓库后依然可以独立运行（原始游戏位于本项目的上级目录，不会随提交带走）。

桥接层按以下优先级寻找游戏目录（见 `src/game_bridge.py`）：

1. 环境变量 `IRONCROWNS_GAME_DIR`
2. 本目录 `game/index.html`（提交布局）
3. 上级目录 `../index.html`（开发布局）

## 同步方式

游戏更新后（如给 `GameAgent` 桥接层加新动作），从上级目录重新复制：

```bash
# 在项目根目录执行
cp ../index.html game/index.html
```
