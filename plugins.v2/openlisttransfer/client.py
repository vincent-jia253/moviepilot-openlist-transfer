from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OpenListError(RuntimeError):
    pass


class OpenListClient:
    def __init__(self, base_url: str, token: str = "", username: str = "", password: str = "", timeout: int = 30, verify_ssl: bool = True) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.token, self.username, self.password = token.strip(), username.strip(), password
        self.timeout, self.verify_ssl = timeout, verify_ssl

    def authenticate(self) -> None:
        if self.token:
            return
        if not self.username or not self.password:
            raise OpenListError("未配置令牌或账号密码")
        data = self._request("POST", "/api/auth/login", json={"username": self.username, "password": self.password}, authenticated=False)
        self.token = (data or {}).get("token", "")
        if not self.token:
            raise OpenListError("OpenList 登录响应没有令牌")

    def _request(self, method: str, path: str, *, json: Optional[dict] = None, params: Optional[dict] = None, authenticated: bool = True) -> Any:
        if authenticated and not self.token:
            self.authenticate()
        url = urljoin(self.base_url, path.lstrip("/"))
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        body = __import__("json").dumps(json).encode("utf-8") if json is not None else None
        request = Request(url, data=body, method=method, headers={"Authorization": self.token, "Content-Type": "application/json"} if authenticated else {"Content-Type": "application/json"})
        context = ssl.create_default_context() if self.verify_ssl else ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                payload = __import__("json").loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, ValueError) as exc:
            raise OpenListError(f"OpenList 请求失败: {exc}") from exc
        if payload.get("code") != 200:
            raise OpenListError(payload.get("message") or f"OpenList 返回错误: {payload.get('code')}")
        return payload.get("data")

    def get(self, path: str) -> Dict[str, Any]:
        return self._request("POST", "/api/fs/get", json={"path": path}) or {}

    def list(self, path: str) -> List[Dict[str, Any]]:
        data = self._request("POST", "/api/fs/list", json={"path": path, "page": 1, "per_page": 0, "refresh": False}) or {}
        return data.get("content") or []

    def mkdir(self, path: str) -> None:
        try:
            self._request("POST", "/api/fs/mkdir", json={"path": path})
        except OpenListError as exc:
            if "exist" not in str(exc).lower() and "存在" not in str(exc):
                raise

    def copy(self, src_dir: str, dst_dir: str, name: str) -> List[Dict[str, Any]]:
        data = self._request("POST", "/api/fs/copy", json={"src_dir": src_dir, "dst_dir": dst_dir, "names": [name], "overwrite": False, "skip_existing": False, "merge": False}) or {}
        return data.get("tasks") or []

    def remove(self, parent: str, name: str) -> None:
        self._request("POST", "/api/fs/remove", json={"dir": parent, "names": [name]})

    def copy_task(self, task_id: str) -> Dict[str, Any]:
        return self._request("POST", "/api/task/copy/info", params={"tid": task_id}) or {}
