# -*- coding: utf-8 -*-
"""游戏桥接层：在无头浏览器中运行《铁冠诸侯》，把 window.GameAgent 的 JS API
代理成线程安全的 Python 方法，供 hello-agents 智能体调用。

架构（重要）
------------
所有 Playwright 操作都在一个独立工作线程中执行。原因：Playwright 同步 API
会在已有运行中 asyncio 事件循环的线程里拒绝启动（"Sync API inside the asyncio
loop"），而 Jupyter 内核主线程恰好有运行中的事件循环。工作线程内没有循环，
因此同一套代码在脚本与 notebook 中都能运行。

外部只接触 GameBridge 门面：命令经 queue 送入工作线程，结果经 Future 返回。

依赖
----
pip install playwright          # 无需 playwright install，复用系统 Edge
"""

from __future__ import annotations

import http.server
import os
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from playwright.sync_api import sync_playwright

_PROJECT_ROOT = Path(__file__).resolve().parent.parent   # src/ 的上一级 = 项目根


def _default_game_dir() -> Path:
    """按优先级找游戏目录：环境变量 → 项目内打包副本（提交布局）→ 上级目录（开发布局）。"""
    env = os.environ.get("IRONCROWNS_GAME_DIR")
    if env:
        return Path(env)
    bundled = _PROJECT_ROOT / "game"
    if (bundled / "index.html").exists():
        return bundled
    dev_layout = _PROJECT_ROOT.parent
    if (dev_layout / "index.html").exists():
        return dev_layout
    raise FileNotFoundError(
        "找不到游戏 index.html。请把游戏文件放到项目的 game/ 目录，"
        "或设置环境变量 IRONCROWNS_GAME_DIR 指向游戏目录。"
    )

_SHUTDOWN = "_shutdown"   # 内部命令：关闭工作线程


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """只允许访问游戏入口页 + 屏蔽访问日志。

    游戏是自包含单文件（无外部资源引用），白名单不会影响运行，
    同时避免把同目录的 .env（真实 API Key）与 .git 暴露到回环 HTTP。
    """

    _ALLOWED = ("/", "/index.html")

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def translate_path(self, path: str) -> str:
        if path.split("?", 1)[0] not in self._ALLOWED:
            return str(Path(self.directory) / "__forbidden__")
        return super().translate_path(path)


