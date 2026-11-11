# Databricks notebook source
# MAGIC %md
# MAGIC # 06（発展・持ち帰り）最新の生産実績で再判定する（約 5 分）
# MAGIC
# MAGIC 一括実行が使うのは「取り込んだ時点のデータ」です。取り込みが遅れると、**本当は対象の車がリストから漏れます**。
# MAGIC ここでは「工場 C の生産実績の連携が遅れていた」場面を作り、届いたデータを取り込んで再判定します。
# MAGIC
# MAGIC 1. 届いた生産実績（JSON）をボリュームの `landing` に置く
# MAGIC 2. **Auto Loader** で新しいファイルだけを車歴に追加する
# MAGIC 3. ③〜⑩ はビューなので、読み直すだけで最新の判定になる → 05 の版と比べる
# MAGIC 4. 05 をもう一度実行すると次の版が保存され、07 のアプリの画面にも反映される
# MAGIC
# MAGIC 本番では、生産実績を **Zerobus Ingest** で秒単位で直接テーブルに書き込めます（最後のセルを参照）。

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. 遅れて届いた生産実績を置く
# MAGIC XB3（TB 系）の 40 台。対策の境界より前に作られた車で、仕向けは対象の国です。

# COMMAND ----------

import json
import random
from datetime import datetime, timedelta

rnd = random.Random(7)
boundary = spark.sql("SELECT first_article_date FROM first_article_ledger WHERE target_part_no LIKE 'SG100XB3%'").first()[0]
specs = spark.sql("""SELECT DISTINCT spec_type_code, spec_option_code FROM parts_application
                   WHERE model_code = 'XB3' AND model_year = 2025 AND change_part_no LIKE 'SG100-XB3%'""").collect()
records = []
for i in range(40):
    ts = datetime.combine(boundary - timedelta(days=rnd.randint(10, 60)), datetime.min.time()) + timedelta(hours=8, seconds=rnd.randint(0, 40000))
    country = rnd.choice(["", "US", "DE", "AU"])
    t = rnd.choice(specs)
    records.append({
        "model_year": 2025, "series_code": "3020", "series_name": "SERIES B", "model_code": "XB3",
        "japan_type_approval": "6AA-XB3" if country == "" else "-----------", "vin": f"XB3-C{i + 1:06d}",
        "production_serial": f"C{i + 1:06d}", "production_facility_code": "C",
        "production_date": ts.date().isoformat(), "production_time": ts.strftime("%H%M%S"),
        "inspection_date": (ts.date() + timedelta(days=5)).isoformat(),
        "spec_model_code": "B30", "spec_type_code": t.spec_type_code, "spec_option_code": t.spec_option_code, "series_number": "297",
        "certificate_no": f"{rnd.randint(1, 9999999):07d}" if country == "" else "-------",
        "export_model_code": "" if country == "" else "XB385G", "export_type_code": "" if country == "" else "T1",
        "export_country_code": country,
    })
fn = f"{LANDING_DIR}/vehicle_history/plant_C_{datetime.now():%Y%m%d%H%M%S}.json"
os.makedirs(os.path.dirname(fn), exist_ok=True)
with open(fn, "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in records))
print(f"{len(records)} 台分の生産実績を置きました: {fn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Auto Loader で取り込む
# MAGIC `availableNow` は「今あるファイルのうち、まだ取り込んでいないものだけ」を処理して止まります。何度実行しても二重には入りません。

# COMMAND ----------

before = spark.table("vehicle_history").count()
q = (spark.readStream.format("cloudFiles")
     .option("cloudFiles.format", "json")
     .schema(spark.table("vehicle_history").schema)
     .load(f"{LANDING_DIR}/vehicle_history")
     .writeStream
     .option("checkpointLocation", f"{LANDING_DIR}/_checkpoints/vehicle_history")
     .trigger(availableNow=True)
     .toTable("vehicle_history"))
q.awaitTermination()
print(f"車歴: {before:,} 台 → {spark.table('vehicle_history').count():,} 台")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. 再判定して、05 で保存した版と比べる

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH last_saved AS (SELECT * FROM scope_result WHERE version = (SELECT max(version) FROM scope_result))
# MAGIC SELECT '05 で保存した版' AS `結果`, count(*) AS `概算VIN`, count_if(judgement = '●') AS `対象`, count_if(judgement = '×') AS `対象外` FROM last_saved
# MAGIC UNION ALL
# MAGIC SELECT '最新データで再判定', count(*), count_if(judgement = '●'), count_if(judgement = '×') FROM v_step10_decision

# COMMAND ----------

# MAGIC %md
# MAGIC 前の版に無かった、新しく対象になった車です。05 をもう一度実行すると、この結果が次の版として保存され、回答用リストも出し直せます。

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT d.vin AS VIN, d.model_code AS `型式`, d.production_date AS `製造日`, d.country_name AS `仕向け`, d.judgement AS `判定`
# MAGIC FROM v_step10_decision d
# MAGIC LEFT ANTI JOIN (SELECT vin FROM scope_result WHERE version = (SELECT max(version) FROM scope_result)) s USING (vin)
# MAGIC ORDER BY `製造日`

# COMMAND ----------

# MAGIC %md
# MAGIC ## 参考：本番で生産実績を秒単位で入れるには（Zerobus Ingest）
# MAGIC
# MAGIC 工場の設備やシステムから、**Zerobus Ingest** で Unity Catalog のテーブルへ直接書き込めます（メッセージ基盤を挟まない）。
# MAGIC 公開事例では、工場のテレメトリーの遅延が平均 4.5 秒から 0.1 秒に短くなり、設備の過熱を数時間ではなく数分で検知できるようになっています。
# MAGIC 対象範囲特定でも、車歴を秒単位で最新にしておけば、依頼が来た瞬間に「今の時点で正しい台数」で一括実行できます。
# MAGIC
# MAGIC 書き込み方は SDK（gRPC）と REST があります。REST の例（このハンズオンでは実行しません。サービスプリンシパルが必要です）:
# MAGIC
# MAGIC ```bash
# MAGIC # 1) サービスプリンシパルで、書き込み先テーブルに限った OAuth トークンを取る（/oidc/v1/token）
# MAGIC # 2) 取ったトークンで、レコード（JSON の配列）を送る
# MAGIC curl -X POST "https://<workspace-id>.zerobus.<region>.cloud.databricks.com/zerobus/v1/tables/<catalog>.<schema>.vehicle_history/insert" \
# MAGIC   -H "Content-Type: application/json" -H "Authorization: Bearer $OAUTH_TOKEN" \
# MAGIC   -d '[{"vin": "XB3-C000041", "model_code": "XB3", "model_year": 2025, "production_date": "2025-05-12", "production_time": "101530"}]'
# MAGIC ```
# MAGIC 詳しくは Databricks のドキュメント「Zerobus Ingest」を参照してください。

# COMMAND ----------

check(spark.table("vehicle_history").count() >= before, "届いた生産実績を車歴に取り込んだ")
