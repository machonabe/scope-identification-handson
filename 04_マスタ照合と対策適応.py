# Databricks notebook source
# MAGIC %md
# MAGIC # 04 ⑤ マスタ照合 → ⑥ 対策適応判定（約 7 分）
# MAGIC
# MAGIC | 工程 | やること | ポイント |
# MAGIC |---|---|---|
# MAGIC | ⑤ マスタ照合 | 概算VIN 1 台ずつに、車歴に無い情報（イベント車・廃車・終了証）を突き合わせて **印を付ける** | 台数は減らさない。広い概算VIN で照合する |
# MAGIC | ⑥ 対策適応判定 | 対策品が使われ始めた境界（初物）以降に作った車に **対策適応済み** の印を付ける | 時刻が分かれば時刻で、日付だけなら **翌日以降**（当日は対象に残す＝安全側） |
# MAGIC
# MAGIC どちらも「印を付ける」だけです。対象か対象外かは ⑩ でまとめて決めます。

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# MAGIC %md
# MAGIC ## ⑤ マスタ照合

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE VIEW v_step5_master_flags AS
# MAGIC SELECT a.vin,
# MAGIC        s.vin IS NOT NULL                                           AS is_scrapped,       -- 廃車（予定含む）
# MAGIC        e.vin IS NOT NULL                                           AS is_event,          -- イベント車
# MAGIC        coalesce(e.exported, false)                                 AS event_exported,    -- 輸出済みのイベント車は除外しない
# MAGIC        (a.export_country_code = '' AND a.certificate_no = '-------') AS cert_not_issued,  -- 国内で完成検査終了証が未発行
# MAGIC        e.event, e.destination AS event_destination
# MAGIC FROM v_step3_approx_vin a
# MAGIC LEFT JOIN master_scrapped s USING (vin)
# MAGIC LEFT JOIN master_event_vehicles e USING (vin);
# MAGIC
# MAGIC SELECT count(*)                                    AS `概算VIN`,
# MAGIC        count_if(is_scrapped)                       AS `廃車`,
# MAGIC        count_if(is_event AND NOT event_exported)   AS `イベント車_非輸出`,
# MAGIC        count_if(is_event AND event_exported)       AS `イベント車_輸出済み_注記のみ`,
# MAGIC        count_if(cert_not_issued)                   AS `終了証_未発行`
# MAGIC FROM v_step5_master_flags

# COMMAND ----------

# MAGIC %md
# MAGIC 印が付いた車の一覧です。輸出済みのイベント車は **注記だけ**（対象外にしない）なのがポイントです。

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT a.vin AS VIN, a.model_code AS `型式`, a.country_name AS `仕向け`,
# MAGIC        CASE WHEN m.is_scrapped THEN '× 廃車'
# MAGIC             WHEN m.is_event AND NOT m.event_exported THEN concat('× イベント車（', m.event, '）')
# MAGIC             WHEN m.is_event AND m.event_exported THEN concat('注 イベント車・輸出済み（', m.event_destination, '）')
# MAGIC             WHEN m.cert_not_issued THEN '× 完成検査終了証 未発行' END AS `印`
# MAGIC FROM v_step5_master_flags m JOIN v_step3_approx_vin a USING (vin)
# MAGIC WHERE m.is_scrapped OR m.is_event OR m.cert_not_issued
# MAGIC ORDER BY `印`, VIN

# COMMAND ----------

