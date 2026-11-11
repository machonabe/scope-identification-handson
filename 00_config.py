# Databricks notebook source
# MAGIC %md
# MAGIC # 00_config（共通設定の読み込み）
# MAGIC
# MAGIC 各ノートブックの最初で `%run ./00_config` として呼び出します。単体で実行する必要はありません。
# MAGIC
# MAGIC - 同じフォルダの `config.yaml` を読み込み、カタログ・スキーマ・ボリュームを決めます
# MAGIC - スキーマとボリュームが無ければ作り、以降の SQL セルはこのスキーマで動きます
# MAGIC - 後のノートブックで使う変数：`CFG`、`CATALOG`、`SCHEMA`、`VOLUME_PATH`、`REQUEST_PATH`、`OUTPUT_DIR`、`LANDING_DIR`、`CONFIG_PATH`

# COMMAND ----------

import os


def _config_candidates():
    """config.yaml を探す場所：作業フォルダ → ノートブックのフォルダ。"""
    yield os.path.join(os.getcwd(), "config.yaml")
    try:
        nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
        yield os.path.join("/Workspace" + os.path.dirname(nb_path), "config.yaml")
    except Exception:
        pass


def _parse_yaml(text):
    """PyYAML があれば使い、無ければ 2 階層までの簡単な YAML を読みます。"""
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        pass
    root, parent = {}, None
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"').strip("'")
        if not raw.startswith(" "):
            if value:
                root[key] = int(value) if value.isdigit() else value
                parent = None
            else:
                parent = root.setdefault(key, {})
        elif parent is not None:
            parent[key] = int(value) if value.isdigit() else value
    return root


_path = next((p for p in _config_candidates() if os.path.exists(p)), None)
if _path is None:
    raise FileNotFoundError("config.yaml が見つかりません。ノートブックと同じフォルダに config.yaml を置いてください。")
with open(_path, encoding="utf-8") as f:
    CFG = _parse_yaml(f.read())
CONFIG_PATH = _path

CATALOG, SCHEMA, VOLUME = CFG["catalog"], CFG["schema"], CFG["volume"]
VOLUME_PATH = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
REQUEST_PATH = f"{VOLUME_PATH}/requests/{CFG['request_file']}"
OUTPUT_DIR = f"{VOLUME_PATH}/outputs"
LANDING_DIR = f"{VOLUME_PATH}/landing"

# COMMAND ----------

_catalogs = [r[0] for r in spark.sql("SHOW CATALOGS").collect()]
if CATALOG not in _catalogs:
    raise ValueError(f"カタログ '{CATALOG}' がありません。config.yaml の catalog を次のどれかにしてください: {_catalogs}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")
spark.sql(f"USE CATALOG `{CATALOG}`")
spark.sql(f"USE SCHEMA `{SCHEMA}`")
for _d in (f"{VOLUME_PATH}/requests", OUTPUT_DIR, LANDING_DIR):
    os.makedirs(_d, exist_ok=True)


def check(ok, message):
    """確認ポイントの結果を表示します（不合格なら止めます）。"""
    if not ok:
        raise AssertionError(f"❌ {message}")
    print(f"✅ {message}")


print(f"設定ファイル : {_path}")
print(f"保存先       : {CATALOG}.{SCHEMA}（ボリューム {VOLUME_PATH}）")
