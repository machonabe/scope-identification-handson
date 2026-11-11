# Databricks notebook source
# MAGIC %md
# MAGIC # 05 ⑩ 対象確定 → ⑪ 帳票出力（約 8 分）
# MAGIC
# MAGIC | 工程 | やること |
# MAGIC |---|---|
# MAGIC | ⑩ 対象確定 | ⑤⑥ の印と ④ の窓を 1 つにまとめ、概算VIN を 1 台ずつ **上から順に判定**し、最初に当たった理由で ●／× を確定する |
# MAGIC | ⑪ 帳票出力 | 確定結果を **回答用リスト（Excel）** にまとめる。並び順は 年式 → 型式 → 打刻No |
# MAGIC
# MAGIC 判定の順番（上ほど強い）：**事実系の除外**（廃車 → イベント車・非輸出 → 終了証未発行）＞ **条件系の除外**（対策適応済み → 対象期間外）

# COMMAND ----------

# MAGIC %pip install -q openpyxl

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# MAGIC %md
# MAGIC ## ⑩ 対象確定

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE VIEW v_step10_decision AS
# MAGIC WITH judged AS (
# MAGIC   SELECT a.vin, a.model_year, a.production_serial, a.model_code, a.series_name, a.production_facility_code, a.production_date, a.production_time,
# MAGIC          a.country_name, a.region_name, a.export_country_code,
# MAGIC          CASE
# MAGIC            WHEN m.is_scrapped                         THEN '廃車'
# MAGIC            WHEN m.is_event AND NOT m.event_exported   THEN 'イベント車（非輸出）'
# MAGIC            WHEN m.cert_not_issued                     THEN '完成検査終了証 未発行'
# MAGIC            WHEN c.is_cm_applied                       THEN '対策適応済み'
# MAGIC            WHEN a.production_date < coalesce(w.window_from, DATE'1900-01-01')
# MAGIC              OR a.production_date > coalesce(w.window_to, DATE'9999-12-31') THEN '対象期間外'
# MAGIC          END AS exclusion_reason,
# MAGIC          concat_ws(' / ',
# MAGIC            CASE WHEN m.is_event AND m.event_exported THEN 'イベント車・輸出済み（除外しない）' END,
# MAGIC            CASE WHEN c.same_day_kept THEN '初物エフ日の当日に製造（安全側で対象）' END,
# MAGIC            n.note) AS notes
# MAGIC   FROM v_step3_approx_vin a
# MAGIC   JOIN v_step4_window w USING (part_family)
# MAGIC   JOIN v_step5_master_flags m USING (vin)
# MAGIC   JOIN v_step6_countermeasure c USING (vin)
# MAGIC   LEFT JOIN cond_notes n ON n.country_code = coalesce(nullif(a.export_country_code, ''), 'JP')
# MAGIC )
# MAGIC SELECT *, CASE WHEN exclusion_reason IS NULL THEN '●' ELSE '×' END AS judgement FROM judged;
# MAGIC
# MAGIC SELECT count(*) AS `概算VIN`, count_if(judgement = '●') AS `対象`, count_if(judgement = '×') AS `対象外` FROM v_step10_decision

# COMMAND ----------

# MAGIC %md
# MAGIC ### 対象外の理由の内訳
# MAGIC 1 台に理由は 1 つだけ（最初に当たったもの）なので、**対象 ＋ 対象外 ＝ 概算VIN** がぴったり合います。

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT coalesce(exclusion_reason, '（対象）') AS `理由`, judgement AS `判定`, count(*) AS `台数`
# MAGIC FROM v_step10_decision GROUP BY ALL ORDER BY `判定` DESC, `台数` DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ### 確定結果を版として保存する
# MAGIC 実行するたびに版（v1, v2, …）を増やして残します。条件を直して再実行したときに、前の版と比べられます。

# COMMAND ----------

from pyspark.sql import functions as F

REQUEST_NO = spark.table("case_registry").first()["request_no"]
prev = spark.sql("SELECT max(version) AS v FROM scope_result").first()["v"] if spark.catalog.tableExists("scope_result") else None
VERSION = (prev or 0) + 1
result = (spark.table("v_step10_decision")
          .withColumn("request_no", F.lit(REQUEST_NO)).withColumn("version", F.lit(VERSION))
          .withColumn("run_at", F.current_timestamp()))
