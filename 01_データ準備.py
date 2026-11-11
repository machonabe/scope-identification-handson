# Databricks notebook source
# MAGIC %md
# MAGIC # 01 データ準備（約 5 分）
# MAGIC
# MAGIC 対象範囲特定で使うデータを、**合成データ**で用意します。
# MAGIC 車種名・型式・VIN・部番・部署名・会社名はすべて架空です。
# MAGIC
# MAGIC | テーブル | 中身 | 使う工程 |
# MAGIC |---|---|---|
# MAGIC | `vehicle_history` | 車歴（型式・年式・製造日時・打刻No・仕向け） | ③〜⑪ |
# MAGIC | `parts_application` | 部品適用情報（部番 → 仕様コード） | ② ③ |
# MAGIC | `first_article_ledger` | 初物台帳（対策品の初物 VIN・製造日） | ④ ⑥ |
# MAGIC | `master_event_vehicles` / `master_scrapped` | 号機マスタ（イベント車・廃車） | ⑤ |
# MAGIC | `country_master` | 仕向け国 → 地域 | ② ③ |
# MAGIC
# MAGIC あわせて、他部門から届いた想定の **調査依頼書（Excel）** をボリュームに置きます（② で読み込みます）。

# COMMAND ----------

# MAGIC %pip install -q openpyxl

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. 車歴を作る
# MAGIC 4 型式 × 3 年式。製造日時は打刻No（製番）の順に進みます。XC5 は今回の部品を使わない型式です。

# COMMAND ----------

import random
from datetime import date, datetime, time, timedelta

import pandas as pd

rnd = random.Random(CFG["data"]["seed"])
N = int(CFG["data"]["vehicles_per_model_year"])

# 型式, シリーズコード, 車種名, 仕様モデル, 工場, 仕様タイプ, 仕様オプション
MODELS = [
    ("XA1", "3010", "SERIES A", "A10", "A", ["TA1", "TA2", "TA3"], ["00", "2H"]),
    ("XA2", "3010", "SERIES A HYBRID", "A20", "A", ["TH1", "TH2"], ["00"]),
    ("XB3", "3020", "SERIES B", "B30", "B", ["TB1", "TB2", "TC1"], ["00", "4W"]),
    ("XC5", "3030", "SERIES C", "C50", "B", ["TD1"], ["00"]),
]
# 年式 → 生産期間（その年式を作っていた期間）
MODEL_YEARS = {2024: (date(2023, 10, 2), date(2024, 9, 27)),
               2025: (date(2024, 10, 1), date(2025, 9, 26)),
               2026: (date(2025, 10, 1), date(2026, 6, 26))}
# 仕向け国（国内は輸出コードが空）
COUNTRIES = [("JP", "日本", "日本", 35), ("US", "アメリカ", "北中米", 18), ("CA", "カナダ", "北中米", 5),
             ("MX", "メキシコ", "北中米", 4), ("DE", "ドイツ", "欧州", 7), ("GB", "イギリス", "欧州", 5),
             ("FR", "フランス", "欧州", 3), ("AU", "オーストラリア", "ア大", 6), ("TH", "タイ", "ア大", 5),
             ("CN", "中国", "中国", 6), ("BR", "ブラジル", "南米", 3), ("SA", "サウジアラビア", "中東アフリカ", 3)]


def workdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


