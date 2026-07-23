# -*- coding: utf-8 -*-
"""Macy选品·AI精判类目映射（2026-07-23）。
把 macy_cat_map 里 decided_by='sniff' 的供应商类目，逐个让gpt-5.2判到Macy 77个叶子
类目之一（或判无匹配）。人工锁定(locked=1)的跳过。批量、断点续跑。
"""
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

BATCH = 25   # 一次让AI判25个供应商类目（省调用次数）


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.services.assistant_service import _openai_client
        from app.services.listing_sentinel_service import _ensure_openai_key
        _ensure_openai_key(app.config.get("BASE_DIR", app.root_path))

        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT brand, leaf, full_path FROM order_system.macy_leaf_category
                               WHERE active=1 ORDER BY brand, leaf""")
                leaves = cur.fetchall()
                cur.execute("""SELECT id, supplier, supplier_cat, product_count
                               FROM order_system.macy_cat_map
                               WHERE decided_by='sniff' AND locked=0
                               ORDER BY product_count DESC""")
                todo = cur.fetchall()
        finally:
            conn.close()

        leaf_list = [f"{r['brand']} | {r['leaf']} | {r['full_path']}" for r in leaves]
        leaf_names = {r["leaf"] for r in leaves}
        leaf_brand = {r["leaf"]: r["brand"] for r in leaves}
        print(f"待判类目:{len(todo)} 目标Macy叶子:{len(leaves)}")

        client = _openai_client()
        MODEL = "gpt-5.2"
        done = 0
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            cat_lines = "\n".join(f"{n}. [{c['supplier']}] {c['supplier_cat']}"
                                  for n, c in enumerate(chunk))
            prompt = f"""你是Macy平台选品分类专家。下面是一批"供应商类目"，请判断每个能不能归到Macy可上架的叶子类目。

【Macy可上的叶子类目】(格式: 品牌 | 叶子类目 | 完整路径)
{chr(10).join(leaf_list)}

【待判供应商类目】
{cat_lines}

规则：
1. 每个供应商类目，选**唯一最合适**的一个Macy叶子类目；判不出/不属于任何一个→leaf填null
2. 只看类目名判断产品本质是否同类（如供应商"Bar Stools"→Macy"Bar Stools"）；宁缺勿滥，不确定就null
3. leaf必须是上面列表里**原样**的叶子类目名
只输出JSON: {{"results":[{{"i":序号,"leaf":"叶子类目名或null","reason":"简短中文理由"}}]}}"""
            try:
                resp = client.chat.completions.create(
                    model=MODEL, response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": "Output valid JSON only."},
                              {"role": "user", "content": prompt}])
                res = json.loads(resp.choices[0].message.content).get("results", [])
            except Exception as exc:
                print(f"  batch {i} failed: {exc}")
                continue

            conn = DBManager.get_connection()
            try:
                with conn.cursor() as cur:
                    for item in res:
                        n = item.get("i")
                        if n is None or n >= len(chunk):
                            continue
                        c = chunk[n]
                        leaf = item.get("leaf")
                        if leaf and leaf in leaf_names:
                            cur.execute("""UPDATE order_system.macy_cat_map
                                SET macy_leaf=%s, macy_brand=%s, decided_by='ai',
                                    ai_reason=%s WHERE id=%s AND locked=0""",
                                (leaf, leaf_brand[leaf], (item.get("reason") or "")[:400], c["id"]))
                        else:
                            cur.execute("""UPDATE order_system.macy_cat_map
                                SET macy_leaf=NULL, decided_by='ai',
                                    ai_reason=%s WHERE id=%s AND locked=0""",
                                ((item.get("reason") or "无匹配")[:400], c["id"]))
                conn.commit()
            finally:
                conn.close()
            done += len(chunk)
            print(f"  判完 {done}/{len(todo)}", flush=True)

        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT
                    SUM(macy_leaf IS NOT NULL) AS matched,
                    SUM(decided_by='ai' AND macy_leaf IS NULL) AS ai_nomatch,
                    SUM(decided_by='prefilter') AS prefiltered
                    FROM order_system.macy_cat_map""")
                print("最终:", cur.fetchone())
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