result.write.mode("append").option("mergeSchema", "true").saveAsTable("scope_result")
summary = spark.sql(f"""SELECT count(*) AS n_approx, count_if(judgement = '●') AS n_target, count_if(judgement = '×') AS n_excluded
                        FROM scope_result WHERE version = {VERSION}""").first()
print(f"{REQUEST_NO} v{VERSION}: 概算VIN {summary.n_approx:,} 台 ＝ 対象 {summary.n_target:,} 台 ＋ 対象外 {summary.n_excluded:,} 台")

# COMMAND ----------

# MAGIC %md
# MAGIC ## ⑪ 帳票出力（回答用リスト）
# MAGIC 表紙（型式別内訳）と対象範囲リストの 2 シートの Excel を、ボリュームの `outputs` に出します。

# COMMAND ----------

import io

import pandas as pd

rows = spark.sql(f"""
  SELECT model_year AS `年式`, production_serial AS `打刻No`, vin AS VIN, model_code AS `型式`, series_name AS `車種`,
         date_format(production_date, 'yyyy-MM-dd') AS `製造日`, country_name AS `仕向け`, judgement AS `判定`, coalesce(exclusion_reason, '—') AS `対象外理由`, notes AS `注記`
  FROM scope_result WHERE version = {VERSION}
  ORDER BY `年式`, `型式`, `打刻No`""").toPandas()
cover = spark.sql(f"""
  SELECT model_code AS `型式`, series_name AS `車種`, count(*) AS `概算VIN`,
         count_if(judgement = '●') AS `対象`, count_if(judgement = '×') AS `対象外`,
         count_if(exclusion_reason = '対策適応済み') AS `うち対策適応済み`, count_if(exclusion_reason = '対象期間外') AS `うち対象期間外`,
         count_if(exclusion_reason IN ('廃車', 'イベント車（非輸出）', '完成検査終了証 未発行')) AS `うちマスタ除外`
  FROM scope_result WHERE version = {VERSION} GROUP BY ALL ORDER BY `型式`""").toPandas()
case = pd.DataFrame([("依頼番号", REQUEST_NO), ("版", f"v{VERSION}"), ("概算VIN", summary.n_approx),
                     ("対象", summary.n_target), ("対象外", summary.n_excluded)], columns=["項目", "値"])

out_name = f"回答リスト_{REQUEST_NO}_v{VERSION}.xlsx"
buf = io.BytesIO()                      # メモリ上で Excel を作ってから、ボリュームに書き込む
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    case.to_excel(xw, sheet_name="表紙", index=False)
    cover.to_excel(xw, sheet_name="表紙", index=False, startrow=len(case) + 2)
    rows.to_excel(xw, sheet_name="対象範囲リスト", index=False)
with open(f"{OUTPUT_DIR}/{out_name}", "wb") as f:
    f.write(buf.getvalue())
spark.sql(f"UPDATE case_registry SET status = '完了' WHERE request_no = '{REQUEST_NO}'")
print(f"回答用リストを出力しました: {OUTPUT_DIR}/{out_name}")
print(f"ダウンロード：左の「カタログ」→ {CATALOG} → {SCHEMA} → ボリューム {VOLUME} → outputs")
display(cover)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 地域 × 型式の対象台数
# MAGIC 結果の表の「＋」→「可視化」で棒グラフにすると、会議でそのまま見せられます。

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT region_name AS `地域`, model_code AS `型式`, count_if(judgement = '●') AS `対象`, count_if(judgement = '×') AS `対象外`
# MAGIC FROM scope_result WHERE version = (SELECT max(version) FROM scope_result)
# MAGIC GROUP BY ALL ORDER BY `地域`, `型式`

# COMMAND ----------

# MAGIC %md
# MAGIC ## 確認ポイント

# COMMAND ----------

check(summary.n_target + summary.n_excluded == summary.n_approx, "対象 ＋ 対象外 ＝ 概算VIN")
check(os.path.exists(f"{OUTPUT_DIR}/{out_name}"), f"回答用リスト {out_name} を出力")
check(spark.table("case_registry").first()["status"] == "完了", f"案件 {REQUEST_NO} を「完了」にした")
