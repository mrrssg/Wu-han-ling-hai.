# -*- coding: utf-8 -*-
"""SellerSprite 直连客户端(服务器用,不经 Claude 的 MCP)。

走 SellerSprite 的 HTTP MCP 端点(Streamable HTTP, 实测无状态单次 POST 即可)。
secret-key 从 instance/sellersprite_key.txt 读(不入 git),或环境变量 SS_KEY。
目前只封装 google_trend(蓝海季节用)。
"""
import json
import time
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


def market_research(keyword: str, marketplace: str = "US") -> dict:
    """类目市场概况(Amazon,泛需求参考): 关键词 → 该类目节点聚合。
    返回 {units:月销量, revenue:月销售额, price:均价, return_rate:退货率%, node:类目路径}
    或 {}(无数据)。空结果退避重试一次。"""
    if not keyword:
        return {}
    for attempt in range(2):
        try:
            payload = _call("market_research", {"request": {
                "departmentKeyword": keyword, "marketplace": marketplace,
                "page": 1, "size": 1}})
            items = (payload.get("data") or {}).get("items") or []
            if items:
                it = items[0]
                return {"units": it.get("totalUnits"), "revenue": it.get("totalRevenue"),
                        "price": it.get("avgPrice"), "return_rate": it.get("returnRatio"),
                        "node": it.get("nodeLabelPathLocale") or it.get("nodeLabelPath")}
            if attempt == 1:
                return {}
        except Exception:
            if attempt == 1:
                return {}
        time.sleep(3)
    return {}


def google_trend(keyword: str, marketplace: str = "US", monthly: bool = True) -> list:
    """返回 [{'time':ms,'value':int}, ...]，失败/无数据返回 []。
    空结果退避重试一次(应对限流；真空关键词多花一次调用可接受)。"""
    if not keyword:
        return []
    for attempt in range(2):
        try:
            payload = _call("google_trend", {"request": {
                "keyword": keyword, "marketplace": marketplace, "monthly": monthly}})
            items = (payload.get("data") or {}).get("items") or []
            if items or attempt == 1:
                return items
        except Exception:
            if attempt == 1:
                return []
        time.sleep(3)      # 退避后重试
    return []
