# -*- coding: utf-8 -*-
"""ERP AI助理（2026-07-17上线，用户需求：右下角对话框，答系统规则/字段/公式/操作）。

设计要点（都是成本和防胡说的关键）：
  * 知识库模块化：docs/assistant_kb/ 下按主题拆文件 + manifest.json 关键词索引，
    每次只把命中的前几个模块喂给模型（几千token），不整库塞（几万token消耗不起）
  * 数字纪律：Phase 1 不接数据库查询，具体数字一律指路对应页面，模型不许编数字
  * 模型 gpt-5.2（用户2026-07-17定），走哨兵同一条 Brightdata 美国代理 + key
  * 用量控制：assistant_chat_log 逐条记 token，每日请求数封顶
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Tuple

from app.models.db_manager import DBManager

MODEL_NAME = "gpt-5.2"
MAX_MODULES = 3          # 每次最多喂几个知识模块
MAX_HISTORY = 8          # 带最近几条对话历史
DAILY_REQUEST_CAP = 300  # 每日提问上限（防失控，3个运营远用不完）
MAX_QUESTION_LEN = 2000

_KB_CACHE: Dict[str, Any] = {"mtime": None, "manifest": None, "texts": {}}


def _kb_dir() -> str:
    from flask import current_app
    base = current_app.config.get("BASE_DIR", current_app.root_path)
    return os.path.join(base, "docs", "assistant_kb")


def _load_kb() -> Tuple[List[Dict], Dict[str, str]]:
    """载入 manifest + 模块正文，按 manifest.json 的 mtime 缓存。"""
    path = os.path.join(_kb_dir(), "manifest.json")
    mtime = os.path.getmtime(path)
    if _KB_CACHE["mtime"] != mtime:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)["modules"]
        texts = {}
        for m in manifest:
            fp = os.path.join(_kb_dir(), m["file"])
            with open(fp, encoding="utf-8") as f:
                texts[m["id"]] = f.read()
        _KB_CACHE.update({"mtime": mtime, "manifest": manifest, "texts": texts})
    return _KB_CACHE["manifest"], _KB_CACHE["texts"]


def select_modules(question: str) -> List[str]:
    """关键词打分选模块：命中多的在前，最多 MAX_MODULES 个；全不命中给默认组合。"""
    manifest, _ = _load_kb()
    q = (question or "").lower()
    scored = []
    for m in manifest:
        score = sum(1 for kw in m["keywords"] if kw.lower() in q)
        if m["title"].lower() in q:
            score += 2
        if score > 0:
            scored.append((score, m["id"]))
    scored.sort(key=lambda x: -x[0])
    if scored:
        return [mid for _, mid in scored[:MAX_MODULES]]
    # 没命中关键词：默认给字段字典+公式（最常被问）
    return ["field_dict", "formulas"]


SYSTEM_PROMPT = """你是AutoWeb电商ERP系统的内置助理，服务对象是电商运营（管理Lowes/Macy等平台店铺）。
规则：
1. 只依据下面提供的【系统资料】回答。资料里没有的，直说"资料里没有这条"，并猜测可能在哪个页面能看到。
2. 严禁编造任何具体数字（销量/金额/利润率等实时数据）。被问到数字时，指路对应页面或飞书看板。
3. 用简体中文，口语化、简洁，像同事之间讲话。公式用文字+算式写清楚。
4. 涉及不可逆操作（删offer、推价、下架）时，提醒确认要点。
5. 回答保持在300字以内，除非用户要求详细展开。"""


def _openai_client():
    # 复用哨兵的代理与key加载（香港服务器直连OpenAI会403，必须走美国代理）
    from flask import current_app
    from app.services.listing_sentinel_service import _ensure_openai_key, _OPENAI_PROXY
    import httpx
    from openai import OpenAI
    _ensure_openai_key(current_app.config.get("BASE_DIR", current_app.root_path))
    try:
        http_client = httpx.Client(proxy=_OPENAI_PROXY, timeout=120)
    except TypeError:
        http_client = httpx.Client(proxies=_OPENAI_PROXY, timeout=120)
    return OpenAI(http_client=http_client)


def _ensure_log_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_system.assistant_chat_log (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            question TEXT,
            reply_chars INT,
            modules VARCHAR(255),
            model VARCHAR(32),
            prompt_tokens INT,
            completion_tokens INT,
            page VARCHAR(255),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_created (created_at)
        ) CHARSET=utf8mb4""")


def _today_count(cursor) -> int:
    cursor.execute("""SELECT COUNT(*) AS n FROM order_system.assistant_chat_log
                      WHERE created_at >= CURDATE()""")
    return int((cursor.fetchone() or {}).get("n") or 0)


def chat(question: str, history: List[Dict], page: str = "") -> Dict[str, Any]:
    """一次问答。history=[{role:'user'|'assistant', content:str}]（前端带最近几条）。"""
    question = (question or "").strip()[:MAX_QUESTION_LEN]
    if not question:
        return {"success": False, "msg": "问题为空"}

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            _ensure_log_table(cur)
            if _today_count(cur) >= DAILY_REQUEST_CAP:
                return {"success": False,
                        "msg": f"今日提问已达上限（{DAILY_REQUEST_CAP}条），明天再来"}
        conn.commit()
    finally:
        conn.close()

    module_ids = select_modules(question)
    manifest, texts = _load_kb()
    titles = {m["id"]: m["title"] for m in manifest}
    kb_text = "\n\n".join(
        f"=== 系统资料：{titles[mid]} ===\n{texts[mid]}" for mid in module_ids)
    toc = "、".join(m["title"] for m in manifest)

    messages = [{"role": "system", "content": SYSTEM_PROMPT
                 + f"\n\n系统资料共有这些主题：{toc}。本次已按问题挑选相关部分给你。"}]
    messages.append({"role": "system", "content": kb_text})
    for h in (history or [])[-MAX_HISTORY:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": str(h["content"])[:2000]})
    ctx = f"（用户当前在页面：{page}）\n" if page else ""
    messages.append({"role": "user", "content": ctx + question})

    client = _openai_client()
    resp = client.chat.completions.create(model=MODEL_NAME, messages=messages)
    reply = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    pt = getattr(usage, "prompt_tokens", None) if usage else None
    ct = getattr(usage, "completion_tokens", None) if usage else None

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_system.assistant_chat_log
                    (question, reply_chars, modules, model,
                     prompt_tokens, completion_tokens, page)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (question[:2000], len(reply), ",".join(module_ids), MODEL_NAME,
                 pt, ct, (page or "")[:255]))
        conn.commit()
    finally:
        conn.close()

    return {"success": True, "reply": reply, "modules": module_ids,
            "prompt_tokens": pt, "completion_tokens": ct}
