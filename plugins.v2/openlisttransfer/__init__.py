from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

try:
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError:  # pragma: no cover - only used by standalone syntax checks
    class IntervalTrigger:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

try:
    from app.log import logger
    from app.plugins import _PluginBase
except ImportError:  # pragma: no cover - MoviePilot supplies these at runtime
    class _FallbackLogger:
        def error(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
    class _PluginBase:
        def save_data(self, *args, **kwargs): pass
        def get_data(self, *args, **kwargs): return None
    logger = _FallbackLogger()

from .client import OpenListClient
from .engine import TransferEngine, parse_mappings


class OpenListTransfer(_PluginBase):
    plugin_name = "OpenList 安全转移"
    plugin_desc = "通过 OpenList 服务端复制文件，严格校验后按需清理源文件。"
    plugin_icon = "https://raw.githubusercontent.com/OpenListTeam/Logo/main/logo.svg"
    plugin_version = "1.0.0"
    plugin_author = "vincent-jia253"
    author_url = "https://github.com/vincent-jia253"
    plugin_config_prefix = "openlisttransfer_"
    plugin_order, auth_level = 30, 1
    _enabled, _engine, _worker = False, None, None
    _config: Dict[str, Any] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._config = config or {}
        self._enabled = bool(self._config.get("enabled"))
        if not self._enabled:
            return
        try:
            mappings = parse_mappings(self._config.get("mappings", ""))
            if not mappings:
                raise ValueError("至少配置一组目录映射")
            client = OpenListClient(self._config.get("base_url", ""), self._config.get("token", ""), self._config.get("username", ""), self._config.get("password", ""), int(self._config.get("request_timeout", 30)), bool(self._config.get("verify_ssl", True)))
            self._engine = TransferEngine(client, mappings, delete_source=bool(self._config.get("delete_source", False)), strict_hash=bool(self._config.get("strict_hash", True)), dry_run=bool(self._config.get("dry_run", True)), stable_minutes=int(self._config.get("stable_minutes", 10)), poll_seconds=int(self._config.get("poll_seconds", 5)), save_state=lambda value: self.save_data("runtime_state", value), logger=logger)
        except Exception as exc:
            logger.error(f"OpenList 安全转移初始化失败: {exc}")
            self._enabled = False

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._engine:
            return []
        return [{"id": f"{self.__class__.__name__}.Transfer", "name": "OpenList 安全转移定时任务", "trigger": IntervalTrigger(minutes=max(1, int(self._config.get("interval_minutes", 10)))), "func": self.start_transfer, "kwargs": {}}]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{"path": "/status", "endpoint": self.api_status, "methods": ["GET"], "auth": "bear", "summary": "获取转移状态"}, {"path": "/run", "endpoint": self.api_run, "methods": ["POST"], "auth": "bear", "summary": "立即执行转移"}, {"path": "/stop", "endpoint": self.api_stop, "methods": ["POST"], "auth": "bear", "summary": "停止转移"}]

    def api_status(self) -> dict:
        return self._engine.snapshot() if self._engine else {"summary": {"running": False, "message": "插件未启用"}, "items": []}

    def api_run(self) -> dict:
        return {"success": self.start_transfer(), "message": "任务已启动或已有任务运行中"}

    def api_stop(self) -> dict:
        if self._engine:
            self._engine.stop()
        return {"success": True, "message": "已请求停止"}

    def start_transfer(self) -> bool:
        if not self._engine or self._engine.running:
            return False
        self._worker = threading.Thread(target=self._engine.run, name="OpenListTransfer", daemon=True)
        self._worker.start()
        return True

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{"component": "VForm", "content": [
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
            {"component": "VSwitch", "props": {"model": "delete_source", "label": "校验后删除源文件"}},
            {"component": "VSwitch", "props": {"model": "strict_hash", "label": "严格哈希校验"}},
            {"component": "VSwitch", "props": {"model": "dry_run", "label": "试运行（建议首次开启）"}},
            {"component": "VTextField", "props": {"model": "base_url", "label": "OpenList 地址", "placeholder": "https://openlist.example.com"}},
            {"component": "VSwitch", "props": {"model": "verify_ssl", "label": "校验 HTTPS 证书"}},
            {"component": "VTextField", "props": {"model": "token", "label": "OpenList 令牌", "type": "password"}},
            {"component": "VTextField", "props": {"model": "username", "label": "OpenList 用户名"}},
            {"component": "VTextField", "props": {"model": "password", "label": "OpenList 密码", "type": "password"}},
            {"component": "VTextarea", "props": {"model": "mappings", "label": "目录映射（每行一组）", "placeholder": "/本地/电影 => /云盘/电影\n/本地/电视剧 => /云盘/电视剧", "rows": 5}},
            {"component": "VTextField", "props": {"model": "interval_minutes", "label": "同步间隔（分钟）", "type": "number", "min": 1}},
            {"component": "VTextField", "props": {"model": "stable_minutes", "label": "文件稳定时间（分钟）", "type": "number", "min": 0}},
            {"component": "VTextField", "props": {"model": "poll_seconds", "label": "进度刷新（秒）", "type": "number", "min": 2}},
            {"component": "VTextField", "props": {"model": "request_timeout", "label": "接口超时（秒）", "type": "number", "min": 5}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "严格模式下没有可比较哈希时不会删除源文件。"}},
        ]}], {"enabled": False, "delete_source": False, "strict_hash": True, "dry_run": True, "verify_ssl": True, "base_url": "", "token": "", "username": "", "password": "", "mappings": "", "interval_minutes": 10, "stable_minutes": 10, "poll_seconds": 5, "request_timeout": 30}

    def get_page(self) -> List[dict]:
        return self._page(self.api_status(), full=True)

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        return [{"key": "status", "name": "传输状态"}]

    def get_dashboard(self, key: str = "status", **kwargs):
        return {"cols": 12}, {"title": "OpenList 安全转移", "refresh": 5, "border": True}, self._page(self.api_status(), full=False)

    def _page(self, data: dict, full: bool) -> List[dict]:
        summary, items = data.get("summary", {}), data.get("items", [])
        active = next((item for item in items if float(item.get("progress", 0)) < 100), items[-1] if items else {})
        def metric(label: str, value: str) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "div", "props": {"class": "text-caption"}, "text": label}, {"component": "div", "props": {"class": "text-subtitle-2"}, "text": value}]}
        page = [{"component": "VRow", "content": [metric("状态", str(summary.get("message", "尚未运行"))), metric("当前速度", f"{self._bytes(summary.get('speed', 0))}/s"), metric("总剩余", self._duration(summary.get("eta_seconds"))), metric("文件进度", f"{summary.get('completed_files', 0)} / {summary.get('total_files', 0)}")]}]
        if active:
            page += [{"component": "VListItem", "props": {"title": active.get("name", ""), "subtitle": f"{active.get('source', '')} → {active.get('target', '')}"}}, {"component": "VProgressLinear", "props": {"modelValue": float(active.get("progress", 0)), "height": 10}}, {"component": "VRow", "content": [metric("文件状态", active.get("status", "")), metric("进度", f"{float(active.get('progress', 0)):.1f}%"), metric("速度", f"{self._bytes(active.get('speed', 0))}/s"), metric("预计剩余", self._duration(active.get("eta_seconds")))]}]
        if full and items:
            page.append({"component": "VTable", "props": {"density": "compact"}, "content": [{"component": "tbody", "content": [{"component": "tr", "content": [{"component": "td", "text": i.get("source", "")}, {"component": "td", "text": i.get("status", "")}, {"component": "td", "text": f"{float(i.get('progress', 0)):.1f}%"}, {"component": "td", "text": i.get("error", "")}]} for i in items[-50:]]}]})
        return page

    @staticmethod
    def _bytes(value: Any) -> str:
        value = float(value or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return "0 B"

    @staticmethod
    def _duration(value: Any) -> str:
        if value is None:
            return "计算中"
        seconds = max(0, int(value)); hours, seconds = divmod(seconds, 3600); minutes, seconds = divmod(seconds, 60)
        return f"{hours}小时{minutes}分" if hours else f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"

    def stop_service(self):
        if self._engine:
            self._engine.stop()
        self._engine, self._worker = None, None