# MAGIC %md
# MAGIC ## ⑥ 対策適応判定
# MAGIC
# MAGIC | 初物台帳で分かること | 対策適応済み（対象外）にする車 |
# MAGIC |---|---|
# MAGIC | 初物 VIN が分かる → 製造時刻まで分かる | 初物 VIN の **製造時刻以降** に作った車 |
# MAGIC | 日付だけ分かる | **翌日以降** に作った車（当日は何台目から対策品か不明なので対象に残す） |
# MAGIC | 初物の記録なし | 除外しない |

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE VIEW v_step6_countermeasure AS
# MAGIC WITH boundary AS (
# MAGIC   SELECT left(l.target_part_no, 8) AS part_family, l.first_article_no, l.first_article_vin, l.first_article_date,
# MAGIC          to_timestamp(concat(cast(f.production_date AS STRING), ' ', f.production_time), 'yyyy-MM-dd HHmmss') AS first_article_ts
# MAGIC   FROM first_article_ledger l
# MAGIC   LEFT JOIN vehicle_history f ON f.vin = l.first_article_vin
# MAGIC ), cars AS (
# MAGIC   SELECT vin, part_family, production_date,
# MAGIC          to_timestamp(concat(cast(production_date AS STRING), ' ', production_time), 'yyyy-MM-dd HHmmss') AS production_ts
# MAGIC   FROM v_step3_approx_vin
# MAGIC )
# MAGIC SELECT c.vin, c.part_family, c.production_ts, b.first_article_no,
# MAGIC        CASE WHEN b.first_article_ts IS NOT NULL THEN '時刻で判定'
# MAGIC             WHEN b.first_article_date IS NOT NULL THEN '日付のみ（翌日以降を対象外）'
# MAGIC             ELSE '初物の記録なし' END AS rule,
# MAGIC        coalesce(date_format(b.first_article_ts, 'yyyy-MM-dd HH:mm:ss'), cast(b.first_article_date AS STRING)) AS boundary,
# MAGIC        CASE WHEN b.first_article_ts IS NOT NULL THEN c.production_ts >= b.first_article_ts
# MAGIC             WHEN b.first_article_date IS NOT NULL THEN c.production_date > b.first_article_date
# MAGIC             ELSE false END AS is_cm_applied,
# MAGIC        coalesce(b.first_article_ts IS NULL AND c.production_date = b.first_article_date, false) AS same_day_kept
# MAGIC FROM cars c LEFT JOIN boundary b USING (part_family);
# MAGIC
# MAGIC SELECT part_family AS `部品系統`, rule AS `判定方法`, boundary AS `境界`,
# MAGIC        count(*) AS `概算VIN`, count_if(is_cm_applied) AS `対策適応済み`, count_if(same_day_kept) AS `当日製造で対象に残した車`
# MAGIC FROM v_step6_countermeasure
# MAGIC GROUP BY ALL ORDER BY ALL

# COMMAND ----------

# MAGIC %md
# MAGIC ### 境界の前後を見る（XA1：時刻で判定）
# MAGIC 初物 VIN の製造時刻を境に、印が切り替わります。

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH x AS (
# MAGIC   SELECT c.*, a.production_serial, row_number() OVER (ORDER BY abs(unix_timestamp(c.production_ts) - unix_timestamp(to_timestamp(c.boundary)))) AS dist
# MAGIC   FROM v_step6_countermeasure c JOIN v_step3_approx_vin a USING (vin)
# MAGIC   WHERE c.part_family = 'SG100XA1'
# MAGIC )
# MAGIC SELECT vin AS VIN, production_serial AS `打刻No`, production_ts AS `製造日時`, boundary AS `境界`,
# MAGIC        CASE WHEN is_cm_applied THEN '× 対策適応済み' ELSE '● 対策前' END AS `印`
# MAGIC FROM x WHERE dist <= 8 ORDER BY production_ts

# COMMAND ----------

# MAGIC %md
# MAGIC ## 確認ポイント

# COMMAND ----------

flags = spark.sql("SELECT count_if(is_scrapped OR is_event OR cert_not_issued) AS n FROM v_step5_master_flags").first()["n"]
cm_rows = spark.sql("SELECT count_if(is_cm_applied) AS applied, count_if(same_day_kept) AS kept FROM v_step6_countermeasure").first()
check(spark.table("v_step5_master_flags").count() == spark.table("v_step3_approx_vin").count(), "マスタ照合は台数を減らさず、概算VIN 全台に印を付けた")
check(flags > 0, f"マスタの印が付いた車 {flags} 台")
check(cm_rows["applied"] > 0, f"対策適応済み {cm_rows['applied']:,} 台（日付のみの境界で当日に残した車 {cm_rows['kept']} 台）")