rows = []
for model_code, series_code, series_name, spec_model, plant, types, options in MODELS:
    for i_my, (my, (start, end)) in enumerate(MODEL_YEARS.items()):
        days = workdays(start, end)
        stamps = sorted(datetime.combine(rnd.choice(days), time(8, 0)) + timedelta(seconds=rnd.randint(0, 12 * 3600))
                        for _ in range(N))
        for i, ts in enumerate(stamps):
            serial = f"{1 + i_my // 10}{i_my % 10}{i + 1:05d}"   # 例: 1000001（MY2024）, 1100001（MY2025）
            code, _, _, _ = rnd.choices(COUNTRIES, weights=[c[3] for c in COUNTRIES])[0]
            domestic = code == "JP"
            rows.append({
                "model_year": my, "series_code": series_code, "series_name": series_name, "model_code": model_code,
                "japan_type_approval": f"6AA-{model_code}" if domestic else "-----------",
                "vin": f"{model_code}-{serial}", "production_serial": serial, "production_facility_code": plant,
                "production_date": ts.date(), "production_time": ts.strftime("%H%M%S"),
                "inspection_date": ts.date() + timedelta(days=rnd.randint(1, 20)),
                "spec_model_code": spec_model, "spec_type_code": rnd.choice(types), "spec_option_code": rnd.choice(options),
                "series_number": f"{296 + i_my}", "certificate_no": f"{rnd.randint(1, 9999999):07d}" if domestic else "-------",
                "export_model_code": "" if domestic else f"{model_code}8{my % 10}G",
                "export_type_code": "" if domestic else rnd.choice(["T1", "T2"]),
                "export_country_code": "" if domestic else code,
            })

veh = pd.DataFrame(rows)
# 国内の MY2025 のうち 15 台は、まだ完成検査終了証が出ていない（市場に出ていない）車にする
dom25 = veh[(veh.export_country_code == "") & (veh.model_year == 2025) & (veh.model_code != "XC5")].index
veh.loc[rnd.sample(list(dom25), 15), "certificate_no"] = "-------"
print(f"車歴: {len(veh):,} 台")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. 部品適用情報・初物台帳・号機マスタ・仕向け国を作る
# MAGIC - EPS ギヤボックス（`SG100-…`）は型式ごとに部番が違い、XB3 は TB 系だけが対象部品です（TC1 は別部品）
# MAGIC - 初物台帳：XA1・XA2 は初物 VIN が分かる（時刻まで判定できる）、XB3 は日付だけ分かる

# COMMAND ----------

PARTS = {  # 型式 → 対策前の部番, 対策品の部番, 対象の仕様タイプ（None はすべて）
    "XA1": ("SG100-XA1-A01", "SG100-XA1-A02", None),
    "XA2": ("SG100-XA2-A11", "SG100-XA2-A12", None),
    "XB3": ("SG100-XB3-B01", "SG100-XB3-B02", ["TB1", "TB2"]),
    "XB3-TC": ("SG100-XB4-B51", "SG100-XB4-B51", ["TC1"]),
    "XC5": ("SG100-XC5-C01", "SG100-XC5-C01", None),
}
specs = veh[["model_year", "model_code", "spec_model_code", "spec_type_code", "spec_option_code"]].drop_duplicates()
appl_rows = []
for key, (old, new, only_types) in PARTS.items():
    model_code = key.split("-")[0]
    for r in specs[specs.model_code == model_code].itertuples():
        if only_types and r.spec_type_code not in only_types:
            continue
        for part in sorted({old, new}):
            appl_rows.append({"change_part_no": part, "base_part_no": old, "model_year": r.model_year,
                              "model_code": model_code, "spec_model_code": r.spec_model_code, "spec_type_code": r.spec_type_code,
                              "spec_option_code": r.spec_option_code})
parts_appl = pd.DataFrame(appl_rows)


def nth_vehicle(model_code, my, ratio, types=None):
    """指定した型式・年式の、製造順で ratio の位置にある車（初物の境界に使う）。"""
    df = veh[(veh.model_code == model_code) & (veh.model_year == my)]
    if types:
        df = df[df.spec_type_code.isin(types)]
    df = df.sort_values(["production_date", "production_time"])
    return df.iloc[int(len(df) * ratio)]


