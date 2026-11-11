"""対象範囲特定ハンズオンのアプリ（FastAPI）。

ハンズオンのノートブックが作った Unity Catalog のテーブル・ビューを、SQL ウェアハウス経由で読んで画面に出します。
カタログ・スキーマは、07 がこのフォルダにコピーした config.yaml から読みます。
データはアプリのサービスプリンシパルの権限で読みます（07 で SELECT・READ VOLUME を付与）。
画面 1 つ分の SQL はまとめて同時に投げます（待ち時間を、一番遅い 1 本分にするため）。
"""
import csv
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem, StatementState
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path):
    """config.yaml を読む（PyYAML が無ければ 2 階層までの簡単な YAML として読む）。"""
    text = open(path, encoding="utf-8").read()
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
            root[key] = value if value else {}
            parent = None if value else root[key]
        elif parent is not None:
            parent[key] = value
    return root


CFG = load_config(os.path.join(HERE, "config.yaml"))
CATALOG, SCHEMA, VOLUME = CFG["catalog"], CFG["schema"], CFG["volume"]
OUTPUT_DIR = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}/outputs"
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

w = WorkspaceClient()
app = FastAPI(title="対象範囲特定（ハンズオン）")
POOL = ThreadPoolExecutor(max_workers=8)  # 同時に投げる SQL の上限（2X-Small のウェアハウスで待ち行列にならない程度）
LATEST = "(SELECT max(version) FROM scope_result)"  # 最新の版
NO_RESULT = "まだ確定結果がありません。05 を実行してください"


def q(sql, **params):
    """SQL を実行して、行を dict のリストで返す（パラメータは :name で受け取る）。"""
    if not WAREHOUSE_ID:
        raise HTTPException(500, "SQL ウェアハウスがアプリに設定されていません（07 を実行し直してください）")
    plist = [StatementParameterListItem(name=k, value=None if v is None else str(v)) for k, v in params.items()]
    r = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID, statement=sql, catalog=CATALOG, schema=SCHEMA, parameters=plist,
        wait_timeout="50s", disposition=Disposition.INLINE, format=Format.JSON_ARRAY)
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(1)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        msg = r.status.error.message if r.status.error else str(r.status.state)
        raise HTTPException(500, msg.splitlines()[0][:300])
    cols = [c.name for c in r.manifest.schema.columns]
    rows = list(r.result.data_array or []) if r.result else []
    chunk = r.result.next_chunk_index if r.result else None
    while chunk is not None:
        part = w.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk)
        rows += part.data_array or []
        chunk = part.next_chunk_index
    return [dict(zip(cols, row)) for row in rows]


def qall(**jobs):
    """名前 → SQL（または (SQL, パラメータ)、関数）を同時に実行して、名前 → 結果で返す。"""
    futures = {}
    for name, job in jobs.items():
        if callable(job):
            futures[name] = POOL.submit(job)
        else:
            sql, params = job if isinstance(job, tuple) else (job, {})
            futures[name] = POOL.submit(q, sql, **params)
    return {name: f.result() for name, f in futures.items()}


def version_filter(version):
    """版の条件（指定がなければ最新の版）と、そのパラメータ。"""
    return ("version = :v", {"v": version}) if version else (f"version = {LATEST}", {})


