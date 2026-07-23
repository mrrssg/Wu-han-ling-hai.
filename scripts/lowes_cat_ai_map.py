# -*- coding: utf-8 -*-
"""Lowes 选品·AI 两步类目映射（2026-07-23）。

Lowes 有 2836 个叶子路径，塞不进一个 prompt，故两步：
  Stage A: 供应商类目 → 42 个一级之一(或 null=不上)。decided_by 'sniff' → 'ai_l1'
  Stage B: 在选中的一级下（≤246 个叶子），选唯一最合适的叶子路径。'ai_l1' → 'ai'
人工锁定(locked=1)的跳过。断点续跑：Stage A 处理剩余 'sniff'，Stage B 处理 'ai_l1'。

用法: python scripts/lowes_cat_ai_map.py [costway|vevor] [--stage a|b]
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

BATCH = 30
MODEL = "gpt-5.2"


def _run_stage_a(client, sup_filter):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT l1 FROM order_system.lowes_leaf_category "
                        "WHERE active=1 AND l1<>'' ORDER BY l1")
            l1s = [r["l1"] for r in cur.fetchall()]
            q = ("SELECT id, supplier, supplier_cat, product_count FROM order_system.lowes_cat_map "
                 "WHERE decided_by='sniff' AND locked=0")
            p = []
            if sup_filter:
                q += " AND supplier=%s"; p.append(sup_filter)
            q += " ORDER BY product_count DESC"
            cur.execute(q, p)
            todo = cur.fetchall()
    finally:
        conn.close()
    l1set = set(l1s)
    print(f"[A] 待判一级:{len(todo)} 目标一级:{len(l1s)}")
    l1_lines = "\n".join(l1s)
    done = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        cat_lines = "\n".join(f"{n}. [{c['supplier']}] {c['supplier_cat']}"
                              for n, c in enumerate(chunk))
        prompt = f"""你是Lowes平台选品分类专家。给一批"供应商类目"，判断每个最应归到 Lowes 哪个"一级类目"。

【Lowes 一级类目】(共{len(l1s)}个)
{l1_lines}

【Lowes 分类习惯（重要，避免归错一级）】
- **几乎所有室内家具都归到 Home Decor**（其下有完整 Furniture 子树：客厅/卧室/餐厅厨房/办公/儿童家具）。
  例：Dining Chairs/Dining Tables/餐椅餐桌、Bar Stools吧凳、Nightstands、Dressers、TV Stands/Entertainment Centers电视柜、
  File Cabinets文件柜、Sideboards餐边柜、Bookcases书架、Makeup Vanities梳妆台、Coffee/End Tables → 一律 **Home Decor**。
- **Dining & Entertaining** 只放餐具/杯盘/上菜/派对一次性用品，**不放餐桌餐椅**。
- **Office & School Supplies** 只放文具耗材，**办公家具(书桌/文件柜/办公椅)归 Home Decor**。
- **Electronics** 只放电子设备本身，**电视柜/影音柜归 Home Decor**。
- 户外家具/庭院 → Outdoors；工具箱/工具柜 → Tools；收纳箱/货架/衣物收纳 → Storage & Organization。

【待判供应商类目】
{cat_lines}

