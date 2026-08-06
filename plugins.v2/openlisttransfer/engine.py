from __future__ import annotations

import posixpath
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import OpenListClient, OpenListError

SUCCESS = {"succeeded", "success", "completed", "done", "6"}
FAILURE = {"failed", "canceled", "cancelled", "errored", "7", "8"}


def normalize_path(path: str) -> str:
    return posixpath.normpath("/" + (path or "").strip().strip("/"))


def parse_mappings(raw: str) -> List[Tuple[str, str]]:
    result = []
    for number, line in enumerate((raw or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "=>" if "=>" in line else "→" if "→" in line else None
        if not separator:
            raise ValueError(f"第 {number} 行映射格式错误，应为 /源目录 => /目标目录")
        source, target = [normalize_path(value) for value in line.split(separator, 1)]
        if source == target or target.startswith(source + "/"):
            raise ValueError(f"第 {number} 行目标目录不能等于或位于源目录内部")
        if any(source == old or source.startswith(old + "/") or old.startswith(source + "/") for old, _ in result):
            raise ValueError(f"第 {number} 行源目录与其他映射重复或互相包含")
        result.append((source, target))
    return result


def comparable_hashes(source: Dict[str, Any], target: Dict[str, Any]) -> Tuple[bool, bool]:
    def extract(item: Dict[str, Any]) -> Dict[str, str]:
        raw = item.get("hash_info") or item.get("hashinfo") or {}
        return {str(k).lower(): str(v).lower() for k, v in raw.items() if v} if isinstance(raw, dict) else {}
    left, right = extract(source), extract(target)
    common = set(left) & set(right)
    return (bool(common), bool(common) and all(left[key] == right[key] for key in common))


@dataclass
class Item:
    source: str
    target: str
    name: str
    size: int
    mapping: str
    status: str = "等待"
    progress: float = 0.0
    speed: float = 0.0
    eta_seconds: Optional[int] = None
    task_id: str = ""
    error: str = ""
    transferred_bytes: int = 0


class TransferEngine:
    def __init__(self, client: OpenListClient, mappings: List[Tuple[str, str]], *, delete_source: bool, strict_hash: bool, dry_run: bool, stable_minutes: int, poll_seconds: int, save_state: Callable[[dict], None], logger: Any) -> None:
        self.client, self.mappings = client, mappings
        self.delete_source, self.strict_hash, self.dry_run = delete_source, strict_hash, dry_run
        self.stable_seconds, self.poll_seconds = max(0, stable_minutes) * 60, max(2, poll_seconds)
        self.save_state, self.logger = save_state, logger
        self.lock, self.stop_event = threading.RLock(), threading.Event()
        self.running, self.items = False, []
        self.summary = self.empty_summary()

    @staticmethod
    def empty_summary() -> Dict[str, Any]:
        return {"running": False, "message": "尚未运行", "total_files": 0, "total_bytes": 0, "completed_files": 0, "completed_bytes": 0, "deleted_files": 0, "retained_files": 0, "failed_files": 0, "speed": 0.0, "eta_seconds": None, "started_at": "", "finished_at": ""}

    def snapshot(self) -> dict:
        with self.lock:
            return {"summary": dict(self.summary), "items": [asdict(item) for item in self.items[-200:]]}

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        with self.lock:
            if self.running:
                return
            self.running, self.stop_event = True, threading.Event()
            self.items, self.summary = [], self.empty_summary()
            self.summary.update(running=True, started_at=datetime.now().astimezone().isoformat(timespec="seconds"), message="正在扫描源目录")
            self._persist()
        try:
            self.client.authenticate()
            self.items = self._queue()
            self.summary.update(total_files=len(self.items), total_bytes=sum(item.size for item in self.items), message="正在传输" if self.items else "没有需要处理的稳定文件")
            self._persist()
            for item in self.items:
                if self.stop_event.is_set():
                    break
                self._process(item)
            if self.delete_source and not self.dry_run:
                for source, _ in self.mappings:
                    self._clean_empty(source)
        except Exception as exc:
            self.logger.error(f"OpenList 转移异常: {exc}", exc_info=True)
            self.summary["message"] = f"运行异常: {exc}"
        finally:
            with self.lock:
                self.running, self.summary["running"] = False, False
                self.summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                self.summary["message"] = "任务已停止" if self.stop_event.is_set() else ("处理完成，部分文件失败或被保留" if self.summary["failed_files"] else "处理完成")
                self._persist()

    def _persist(self) -> None:
        self.save_state(self.snapshot())

    def _queue(self) -> List[Item]:
        now = datetime.now(timezone.utc).timestamp()
        result = []
        for source_root, target_root in self.mappings:
            for path, meta in self._walk(source_root):
                modified = self._timestamp(meta.get("modified"))
                if modified and now - modified < self.stable_seconds:
                    continue
                relative = posixpath.relpath(path, source_root)
                result.append(Item(path, normalize_path(posixpath.join(target_root, relative)), posixpath.basename(path), int(meta.get("size") or 0), f"{source_root} => {target_root}"))
        return result

    def _walk(self, root: str):
        stack = [root]
        while stack:
            current = stack.pop()
            for entry in self.client.list(current):
                name = entry.get("name")
                if not name:
                    continue
                path = normalize_path(posixpath.join(current, name))
                if entry.get("is_dir"):
                    stack.append(path)
                else:
                    yield path, entry

    @staticmethod
    def _timestamp(value: Any) -> Optional[float]:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if value else None
        except ValueError:
            return None

    def _process(self, item: Item) -> None:
        try:
            source = self.client.get(item.source)
            target = self._optional(item.target)
            if target:
                self._assert_verified(source, target)
                item.status, item.progress, item.transferred_bytes = "目标已存在且校验通过", 100.0, item.size
            elif self.dry_run:
                item.status, item.progress = "试运行：将复制", 100.0
            else:
                self._ensure_dir(posixpath.dirname(item.target))
                tasks = self.client.copy(posixpath.dirname(item.source), posixpath.dirname(item.target), item.name)
                if not tasks or not tasks[0].get("id"):
                    raise OpenListError("OpenList 未返回复制任务 ID")
                item.task_id = str(tasks[0]["id"])
                self._wait(item)
                self._assert_verified(source, self.client.get(item.target))
                item.status = "校验通过"
            if self.delete_source and not self.dry_run:
                self._assert_verified(self.client.get(item.source), self.client.get(item.target))
                self.client.remove(posixpath.dirname(item.source), item.name)
                item.status = "已校验并删除源文件"
                self.summary["deleted_files"] += 1
            else:
                self.summary["retained_files"] += 1
            self.summary["completed_files"] += 1
            self.summary["completed_bytes"] += item.size
        except Exception as exc:
            item.status, item.error = "失败，源文件已保留", str(exc)
            self.summary["failed_files"] += 1
            self.summary["retained_files"] += 1
            self.logger.error(f"处理失败 {item.source}: {exc}")
        finally:
            self._update_eta()
            self._persist()

    def _wait(self, item: Item) -> None:
        previous_bytes, previous_time = 0, time.monotonic()
        while not self.stop_event.wait(self.poll_seconds):
            task = self.client.copy_task(item.task_id)
            progress = max(0.0, min(100.0, float(task.get("progress") or 0)))
            total = int(task.get("total_bytes") or item.size or 0)
            transferred, now = int(total * progress / 100), time.monotonic()
            elapsed = max(0.001, now - previous_time)
            instant = max(0.0, (transferred - previous_bytes) / elapsed)
            item.speed = instant if not item.speed else item.speed * .65 + instant * .35
            item.progress, item.transferred_bytes = progress, transferred
            item.eta_seconds = int((total - transferred) / item.speed) if item.speed > 0 else None
            item.status = task.get("status") or "传输中"
            self._update_eta()
            self._persist()
            state = str(task.get("state", "")).lower()
            if state in SUCCESS or progress >= 100:
                item.progress, item.transferred_bytes = 100.0, total or item.size
                return
            if state in FAILURE or task.get("error"):
                raise OpenListError(task.get("error") or f"复制任务失败: {state}")
            previous_bytes, previous_time = transferred, now
        raise OpenListError("插件已停止")

    def _assert_verified(self, source: Dict[str, Any], target: Dict[str, Any]) -> None:
        if int(source.get("size") or 0) != int(target.get("size") or 0):
            raise OpenListError("文件大小不一致")
        common, equal = comparable_hashes(source, target)
        if common and not equal:
            raise OpenListError("文件哈希不一致")
        if self.strict_hash and not common:
            raise OpenListError("两端没有可比较的同类型哈希")

    def _optional(self, path: str):
        try:
            return self.client.get(path)
        except OpenListError as exc:
            if "not found" in str(exc).lower() or "不存在" in str(exc):
                return None
            raise

    def _ensure_dir(self, path: str) -> None:
        current = "/"
        for part in path.strip("/").split("/"):
            if part:
                current = normalize_path(posixpath.join(current, part))
                self.client.mkdir(current)

    def _clean_empty(self, root: str) -> None:
        dirs, stack = [], [root]
        while stack:
            current = stack.pop()
            dirs.append(current)
            for entry in self.client.list(current):
                if entry.get("is_dir") and entry.get("name"):
                    stack.append(normalize_path(posixpath.join(current, entry["name"])))
        for directory in sorted(dirs, key=lambda value: value.count("/"), reverse=True):
            if directory != root and not self.client.list(directory):
                self.client.remove(posixpath.dirname(directory), posixpath.basename(directory))

    def _update_eta(self) -> None:
        speed = sum(item.speed for item in self.items if 0 < item.progress < 100)
        remaining = sum(max(0, item.size - item.transferred_bytes) for item in self.items if item.progress < 100)
        self.summary["speed"] = speed
        self.summary["eta_seconds"] = int(remaining / speed) if speed > 0 else None
