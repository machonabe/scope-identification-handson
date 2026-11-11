# Databricks notebook source
# MAGIC %md
# MAGIC # 03 ③ 号機特定（概算VIN）→ ④ 対象期間特定（約 8 分）
# MAGIC
# MAGIC | 工程 | やること |
# MAGIC |---|---|
# MAGIC | ③ 号機特定 | 部品 → 仕様コード → 車歴 と たどり、型式・年式・仕向け国で絞る。**製造日は見ない**。これが調査範囲の上限＝**概算VIN** |
# MAGIC | ④ 対象期間特定 | 依頼書の対象期間を、**製造日の窓 [From, To]** に翻訳する。「対策適用まで」は初物台帳の初物エフ日に解決する |
# MAGIC
# MAGIC 各工程は **ビュー** として作ります。元のデータが増えても、ビューを読み直すだけで最新の結果になります（06 で使います）。

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# MAGIC %md
# MAGIC ## ③ 号機特定（概算VIN）
# MAGIC 概算VIN ＝ 部品 ＆ 型式 ＆ 年式 ＆ 仕向け国（製造日は見ない）。⑤〜⑩ はすべてこの概算VIN に対して判定します。

# COMMAND ----------

# MAGIC %sql
# MAGIC -- 対象部品が付く仕様コード（モデル・タイプ・オプション）。部番は上 8 桁（部品系統）で突き合わせる
# MAGIC CREATE OR REPLACE VIEW v_part_spec AS
# MAGIC SELECT DISTINCT m.model_year, m.model_code, m.spec_model_code, m.spec_type_code, m.spec_option_code, p.part_family
# MAGIC FROM parts_application m
# MAGIC JOIN cond_parts p ON left(regexp_replace(m.change_part_no, '[- ]', ''), 8) = p.part_family;
# MAGIC
# MAGIC -- 概算VIN：部品が付く車のうち、型式・年式・仕向け国が特定条件に合う車
# MAGIC CREATE OR REPLACE VIEW v_step3_approx_vin AS
# MAGIC SELECT f.*, pm.part_family, c.country_name, c.region_name
# MAGIC FROM vehicle_history f
# MAGIC JOIN v_part_spec pm USING (model_year, model_code, spec_model_code, spec_type_code, spec_option_code)
# MAGIC JOIN cond_countries c ON c.country_code = coalesce(nullif(f.export_country_code, ''), 'JP')
# MAGIC WHERE f.model_code IN (SELECT model_code FROM cond_models)
# MAGIC   AND f.model_year BETWEEN (SELECT model_year_from FROM cond_year) AND (SELECT model_year_to FROM cond_year);

# COMMAND ----------

# MAGIC %md
# MAGIC ### 絞り込みの流れ
# MAGIC 1 段ずつ台数が減っていく様子を確かめます。

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH part_cars AS (
# MAGIC   SELECT f.* FROM vehicle_history f JOIN v_part_spec USING (model_year, model_code, spec_model_code, spec_type_code, spec_option_code)
# MAGIC )
# MAGIC SELECT '1. 車歴すべて' AS `段階`, count(*) AS `台数` FROM vehicle_history
# MAGIC UNION ALL SELECT '2. 対象部品が付く車（部品適用情報の仕様コードと一致）', count(*) FROM part_cars
# MAGIC UNION ALL SELECT '3. ＋ 型式・年式が一致', count(*) FROM part_cars
# MAGIC   WHERE model_code IN (SELECT model_code FROM cond_models)
# MAGIC     AND model_year BETWEEN (SELECT model_year_from FROM cond_year) AND (SELECT model_year_to FROM cond_year)
# MAGIC UNION ALL SELECT '4. ＋ 仕向け国が一致 ＝ 概算VIN', count(*) FROM v_step3_approx_vin
# MAGIC ORDER BY `段階`

# COMMAND ----------

# MAGIC %md
# MAGIC ### 型式別の内訳と見本 VIN
# MAGIC XB3 は TB 系だけが対象部品なので、TC1 の車は 2 の段階で外れています。

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT model_code AS `型式`, series_name AS `車種`, model_year AS `年式`, count(*) AS `概算VIN`,
# MAGIC        concat_ws(', ', collect_set(spec_type_code)) AS `仕様タイプ`, min(vin) AS `見本VIN`
# MAGIC FROM v_step3_approx_vin
# MAGIC GROUP BY ALL ORDER BY ALL

# COMMAND ----------

# MAGIC %md
# MAGIC ## ④ 対象期間特定（製造日の窓）
# MAGIC
# MAGIC | 端点の書き方 | 窓の決め方 |
# MAGIC |---|---|
# MAGIC | 年月日（○年○月○日） | その日で切る |
# MAGIC | 番号系（対策適用・設変No など） | 初物台帳から **初物エフ日** を引き、その日で切る |
# MAGIC | 指定なし（生産初号機など） | 切らない |

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE VIEW v_step4_window AS
# MAGIC SELECT p.part_family,
# MAGIC        f.kind AS from_kind,
# MAGIC        CASE f.kind WHEN '年月日' THEN to_date(f.value_date) WHEN '番号系（対策適用）' THEN l.first_article_date END AS window_from,
# MAGIC        t.kind AS to_kind,
# MAGIC        CASE t.kind WHEN '年月日' THEN to_date(t.value_date) WHEN '番号系（対策適用）' THEN l.first_article_date END AS window_to,
# MAGIC        l.first_article_no, l.first_article_vin
# MAGIC FROM cond_parts p
# MAGIC CROSS JOIN (SELECT * FROM cond_period WHERE endpoint = 'From') f
# MAGIC CROSS JOIN (SELECT * FROM cond_period WHERE endpoint = 'To') t
# MAGIC LEFT JOIN first_article_ledger l ON left(l.target_part_no, 8) = p.part_family;
# MAGIC
# MAGIC SELECT part_family AS `部品系統`, from_kind AS `From種別`, window_from AS `From`, to_kind AS `To種別`, window_to AS `To`,
# MAGIC        first_article_no AS `初物管理No`, coalesce(first_article_vin, '（VIN 不明・日付のみ）') AS `初物VIN`
# MAGIC FROM v_step4_window ORDER BY part_family

# COMMAND ----------

# MAGIC %md
# MAGIC ### 概算VIN のうち、対象期間の窓に入る車
# MAGIC 概算VIN − 窓に入る車 ＝ **製造期間で外れた車**。窓は ⑩ 対象確定で使います。

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT a.model_code AS `型式`, count(*) AS `概算VIN`,
# MAGIC        count_if(a.production_date BETWEEN coalesce(w.window_from, DATE'1900-01-01') AND coalesce(w.window_to, DATE'9999-12-31')) AS `窓に入る車`,
# MAGIC        count_if(NOT a.production_date BETWEEN coalesce(w.window_from, DATE'1900-01-01') AND coalesce(w.window_to, DATE'9999-12-31')) AS `製造期間で外れた車`
# MAGIC FROM v_step3_approx_vin a JOIN v_step4_window w USING (part_family)
# MAGIC GROUP BY ALL ORDER BY ALL

# COMMAND ----------

# MAGIC %md
# MAGIC ## 確認ポイント

# COMMAND ----------

n_approx = spark.table("v_step3_approx_vin").count()
n_window = spark.table("v_step4_window").where("window_to IS NOT NULL").count()
check(n_approx > 0, f"概算VIN {n_approx:,} 台")
check(n_window == spark.table("cond_parts").count(), "すべての部品系統で、対象期間の To（初物エフ日）を解決")
