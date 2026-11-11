# Databricks notebook source
# MAGIC %md
# MAGIC # 99 後片付け
# MAGIC ハンズオンで作ったスキーマ（テーブル・ビュー・ボリュームの中身すべて）と、07 でデプロイしたアプリを削除します。**元に戻せません。**
# MAGIC 削除してよい場合だけ、下のウィジェットで「削除する」を選んで実行してください。

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

dbutils.widgets.dropdown("confirm", "削除しない", ["削除しない", "削除する"], "スキーマとアプリを削除しますか")
if dbutils.widgets.get("confirm") == "削除する":
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.errors import NotFound
    try:
        WorkspaceClient().apps.delete(CFG["app_name"])
        print(f"アプリを削除しました: {CFG['app_name']}")
    except NotFound:
        print(f"アプリ {CFG['app_name']} はありません")
    spark.sql(f"DROP SCHEMA IF EXISTS `{CATALOG}`.`{SCHEMA}` CASCADE")
    print(f"削除しました: {CATALOG}.{SCHEMA}")
else:
    print("何もしていません。削除するときはウィジェットで「削除する」を選んで、もう一度実行してください。")
