# Databricks notebook source
# MAGIC %md
# MAGIC # 02 ① 依頼書取込 → ② 条件自動生成（約 7 分）
# MAGIC
# MAGIC | 工程 | やること | 従来の手作業 |
# MAGIC |---|---|---|
# MAGIC | ① 依頼書取込 | 他部門の調査依頼書（Excel）を**そのまま忠実に**読み取り、案件として登録する | 依頼書を受け取り内容を確認する |
# MAGIC | ② 条件自動生成 | 依頼書の記載を、システムが検索できる **特定条件** に翻訳する。あいまいな記載は **要確認** にする | 担当者が特定条件を作る |

# COMMAND ----------

# MAGIC %pip install -q openpyxl

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# MAGIC %md
# MAGIC ## ① 依頼書取込
# MAGIC B 列の項目名と C 列の値を読み、「５．対象となる不具合部品番号」の表と「部番情報 別紙」も読みます。

# COMMAND ----------

from datetime import date, datetime

import pandas as pd
from openpyxl import load_workbook

wb = load_workbook(REQUEST_PATH, data_only=True)
ws = wb["依頼書"]

fields, parts, in_parts = {}, [], False
for b, c, d, e in ws.iter_rows(min_col=2, max_col=5, values_only=True):
    if b is None:
        continue
    if str(b).startswith("５．"):
        in_parts = True
        continue
    if in_parts:
        if b != "No":
            parts.append({"no": int(b), "part_no_text": c, "part_name": d, "applicability": e, "source": "依頼書"})
    elif c is not None:
        fields[str(b)] = c.date().isoformat() if isinstance(c, datetime) else str(c)

side = wb["部番情報 別紙"] if "部番情報 別紙" in wb.sheetnames else None
if side is not None:
    for no, part_no, name, appl in side.iter_rows(min_row=2, values_only=True):
        parts.append({"no": int(no), "part_no_text": part_no, "part_name": name, "applicability": appl, "source": "別紙"})

REQUEST_NO = fields["依頼No"]
header = pd.DataFrame([{"request_no": REQUEST_NO, "item": k, "value": v} for k, v in fields.items()])
spark.createDataFrame(header).write.mode("overwrite").saveAsTable("req_header")
spark.createDataFrame(pd.DataFrame(parts).assign(request_no=REQUEST_NO)).write.mode("overwrite").saveAsTable("req_parts")
spark.sql(f"""CREATE OR REPLACE TABLE case_registry AS
  SELECT '{REQUEST_NO}' AS request_no, '{fields['件名']}' AS defect_name, current_timestamp() AS registered_at, '取込済み' AS status""")

print(f"案件として登録しました: {REQUEST_NO}（{fields['件名']}）／ 項目 {len(fields)} 件・部番 {len(parts)} 行")
display(header)

# COMMAND ----------

# MAGIC %md
# MAGIC ## ② 条件自動生成（特定条件）
# MAGIC
# MAGIC | 依頼書の記載 | 翻訳先 | 使う工程 |
# MAGIC |---|---|---|
# MAGIC | 年式「24M ～ 25M」 | 年式 2024〜2025 | ③ |
# MAGIC | 対象型式・対象国名 | 型式の一覧・仕向け国の一覧（地域名は国に展開） | ③ |
# MAGIC | 部番「SG100-XA1/XA2」 | 部品系統（上 8 桁）→ 部品適用情報で車にたどる | ③ |
# MAGIC | 対象期間「2023年11月1日 ～ 対策適用まで」 | From＝日付、To＝対策（初物台帳で日付に解決） | ④ ⑥ |
# MAGIC | 特記事項（自由記述） | 条件にはせず **要確認** の注記として残す | ⑤ ⑩ |

# COMMAND ----------

import re

items, warnings = [], []


def add(item, raw, value, used_in, ok=True, message=""):
    items.append({"request_no": REQUEST_NO, "item": item, "raw_text": raw, "value": value, "used_in": used_in,
                  "status": "OK" if ok else "要確認", "message": message})


# 年式：「24M ～ 25M」「2024～2025」
raw = fields.get("年式", "")
m = re.search(r"(\d{2,4})M?\s*[～~〜\-]\s*(\d{2,4})M?", raw)
to_year = lambda s: int(s) + 2000 if len(s) == 2 else int(s)
if m:
    year_from, year_to = to_year(m.group(1)), to_year(m.group(2))
    add("年式", raw, f"{year_from}〜{year_to}", "③")
else:
    year_from = year_to = None
    add("年式", raw, "", "③", ok=False, message="年式が読み取れません")

# 対象型式：区切り文字（、 , / 空白）で分けて、車歴にある型式か確かめる
raw = fields.get("対象型式", "")
models = [t for t in re.split(r"[、,，／/\s]+", raw) if t]
known_models = {r[0] for r in spark.sql("SELECT DISTINCT model_code FROM vehicle_history").collect()}
unknown = [t for t in models if t not in known_models]
add("対象型式", raw, ", ".join(models), "③", ok=not unknown, message=f"車歴に無い型式: {unknown}" if unknown else "")

# 対象国名：地域名（北中米・欧州・ア大…）は国に展開する
raw = fields.get("対象国名", "")
cm = spark.table("country_master").toPandas()
ALIASES = {"アジア大洋州": "ア大", "北米": "北中米", "中近東": "中東アフリカ"}
countries, unknown = [], []
for token in [t.strip() for t in re.split(r"[、,，]", raw) if t.strip()]:
    token = ALIASES.get(token, token)
    hit = cm[(cm.region_name == token) | (cm.country_name == token)]
    if len(hit):
        countries.extend(hit.country_code)
    else:
        unknown.append(token)