class GameBridge:
    """无头浏览器中的游戏实例（线程安全门面）。

    用法::

        bridge = GameBridge().start(seed=20260901, player_id=0)   # 任何环境均可
        state = bridge.get_state()
        result = bridge.act("raiseArmy", {"provId": 29})
        bridge.close()
    """

    def __init__(
        self,
        game_dir: Optional[Path | str] = None,
        headless: bool = True,
        viewport: Tuple[int, int] = (1280, 800),
        timeout_ms: int = 20000,
    ) -> None:
        self.game_dir = Path(game_dir) if game_dir else _default_game_dir()
        self.headless = headless
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self.timeout_ms = timeout_ms
        self.url: Optional[str] = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._started = False

    # ---------- 门面（线程安全） ----------

    def _submit(self, method: str, *args: Any, timeout: Optional[int] = None) -> Any:
        """把命令送入工作线程并阻塞等待结果。"""
        if self._closed and method != "start":
            raise RuntimeError("bridge 已关闭")
        fut: Future = Future()
        self._queue.put((method, args, fut))
        return fut.result(timeout=timeout or (self.timeout_ms + 60000))

    def start(self, seed: Optional[int] = None, player_id: int = 0, url: Optional[str] = None) -> "GameBridge":
        """启动工作线程并加载游戏；seed 非 None 时立即开新局。"""
        if self._started:
            return self
        self._thread = threading.Thread(target=self._worker_main, name="game-bridge", daemon=True)
        self._thread.start()
        self._submit("start", {"seed": seed, "player_id": player_id, "url": url})
        self._started = True
        return self

    def close(self) -> None:
        """关闭浏览器与本地服务，结束工作线程。"""
        if not self._started or self._closed:
            return
        try:
            self._submit(_SHUTDOWN, timeout=30000)
        except Exception:
            pass
        self._closed = True
        self._started = False
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "GameBridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 游戏交互 ----------

    def get_state(self) -> Dict[str, Any]:
        """完整局势快照（纯 JSON 数据，无地图几何）。"""
        return self._submit("get_state")

    def act(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """执行一个游戏动作，返回 {ok, msg, lastLogId, turn, pendingEvent}。"""
        return self._submit("act", action, params or {})

    def describe_actions(self) -> list:
        """游戏动作目录（智能体工具据此生成描述，永远与游戏同步）。"""
        return self._submit("describe_actions")

    def screenshot(self, path: Path | str) -> str:
        """截取当前画面（演示产物），返回保存路径。"""
        return self._submit("screenshot", str(path))

    def eval(self, js: str, *args: Any):
        """调试逃生舱：直接在页面执行任意 JS 表达式。"""
        return self._submit("eval", js, args)

    # ---------- 工作线程 ----------

    def _worker_main(self) -> None:
        """工作线程主循环：持有全部 Playwright 对象，顺序处理命令。"""
        httpd = None
        pw = None
        browser = None
        page = None

        def teardown() -> None:
            nonlocal httpd, pw, browser, page
            for closer in (
                lambda: browser and browser.close(),
                lambda: pw and pw.stop(),
                lambda: httpd and httpd.shutdown(),
            ):
                try:
                    closer()
                except Exception:
                    pass
            httpd = pw = browser = page = None

        while True:
            method, args, fut = self._queue.get()
            if method == _SHUTDOWN:
                teardown()
                fut.set_result(True)
                return
            try:
                if method == "start":
                    kwargs = args[0] if args else {}
                    httpd, pw, browser, page, url = self._do_start(**kwargs)
                    self.url = url
                    fut.set_result(None)
                else:
                    if page is None:
                        raise RuntimeError("bridge 尚未启动（先调用 start()）")
                    fut.set_result(self._do_call(page, method, args))
            except Exception as e:
                fut.set_exception(e)

    def _do_start(self, seed=None, player_id=0, url=None):
        """在工作线程内：起静态服务 + 无头 Edge + 加载游戏。"""
        if not (self.game_dir / "index.html").exists():
            raise FileNotFoundError(f"游戏目录下找不到 index.html：{self.game_dir}")

        httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            lambda *a, **k: _QuietHandler(*a, directory=str(self.game_dir), **k),
        )
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        # ?agent=1：游戏启动即暂停（消除旧存档自动推进的竞态）
        page_url = url or f"http://127.0.0.1:{port}/index.html?agent=1"

        pw = sync_playwright().start()
        browser = pw.chromium.launch(channel="msedge", headless=self.headless)
        page = browser.new_page(viewport=self.viewport)
        page.set_default_timeout(self.timeout_ms)
        page.on("pageerror", lambda e: print(f"[游戏页面错误] {e}"))
        page.goto(page_url)
        page.wait_for_function("window.GameAgent !== undefined", timeout=self.timeout_ms)

        if seed is not None:
            r = page.evaluate("([a, p]) => GameAgent.act(a, p)", ["newGame", {"seed": int(seed), "playerId": int(player_id)}])
            if not r.get("ok"):
                raise RuntimeError(f"newGame 失败：{r}")
        return httpd, pw, browser, page, page_url

    def _do_call(self, page, method: str, args: tuple):
        if method == "get_state":
            return page.evaluate("() => GameAgent.snapshot()")
        if method == "act":
            action, params = args
            return page.evaluate("([a, p]) => GameAgent.act(a, p)", [action, params])
        if method == "describe_actions":
            return page.evaluate("() => GameAgent.describeActions()")
        if method == "screenshot":
            (path,) = args
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=path)
            return path
        if method == "eval":
            js, js_args = args
            return page.evaluate(js, *js_args)
        raise ValueError(f"未知桥命令: {method}")