规则：
1. 每个供应商类目，选**唯一最合适**的一个 Lowes 一级类目；判不出/明显不属于任何一个→l1填null
2. 看产品本质属于哪个大类，并遵循上面的 Lowes 分类习惯（家具优先 Home Decor）
3. l1 必须是上面列表里**原样**的一级名
只输出JSON: {{"results":[{{"i":序号,"l1":"一级名或null"}}]}}"""
        try:
            resp = client.chat.completions.create(
                model=MODEL, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": "Output valid JSON only."},
                          {"role": "user", "content": prompt}])
            res = json.loads(resp.choices[0].message.content).get("results", [])
        except Exception as exc:
            print(f"  [A] batch {i} failed: {exc}"); continue
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for item in res:
                    n = item.get("i")
                    if n is None or n >= len(chunk):
                        continue
                    c = chunk[n]
                    l1 = item.get("l1")
                    if l1 and l1 in l1set:
                        cur.execute("""UPDATE order_system.lowes_cat_map
                            SET lowes_l1=%s, decided_by='ai_l1' WHERE id=%s AND locked=0""",
                            (l1, c["id"]))
                    else:
                        cur.execute("""UPDATE order_system.lowes_cat_map
                            SET lowes_l1=NULL, lowes_leaf=NULL, lowes_path=NULL,
                                decided_by='ai', ai_reason='无匹配一级' WHERE id=%s AND locked=0""",
                            (c["id"],))
            conn.commit()
        finally:
            conn.close()
        done += len(chunk)
        print(f"  [A] {done}/{len(todo)}", flush=True)


def _run_stage_b(client, sup_filter):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT l1, leaf, full_path FROM order_system.lowes_leaf_category
                           WHERE active=1 ORDER BY l1, full_path""")
            leaves_by_l1 = defaultdict(list)
            for r in cur.fetchall():
                leaves_by_l1[r["l1"]].append(r["full_path"])
            q = ("SELECT id, supplier, supplier_cat, lowes_l1 FROM order_system.lowes_cat_map "
                 "WHERE decided_by='ai_l1' AND lowes_l1 IS NOT NULL AND lowes_path IS NULL "
                 "AND locked=0")
            p = []
            if sup_filter:
                q += " AND supplier=%s"; p.append(sup_filter)
            cur.execute(q, p)
            todo = cur.fetchall()
    finally:
        conn.close()
    by_l1 = defaultdict(list)
    for c in todo:
        by_l1[c["lowes_l1"]].append(c)
    print(f"[B] 待判叶子:{len(todo)} 涉及一级:{len(by_l1)}")
    valid_paths = {p for ps in leaves_by_l1.values() for p in ps}
    done = 0
    for l1, cats in by_l1.items():
        paths = leaves_by_l1.get(l1, [])
        if not paths:
            continue
        path_lines = "\n".join(paths)
        for i in range(0, len(cats), BATCH):
            chunk = cats[i:i + BATCH]
            cat_lines = "\n".join(f"{n}. [{c['supplier']}] {c['supplier_cat']}"
                                  for n, c in enumerate(chunk))
            prompt = f"""你是Lowes平台选品分类专家。下面供应商类目都属于 Lowes 一级"{l1}"，请各选最合适的叶子类目(完整路径)。

【"{l1}" 下的叶子类目完整路径】
{path_lines}

【待判供应商类目】
{cat_lines}

规则：
1. 每个供应商类目，选**唯一最合适**的一个叶子完整路径；确实没有合适的→path填null
2. path 必须是上面列表里**原样**的完整路径
只输出JSON: {{"results":[{{"i":序号,"path":"完整路径或null","reason":"简短中文理由"}}]}}"""
            try:
                resp = client.chat.completions.create(
                    model=MODEL, response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": "Output valid JSON only."},
                              {"role": "user", "content": prompt}])
                res = json.loads(resp.choices[0].message.content).get("results", [])
            except Exception as exc:
                print(f"  [B] {l1} batch {i} failed: {exc}"); continue
            conn = DBManager.get_connection()
            try:
                with conn.cursor() as cur:
                    for item in res:
                        n = item.get("i")
                        if n is None or n >= len(chunk):
                            continue
                        c = chunk[n]
                        path = item.get("path")
                        if path and path in valid_paths:
                            leaf = path.split("/")[-1]
                            cur.execute("""UPDATE order_system.lowes_cat_map
                                SET lowes_leaf=%s, lowes_path=%s, decided_by='ai',
                                    ai_reason=%s WHERE id=%s AND locked=0""",
                                (leaf, path, (item.get("reason") or "")[:400], c["id"]))
                        else:
                            cur.execute("""UPDATE order_system.lowes_cat_map
                                SET lowes_leaf=NULL, lowes_path=NULL, decided_by='ai',
                                    ai_reason=%s WHERE id=%s AND locked=0""",
                                ((item.get("reason") or "该一级下无合适叶子")[:400], c["id"]))
                conn.commit()
            finally:
                conn.close()
            done += len(chunk)
            print(f"  [B] {done}/{len(todo)} ({l1})", flush=True)


def main() -> int:
    sup_filter = None
    only_stage = None
    args = [a for a in sys.argv[1:]]
    for i, a in enumerate(args):
        al = a.lower()
        if al in ("costway", "vevor"):
            sup_filter = al.capitalize()
        elif al == "--stage" and i + 1 < len(args):
            only_stage = args[i + 1].lower()

    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.services.assistant_service import _openai_client
        from app.services.listing_sentinel_service import _ensure_openai_key
        _ensure_openai_key(app.config.get("BASE_DIR", app.root_path))
        client = _openai_client()

        if only_stage in (None, "a"):
            _run_stage_a(client, sup_filter)
        if only_stage in (None, "b"):
            _run_stage_b(client, sup_filter)

        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT supplier,
                    SUM(lowes_path IS NOT NULL) matched,
                    SUM(decided_by='ai' AND lowes_path IS NULL) nomatch,
                    SUM(decided_by IN('sniff','ai_l1')) pending
                    FROM order_system.lowes_cat_map GROUP BY supplier""")
                for r in cur.fetchall():
                    print(f"最终 {r['supplier']}: 映射{r['matched']} 无匹配{r['nomatch']} 待处理{r['pending']}")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
