# Databricks notebook source
# MAGIC %md
# MAGIC # 07 アプリをデプロイする（約 5 分）
# MAGIC
# MAGIC 05 までの結果を、業務の画面（**Databricks Apps**）で見られるようにします。CLI は使わず、このノートブックだけでデプロイします。
# MAGIC
# MAGIC | 画面 | 中身 |
# MAGIC |---|---|
# MAGIC | 案件管理 | 案件・版の一覧、依頼内容、特定条件（要確認） |
# MAGIC | ダッシュボード | 影響車両台数・対象・対象外、地域別、工場 × 型式別 |
# MAGIC | ステップ表示 | ①〜⑪ の流れ。工程を押すと、使ったデータと結果を表示 |
# MAGIC | 検索 | VIN・型式・判定・地域で絞り込み、CSV 出力 |
# MAGIC | レポート出力 | 調査結果サマリーと、回答用リスト（Excel）のダウンロード |
# MAGIC
# MAGIC アプリのコードは同じフォルダの `app/` にあります（Python の FastAPI と、画面の HTML・JavaScript・CSS）。

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. 設定をアプリに渡す
# MAGIC `config.yaml` を `app/` にコピーします。アプリも同じカタログ・スキーマを読みます。

# COMMAND ----------

import shutil

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.apps import (App, AppDeployment, AppResource, AppResourceSqlWarehouse,
                                         AppResourceSqlWarehouseSqlWarehousePermission, ComputeState)

w = WorkspaceClient()
APP_NAME = CFG["app_name"]
APP_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "app")
check(os.path.exists(os.path.join(APP_DIR, "app.py")), f"アプリのコードがある: {APP_DIR}")
shutil.copyfile(CONFIG_PATH, os.path.join(APP_DIR, "config.yaml"))

warehouses = list(w.warehouses.list())
check(len(warehouses) > 0, f"SQL ウェアハウスがある: {warehouses[0].name if warehouses else '—'}")
WAREHOUSE_ID = warehouses[0].id

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. アプリを作る（01 で作り始めていれば、それを使います）
# MAGIC アプリには、使ってよい SQL ウェアハウスを「リソース」として渡します。

# COMMAND ----------

resource = AppResource(name="sql-warehouse", sql_warehouse=AppResourceSqlWarehouse(
    id=WAREHOUSE_ID, permission=AppResourceSqlWarehouseSqlWarehousePermission.CAN_USE))
try:
    app = w.apps.get(APP_NAME)
    print(f"アプリ {APP_NAME} は作成済みです。リソースを設定します。")
    w.apps.update(APP_NAME, app=App(name=APP_NAME, description=app.description, resources=[resource]))
except NotFound:
    print(f"アプリ {APP_NAME} を作ります（2〜3 分）…")
    w.apps.create_and_wait(app=App(name=APP_NAME, description="市場品質不具合の対象範囲特定（ハンズオン）", resources=[resource]))

app = w.apps.get(APP_NAME)
if app.compute_status.state != ComputeState.ACTIVE:
    print("アプリの計算資源を起動します（1〜3 分）…")
    try:
        w.apps.start_and_wait(APP_NAME)
    except Exception as e:  # 起動中の場合など
        print(f"  起動を待ちます: {str(e).splitlines()[0][:120]}")
        w.apps.wait_get_app_active(APP_NAME)
app = w.apps.get(APP_NAME)
print(f"計算資源: {app.compute_status.state}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. アプリにデータを読む権限を付ける
# MAGIC アプリは自分用のサービスプリンシパルで動きます。このスキーマの読み取りと、ボリュームの読み取りだけを許可します。

# COMMAND ----------

sp = app.service_principal_client_id
for stmt in [f"GRANT USE CATALOG ON CATALOG `{CATALOG}` TO `{sp}`",
             f"GRANT USE SCHEMA, SELECT ON SCHEMA `{CATALOG}`.`{SCHEMA}` TO `{sp}`",
             f"GRANT READ VOLUME ON VOLUME `{CATALOG}`.`{SCHEMA}`.`{VOLUME}` TO `{sp}`"]:
    spark.sql(stmt)
    print(stmt)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. デプロイする

# COMMAND ----------

print("デプロイしています（1〜2 分）…")
try:   # SDK の版によって引数の渡し方が違うので、両方に対応する
    deployment = w.apps.deploy_and_wait(app_name=APP_NAME, app_deployment=AppDeployment(source_code_path=APP_DIR))
except TypeError:
    deployment = w.apps.deploy_and_wait(app_name=APP_NAME, source_code_path=APP_DIR)
print(f"デプロイ: {deployment.status.state}  {deployment.status.message or ''}")
app = w.apps.get(APP_NAME)
displayHTML(f'<p style="font-size:16px">アプリを開く → <a href="{app.url}" target="_blank">{app.url}</a></p>'
            '<p>初めて開くときは、アクセスの許可を求める画面が出ることがあります。</p>')

# COMMAND ----------

# MAGIC %md
# MAGIC ## 確認ポイント
# MAGIC - アプリを開き、ダッシュボードの台数が 05 の結果と同じか確かめる
# MAGIC - 06 を実行してから 05 をもう一度実行すると、アプリの版が増え、台数が変わる（画面を再読み込み）
# MAGIC - アプリは起動から 24 時間で自動的に止まります。止まったら、このノートブックをもう一度実行してください

# COMMAND ----------

check(deployment.status.state.value == "SUCCEEDED", "アプリをデプロイした")
check(app.app_status.state.value in ("RUNNING", "DEPLOYING"), f"アプリの状態: {app.app_status.state.value}")