add("対象国名", raw, ", ".join(countries), "③", ok=not unknown, message=f"国・地域として読めない記載: {unknown}" if unknown else "")

# 対象期間：From と To を、日付・番号系（対策適用など）・指定なしに分ける
raw = fields.get("対象期間", "")
period = []
for side_name, text in zip(["From", "To"], [s.strip() for s in re.split(r"[～~〜]", raw)] + [""]):
    d = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if d:
        kind, value = "年月日", date(int(d.group(1)), int(d.group(2)), int(d.group(3))).isoformat()
    elif text in ("", "生産初号機", "指定なし"):
        kind, value = "指定なし", None
    elif re.search(r"対策|設変|初物|適用|No", text):
        kind, value = "番号系（対策適用）", None
    else:
        kind, value = "不明", None
    period.append({"endpoint": side_name, "kind": kind, "value_date": value, "raw_text": text})
    add(f"対象期間 {side_name}", text, f"{kind}" + (f"（{value}）" if value else ""), "④ ⑥",
        ok=kind != "不明", message="対象期間の書き方が読み取れません" if kind == "不明" else "")

# 部番：「SG100-XA1/XA2」のような省略は分けて、上 8 桁（部品系統）にそろえる
part_rows = []
for p in parts:
    text = str(p["part_no_text"]).replace(" ", "")
    head, _, tail = text.partition("-")
    variants = [f"{head}-{v}" for v in tail.split("/")] if "/" in tail else [text]
    for v in variants:
        family = re.sub(r"[-\s]", "", v)[:8]
        part_rows.append({"part_family": family, "part_text": v, "source": p["source"]})
    if len(variants) > 1:
        add("部番", text, ", ".join(variants), "③", ok=False, message="「/」区切りの省略を分けて読みました。分け方が正しいか確認してください")
known_families = {r[0] for r in spark.sql("SELECT DISTINCT left(regexp_replace(change_part_no, '[- ]', ''), 8) FROM parts_application").collect()}
families = sorted({r["part_family"] for r in part_rows})
missing = [f for f in families if f not in known_families]
add("部品系統（上8桁）", "依頼書＋別紙", ", ".join(families), "③", ok=not missing, message=f"部品適用情報に無い部品系統: {missing}" if missing else "")

# 特記事項：自由記述は条件にせず、人が確認する注記として残す（国名が書いてあれば、その国の車に注記を付ける）
notes = fields.get("特記事項", "")
note_rows = [{"country_code": r.country_code, "note": f"特記事項：{notes}"} for r in cm.itertuples() if r.country_name in notes]
if notes:
    add("特記事項", notes, f"注記として残す（{len(note_rows)} か国の車に注記）", "⑩", ok=False, message="自由記述です。条件にするかは担当者が判断します")
spark.createDataFrame(note_rows, "country_code STRING, note STRING").write.mode("overwrite").saveAsTable("cond_notes")

# 保存（後の工程は SQL でこれらのテーブルを使います）
spark.createDataFrame(pd.DataFrame(items)).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable("cond_items")
spark.createDataFrame(pd.DataFrame({"model_code": models})).write.mode("overwrite").saveAsTable("cond_models")
spark.createDataFrame(cm[cm.country_code.isin(countries)]).write.mode("overwrite").saveAsTable("cond_countries")
spark.createDataFrame(pd.DataFrame(part_rows).drop_duplicates("part_family")).write.mode("overwrite").saveAsTable("cond_parts")
spark.createDataFrame(pd.DataFrame(period)).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable("cond_period")
spark.createDataFrame(pd.DataFrame([{"model_year_from": year_from, "model_year_to": year_to}])).write.mode("overwrite").saveAsTable("cond_year")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 特定条件の一覧（要確認の行を確認してください）

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT item AS `項目`, raw_text AS `依頼書の記載`, value AS `特定条件`, used_in AS `使う工程`, status AS `状態`, message AS `確認してほしいこと`
# MAGIC FROM cond_items
# MAGIC ORDER BY status DESC, item

# COMMAND ----------

# MAGIC %md
# MAGIC ### （任意）AI に依頼書を読ませてみる
# MAGIC 同じ依頼書を生成 AI に読ませ、上のルールの結果と見比べます。モデルが使えないワークスペースでは、メッセージを出して飛ばします。

# COMMAND ----------

prompt = ("次の調査依頼書から、対象の年式（西暦の範囲）、型式の一覧、仕向け国・地域の一覧、対象期間の開始と終了、"
          "部番の一覧を JSON で抜き出してください。読み取れない項目は null にしてください。\n\n"
          + "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n部番: " + ", ".join(str(p["part_no_text"]) for p in parts))
try:
    model = re.sub(r"[^A-Za-z0-9._-]", "", CFG["ai_model"])
    answer = spark.sql(f"SELECT ai_query('{model}', :prompt) AS a", args={"prompt": prompt}).first()["a"]
    print(answer)
except Exception as e:
    print(f"AI は使えなかったので飛ばします（{CFG['ai_model']}）: {str(e).splitlines()[0][:200]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 確認ポイント

# COMMAND ----------

n_warn = sum(1 for i in items if i["status"] == "要確認")
check(spark.table("case_registry").count() == 1, f"案件 {REQUEST_NO} を登録")
check(year_from is not None and len(models) > 0 and len(countries) > 0 and len(families) > 0, "年式・型式・仕向け国・部品系統を特定条件に翻訳")
print(f"⚠️ 要確認 {n_warn} 件（上の表）。今回はこのまま次へ進みます。")