xa1, xa2, xb3 = nth_vehicle("XA1", 2025, 0.60), nth_vehicle("XA2", 2025, 0.45), nth_vehicle("XB3", 2025, 0.55, ["TB1", "TB2"])
ledger = pd.DataFrame([
    {"target_part_no": "SG100XA1A01", "countermeasure_part_no": "SG100XA1A02", "quality_improvement_no": "QI-2026-0001",
     "first_article_no": "FA-2026-0001", "reason": "EPS ギヤボックス異音の対策（架空）", "first_article_vin": xa1.vin, "first_article_date": xa1.production_date},
    {"target_part_no": "SG100XA2A11", "countermeasure_part_no": "SG100XA2A12", "quality_improvement_no": "QI-2026-0001",
     "first_article_no": "FA-2026-0002", "reason": "EPS ギヤボックス異音の対策（架空）", "first_article_vin": xa2.vin, "first_article_date": xa2.production_date},
    {"target_part_no": "SG100XB3B01", "countermeasure_part_no": "SG100XB3B02", "quality_improvement_no": "QI-2026-0001",
     "first_article_no": "FA-2026-0003", "reason": "EPS ギヤボックス異音の対策（架空）", "first_article_vin": None, "first_article_date": xb3.production_date},
])

# 号機マスタ：対象になりそうな車の中から、イベント車・廃車を選ぶ
pool = veh[veh.model_code.isin(["XA1", "XA2", "XB3"]) & veh.model_year.isin([2024, 2025])]
dom, exp = pool[pool.export_country_code == ""], pool[pool.export_country_code.isin(["US", "DE"])]
ev = dom.sample(5, random_state=1)
events = pd.DataFrame(
    [{"vin": r.vin, "region": "国内", "ym": str(r.model_year), "model_code": r.model_code, "stamp_no": r.production_serial,
      "event": rnd.choice(["試作評価", "品質確認", "量産確認"]), "destination": "試験部門（架空）", "exported": False, "added_date": date(2025, 11, 1)}
     for r in ev.itertuples()]
    + [{"vin": r.vin, "region": "輸出", "ym": str(r.model_year), "model_code": r.model_code, "stamp_no": r.production_serial,
        "event": "品質確認", "destination": "海外試験（輸出済み）", "exported": True, "added_date": date(2025, 11, 1)}
       for r in exp.sample(2, random_state=2).itertuples()])
scrapped = pd.DataFrame([{"vin": r.vin, "model_name": r.series_name, "market_action_target": "×", "exclusion_reason": "廃車又は廃車予定",
                          "added_date": date(2025, 12, 1)} for r in pool.drop(ev.index).sample(6, random_state=3).itertuples()])
countries = pd.DataFrame([{"country_code": c, "country_name": n, "region_name": g} for c, n, g, _ in COUNTRIES])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. テーブルとして保存する

# COMMAND ----------

