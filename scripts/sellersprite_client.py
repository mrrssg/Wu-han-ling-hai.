# -*- coding: utf-8 -*-
"""SellerSprite 直连客户端(服务器用,不经 Claude 的 MCP)。

走 SellerSprite 的 HTTP MCP 端点(Streamable HTTP, 实测无状态单次 POST 即可)。
secret-key 从 instance/sellersprite_key.txt 读(不入 git),或环境变量 SS_KEY。
目前只封装 google_trend(蓝海季节用)。
"""
import json
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
_ENDPOINT = "https://mcp.sellersprite.com/mcp"
_HEADERS = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}


def _key() -> str:
    import os
    k = os.environ.get("SS_KEY", "").strip()
    if k:
        return k
    f = _ROOT / "instance" / "sellersprite_key.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    raise RuntimeError("缺少 SellerSprite secret-key(instance/sellersprite_key.txt 或 SS_KEY)")


def _parse(resp) -> dict:
    """响应可能是 application/json 或 SSE(data: 行)。"""
    if "text/event-stream" in resp.headers.get("Content-Type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    continue
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def _call(tool: str, arguments: dict, timeout: int = 60) -> dict:
    url = f"{_ENDPOINT}?secret-key={_key()}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    r = requests.post(url, headers=_HEADERS, json=body, timeout=timeout)
    r.raise_for_status()
    data = _parse(r)
    content = (data.get("result") or {}).get("content") or []
    for c in content:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:
                return {}
    return {}


def google_trend(keyword: str, marketplace: str = "US", monthly: bool = True) -> list:
    """返回 [{'time':ms,'value':int}, ...]，失败/无数据返回 []。"""
    if not keyword:
        return []
    try:
        payload = _call("google_trend", {"request": {
            "keyword": keyword, "marketplace": marketplace, "monthly": monthly}})
        return (payload.get("data") or {}).get("items") or []
    except Exception:
        return []