@app.exception_handler(HTTPException)
async def http_error(_, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/api/meta")
def meta(request: Request):
    try:  # 止まっているときだけ、画面に「起動しています」と出すため
        state = getattr(w.warehouses.get(WAREHOUSE_ID).state, "value", "") if WAREHOUSE_ID else ""
    except Exception:
        state = ""
    return {"catalog": CATALOG, "schema": SCHEMA, "user": request.headers.get("x-forwarded-email", ""),
            "identity": "アプリのサービスプリンシパル", "warehouse_state": state}


CASES_SQL = """
  WITH v AS (SELECT request_no, version, max(run_at) AS run_at, count(*) AS n_approx,
                    count_if(judgement = '●') AS n_target, count_if(judgement = '×') AS n_excluded
             FROM scope_result GROUP BY request_no, version)
  SELECT c.request_no, c.defect_name, c.status, c.registered_at, v.version, v.run_at, v.n_approx, v.n_target, v.n_excluded,
         (SELECT count_if(status = '要確認') FROM cond_items) AS n_warn
  FROM case_registry c LEFT JOIN v ON v.request_no = c.request_no
  ORDER BY c.registered_at DESC, v.version DESC"""


def case_jobs(where, **p):
    """案件の依頼内容（依頼書の項目・部番・特定条件）を取る SQL。"""
    return {"header": (f"SELECT item, value FROM req_header WHERE {where}", p),
            "parts": (f"SELECT no, part_no_text, part_name, applicability, source FROM req_parts WHERE {where} ORDER BY source, no", p),
            "conditions": (f"SELECT item, raw_text, value, used_in, status, message FROM cond_items WHERE {where} ORDER BY status DESC, item", p)}


@app.get("/api/cases")
def cases():
    # 一覧と、いちばん新しく登録した案件の依頼内容を同時に取る
    return qall(versions=CASES_SQL, **case_jobs("request_no = (SELECT max_by(request_no, registered_at) FROM case_registry)"))


@app.get("/api/case/{request_no}")
def case_detail(request_no: str):
    return qall(**case_jobs("request_no = :r", r=request_no))


@app.get("/api/dashboard")
def dashboard(version: int | None = None):
    where, p = version_filter(version)
    r = qall(
        kpi=(f"""SELECT max(version) AS version, count(*) AS n_approx, count_if(judgement = '●') AS n_target,
                        count_if(judgement = '×') AS n_excluded, max(run_at) AS run_at FROM scope_result WHERE {where}""", p),
        reasons=(f"""SELECT exclusion_reason AS reason, count(*) AS n FROM scope_result
                     WHERE {where} AND judgement = '×' GROUP BY 1 ORDER BY 2 DESC""", p),
        region=(f"""SELECT region_name AS region, count(*) AS n, count_if(judgement = '●') AS target, count_if(judgement = '×') AS excluded
                    FROM scope_result WHERE {where} GROUP BY 1 ORDER BY 2 DESC""", p),
        site_model=(f"""SELECT coalesce(production_facility_code, '-') AS plant, model_code, series_name, count(*) AS n,
                               count_if(judgement = '●') AS target, count_if(judgement = '×') AS excluded
                        FROM scope_result WHERE {where} GROUP BY ALL ORDER BY plant, model_code""", p))
    kpi = r["kpi"][0]
    if kpi["version"] is None:
        raise HTTPException(404, NO_RESULT)
    return {"version": int(kpi["version"]), "kpi": kpi, "reasons": r["reasons"], "region": r["region"], "site_model": r["site_model"]}


STEPS = [
    ("1", "① 依頼書取込", "依頼書（Excel）を読み取り案件登録", "req_header・req_parts"),
    ("2", "② 条件自動生成", "依頼書を特定条件に翻訳", "cond_items ほか cond_*"),
    ("3", "③ 号機特定", "部品→仕様コード→車歴で概算VIN", "v_step3_approx_vin"),
    ("4", "④ 対象期間", "製造日の窓 [From, To]", "v_step4_window"),
    ("5", "⑤ マスタ照合", "イベント車・廃車・終了証", "v_step5_master_flags"),
    ("6", "⑥ 対策適応", "初物の境界で対策適応済み", "v_step6_countermeasure"),
    ("7", "⑦〜⑨", "点検改修・Lot崩れ・打刻No飛び（ハンズオンの範囲外）", None),
    ("10", "⑩ 対象確定", "上から順に判定して ●／×", "scope_result"),
    ("11", "⑪ 帳票出力", "回答用リスト（Excel）", "ボリューム outputs"),
]


@app.get("/api/steps")
def steps():
    """①〜⑪ の工程と、それぞれの中身（表）を一度に返す。画面で工程を押したときは待たない。"""
    r = qall(
        version=f"SELECT {LATEST} AS v",
        s1_header="SELECT item AS `項目`, value AS `値` FROM req_header",
        s1_parts="SELECT source AS `出所`, no AS `No`, part_no_text AS `部番`, part_name AS `部品名`, applicability AS `適用の情報` FROM req_parts ORDER BY source, no",
        s2="""SELECT item AS `項目`, raw_text AS `依頼書の記載`, value AS `特定条件`, used_in AS `使う工程`,
                     status AS `状態`, message AS `確認してほしいこと` FROM cond_items ORDER BY status DESC, item""",
        s3_funnel="""
          WITH part_cars AS (SELECT f.* FROM vehicle_history f JOIN v_part_spec USING (model_year, model_code, spec_model_code, spec_type_code, spec_option_code))
          SELECT '1. 車歴すべて' AS `段階`, count(*) AS `台数` FROM vehicle_history
          UNION ALL SELECT '2. 対象部品が付く車', count(*) FROM part_cars
          UNION ALL SELECT '3. ＋ 型式・年式', count(*) FROM part_cars WHERE model_code IN (SELECT model_code FROM cond_models)
               AND model_year BETWEEN (SELECT model_year_from FROM cond_year) AND (SELECT model_year_to FROM cond_year)
          UNION ALL SELECT '4. ＋ 仕向け国 ＝ 概算VIN', count(*) FROM v_step3_approx_vin ORDER BY 1""",
        s3_model="""SELECT model_code AS `型式`, series_name AS `車種`, model_year AS `年式`, count(*) AS `概算VIN`, min(vin) AS `見本VIN`
                    FROM v_step3_approx_vin GROUP BY ALL ORDER BY ALL""",
        s4="""SELECT part_family AS `部品系統`, from_kind AS `From種別`, window_from AS `From`,
                     to_kind AS `To種別`, window_to AS `To`, coalesce(first_article_vin, '（日付のみ）') AS `初物VIN`
              FROM v_step4_window ORDER BY 1""",
        s5="""SELECT count(*) AS `概算VIN`, count_if(is_scrapped OR is_event OR cert_not_issued) AS `印が付いた車`, count_if(is_scrapped) AS `廃車`,
                     count_if(is_event AND NOT event_exported) AS `イベント車（非輸出）`,
                     count_if(is_event AND event_exported) AS `イベント車（輸出済み・注記）`,
                     count_if(cert_not_issued) AS `終了証未発行` FROM v_step5_master_flags""",
        s6="""SELECT part_family AS `部品系統`, rule AS `判定方法`, boundary AS `境界`, count(*) AS `概算VIN`,
                     count_if(is_cm_applied) AS `対策適応済み`, count_if(same_day_kept) AS `当日製造で対象に残した車`
              FROM v_step6_countermeasure GROUP BY ALL ORDER BY 1""",
        s10=f"""SELECT coalesce(exclusion_reason, '（対象）') AS `理由`, judgement AS `判定`, count(*) AS `台数`
                FROM scope_result WHERE version = {LATEST} GROUP BY ALL ORDER BY 2 DESC, 3 DESC""",
        files=list_reports)
    total = lambda rows, col, keep=lambda x: True: sum(int(x[col] or 0) for x in rows if keep(x))
    v, files, conds = r["version"][0]["v"], r["files"], r["s2"]
    tables = {"1": [("依頼書の項目", r["s1_header"]), ("部番", r["s1_parts"])],
              "2": [("特定条件", conds)],
              "3": [("絞り込みの流れ", r["s3_funnel"]), ("型式別の内訳", r["s3_model"])],
              "4": [("製造日の窓", r["s4"])],
              "5": [("マスタの印", r["s5"])],
              "6": [("対策適応の判定", r["s6"])],
              "10": [(f"判定結果（v{v or '—'}）", r["s10"])],
              "11": [("回答用リスト", [{"ファイル": f["name"], "サイズ(KB)": round(f["size"] / 1024, 1)} for f in files])]}
    summary = {"1": f"項目 {len(r['s1_header'])}・部番 {len(r['s1_parts'])} 行",
               "2": f"条件 {len(conds)} 件（要確認 {sum(c['状態'] == '要確認' for c in conds)}）",
               "3": f"概算VIN {total(r['s3_model'], '概算VIN'):,} 台",
               "4": f"窓 {sum(x['To'] is not None for x in r['s4'])} 部品系統",
               "5": f"印 {total(r['s5'], '印が付いた車')} 台",
               "6": f"対策適応済み {total(r['s6'], '対策適応済み'):,} 台",
               "10": f"対象 {total(r['s10'], '台数', lambda x: x['判定'] == '●'):,} 台",
               "11": f"ファイル {len(files)} 件"}
    return [{"id": i, "name": name, "desc": desc, "data": src, "status": "範囲外" if src is None else "完了",
             "summary": summary.get(i, ""), "tables": [{"title": t, "rows": rows} for t, rows in tables.get(i, [])]}
            for i, name, desc, src in STEPS]


SEARCH_SQL = """
  SELECT vin, model_year, production_serial, model_code, series_name, production_facility_code AS plant,
         production_date, country_name, region_name, judgement, coalesce(exclusion_reason, '') AS exclusion_reason, notes, version
  FROM scope_result
  WHERE {where}
    AND (:vin IS NULL OR vin ILIKE concat('%', :vin, '%'))
    AND (:model IS NULL OR model_code = :model)
    AND (:judgement IS NULL OR judgement = :judgement)
    AND (:region IS NULL OR region_name = :region)
  ORDER BY model_year, model_code, production_serial"""


def _search(version, vin, model, judgement, region):
    where, p = version_filter(version)
    return SEARCH_SQL.format(where=where), {**p, "vin": vin or None, "model": model or None,
                                            "judgement": judgement or None, "region": region or None}


@app.get("/api/search")
def search(version: int | None = None, vin: str = "", model: str = "", judgement: str = "", region: str = ""):
    sql, p = _search(version, vin, model, judgement, region)
    where, vp = version_filter(version)
    r = qall(total=(f"SELECT count(*) AS n, count_if(judgement = '●') AS target FROM ({sql})", p),
             rows=(sql + " LIMIT 200", p),
             options=(f"""SELECT max(version) AS version, concat_ws(',', array_sort(collect_set(model_code))) AS models,
                                 concat_ws(',', array_sort(collect_set(region_name))) AS regions
                          FROM scope_result WHERE {where}""", vp))
    o = r["options"][0]
    if o["version"] is None:
        raise HTTPException(404, NO_RESULT)
    return {"version": int(o["version"]), "total": r["total"][0], "rows": r["rows"],
            "models": o["models"].split(","), "regions": o["regions"].split(",")}


@app.get("/api/search.csv")
def search_csv(version: int | None = None, vin: str = "", model: str = "", judgement: str = "", region: str = ""):
    sql, p = _search(version, vin, model, judgement, region)
    rows = q(sql, **p)
    buf = io.StringIO()
    buf.write("﻿")  # Excel で文字化けしないように BOM を付ける
    writer = csv.writer(buf)
    writer.writerow(["VIN", "年式", "打刻No", "型式", "車種", "工場", "製造日", "仕向け", "地域", "判定", "対象外理由", "注記"])
    for r in rows:
        writer.writerow([r["vin"], r["model_year"], r["production_serial"], r["model_code"], r["series_name"], r["plant"],
                         r["production_date"], r["country_name"], r["region_name"], r["judgement"], r["exclusion_reason"], r["notes"]])
    v = rows[0]["version"] if rows else version or "latest"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''vin_list_v{v}.csv"})


@app.get("/api/report")
def report(version: int | None = None):
    where, p = version_filter(version)
    r = qall(head="SELECT item, value FROM req_header",
             concl=(f"""SELECT max(version) AS version, count_if(judgement = '●') AS n_target, count(*) AS n_approx,
                               min(CASE WHEN judgement = '●' THEN production_date END) AS date_from,
                               max(CASE WHEN judgement = '●' THEN production_date END) AS date_to,
                               min_by(vin, CASE WHEN judgement = '●' THEN concat(production_date, production_serial) END) AS vin_first,
                               max_by(vin, CASE WHEN judgement = '●' THEN concat(production_date, production_serial) END) AS vin_last,
                               concat_ws(' / ', array_sort(collect_set(CASE WHEN judgement = '●' THEN model_code END))) AS models,
                               concat_ws(' / ', array_sort(collect_set(CASE WHEN judgement = '●' THEN region_name END))) AS regions,
                               max(run_at) AS run_at
                        FROM scope_result WHERE {where}""", p),
             files=list_reports)
    c = r["concl"][0]
    if c["version"] is None:
        raise HTTPException(404, NO_RESULT)
    return {"version": int(c["version"]), "header": {x["item"]: x["value"] for x in r["head"]}, "conclusion": c, "files": r["files"]}


def list_reports():
    try:
        files = [{"name": e.name, "size": e.file_size or 0, "modified": e.last_modified}
                 for e in w.files.list_directory_contents(OUTPUT_DIR) if not e.is_directory]
    except Exception:
        return []
    return sorted(files, key=lambda f: f["name"], reverse=True)


@app.get("/api/reports/{name}")
def download_report(name: str):
    if "/" in name or not name.endswith(".xlsx"):
        raise HTTPException(400, "ファイル名が不正です")
    data = w.files.download(f"{OUTPUT_DIR}/{name}").contents.read()
    return StreamingResponse(iter([data]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


def warm_up():
    """起動直後に各画面の SQL を一度流し、最初に開いた人を待たせない（ウェアハウスが止まっていたら起こさない）。"""
    t0 = time.time()
    try:
        if getattr(w.warehouses.get(WAREHOUSE_ID).state, "value", "") != "RUNNING":
            print("warm-up: SQL ウェアハウスが止まっているので省略")
            return
        for page in (cases, dashboard, steps, lambda: search(), report):
            page()
        print(f"warm-up: {time.time() - t0:.1f} 秒で完了")
    except Exception as e:
        print(f"warm-up: 省略（{str(e).splitlines()[0][:200]}）")


if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=warm_up, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", 8000)))