TABLES = {
    "vehicle_history": (veh, "車歴（合成データ）"),
    "parts_application": (parts_appl, "部品適用情報（合成データ）：部番 → 仕様コード"),
    "first_article_ledger": (ledger, "初物台帳（対策適用情報）"),
    "master_event_vehicles": (events, "号機マスタ（イベント車）"),
    "master_scrapped": (scrapped, "号機マスタ（廃車）"),
    "country_master": (countries, "仕向け国 → 地域"),
}
for name, (pdf, comment) in TABLES.items():
    spark.createDataFrame(pdf).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(name)
    spark.sql(f"COMMENT ON TABLE {name} IS '{comment}'")
    print(f"{name:24} {len(pdf):>7,} 行  {comment}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. 調査依頼書（Excel）を置く
# MAGIC 他部門が作った依頼書のつもりです。年式が「24M」表記、部番が「/」区切りなど、人が書いた書き方のままにしてあります。

# COMMAND ----------

from openpyxl import Workbook
from openpyxl.styles import Font

wb = Workbook()
ws = wb.active
ws.title = "依頼書"
ws["B1"] = "市場措置 対象号機 調査依頼書（ハンズオン用サンプル・架空）"
ws["B1"].font = Font(bold=True, size=14)
FIELDS = [
    ("依頼件名", "24-25M SERIES A / SERIES B EPS Gear Noise（架空）"), ("依頼No", "REQ-2026-0001"), ("重要度", "B"),
    ("１．依頼者の情報", None), ("部門", "品質保証部（架空）"), ("依頼者", "品質 太郎"), ("依頼日", date(2026, 11, 2)),
    ("２．決定事項又は指示事項", None), ("会議体名", "品質会議（架空）"), ("件名", "EPS ギヤボックス異音不具合（架空）"),
    ("決定事項・指示事項", "提案を承認し、日本・北中米・欧州・ア大で市場措置を実施する。台数確定に向けた VIN リストを依頼する。"),
    ("４．調査の条件（対象特定の条件）", None), ("対象機種", "SERIES A, SERIES B"), ("年式", "24M ～ 25M"),
    ("対象型式", "XA1、XA2、XB3"), ("対象期間", "2023年11月1日 ～ 対策適用まで"), ("対象国名", "日本、北中米、欧州、ア大"),
    ("特記事項", "メキシコ向けは現地で改修済みの可能性あり"),
    ("６．提示資料及び設変の情報", None), ("設変通知No.", "A-26-00123"), ("初物エフ", "あり"),
    ("７．取引先情報", None), ("取引先名", "サプライヤーA（架空）"),
    ("８．回答希望日", date(2026, 11, 20)), ("９．備考欄", "対象号機の最終版を依頼する。"),
]
row = 3
for label, value in FIELDS:
    ws.cell(row=row, column=2, value=label).font = Font(bold=value is None)
    if value is not None:
        ws.cell(row=row, column=3, value=value)
    row += 1
row += 1
ws.cell(row=row, column=2, value="５．対象となる不具合部品番号").font = Font(bold=True)
PART_ROWS = [("No", "部番", "部品名", "適用の情報"),
             (1, "SG100-XA1/XA2", "パワーステアリングギヤボックス（EPS）", "SERIES A XA1、XA2"),
             (2, "SG100-XB3", "パワーステアリングギヤボックス（EPS）", "SERIES B XB3（TB 系）")]
for r in PART_ROWS:
    row += 1
    for c, v in enumerate(r, start=2):
        ws.cell(row=row, column=c, value=v)
ws.column_dimensions["B"].width, ws.column_dimensions["C"].width = 30, 60

side = wb.create_sheet("部番情報 別紙")
for r in [("No", "部番", "部品名", "適用の情報"), (1, "SG100-XA1-A01", "パワーステアリングギヤボックス（EPS）", "SERIES A XA1"),
          (2, "SG100-XA2-A11", "パワーステアリングギヤボックス（EPS）", "SERIES A XA2"),
          (3, "SG100-XB3-B01", "パワーステアリングギヤボックス（EPS）", "SERIES B XB3 TB1・TB2")]:
    side.append(r)
import io
buf = io.BytesIO()                      # メモリ上で Excel を作ってから、ボリュームに書き込む
wb.save(buf)
with open(REQUEST_PATH, "wb") as f:
    f.write(buf.getvalue())
print(f"依頼書を置きました: {REQUEST_PATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5.（07 の準備）アプリの作成を先に始めておく
# MAGIC アプリの計算資源の起動には数分かかるので、ここで作り始めておきます。**待たずに次へ進んでかまいません。**

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.apps import App

_w = WorkspaceClient()
try:
    _w.apps.get(CFG["app_name"])
    print(f"アプリ {CFG['app_name']} は作成済みです")
except NotFound:
    try:
        _w.apps.create(app=App(name=CFG["app_name"], description="市場品質不具合の対象範囲特定（ハンズオン）"))
        print(f"アプリ {CFG['app_name']} の作成を始めました（07 でデプロイします）")
    except Exception as e:
        print(f"アプリの作成を始められませんでした。07 でもう一度試します: {str(e).splitlines()[0][:200]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 確認ポイント

# COMMAND ----------

check(spark.table("vehicle_history").count() == len(veh), f"車歴 {len(veh):,} 台を保存")
check(os.path.exists(REQUEST_PATH), "調査依頼書（Excel）をボリュームに配置")
display(spark.sql("SELECT model_code, series_name, model_year, count(*) AS `台数` FROM vehicle_history GROUP BY ALL ORDER BY ALL"))
