// 対象範囲特定（ハンズオン）の画面。URL の #page で画面を切り替え、/api/* からデータを取ります。
const main = document.getElementById("main");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => (v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("ja-JP"));
const when = (v) => (v ? String(v).replace("T", " ").slice(0, 16) : "—");

async function fetchJson(path) {
  const res = await fetch(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}
// 一度取ったデータは、ページを再読み込みするまで使い回す（画面を行き来しても待たない）
const cache = new Map();
function api(path) {
  if (!cache.has(path)) cache.set(path, fetchJson(path).catch((e) => { cache.delete(path); throw e; }));
  return cache.get(path);
}
let loadingNote = "";  // SQL ウェアハウスが止まっていたときだけ出す
const loading = () => { main.innerHTML = `<div class="state">読み込み中…<span id="loading-note">${esc(loadingNote)}</span></div>`; };
const showError = (e) => { main.innerHTML = `<div class="error">データを読めませんでした：${esc(e.message)}</div>`; };

function table(rows, opts = {}) {
  if (!rows || rows.length === 0) return `<div class="state">${esc(opts.empty || "データがありません")}</div>`;
  const cols = opts.cols || Object.keys(rows[0]).map((k) => ({ key: k, label: k }));
  const plain = (c) => /年式|No|打刻|版|year|serial/i.test(c.label);  // 年式や番号は桁区切りを付けない
  const isNum = (c) => c.num ?? rows.every((r) => r[c.key] === null || r[c.key] === "" || !isNaN(Number(r[c.key])));
  const head = cols.map((c) => `<th class="${isNum(c) ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => `<tr class="${opts.onRow ? "clickable" : ""}" data-i="${i}">` +
    cols.map((c) => `<td class="${isNum(c) ? "num" : ""}">${c.render ? c.render(r) : isNum(c) && !plain(c) ? num(r[c.key]) : esc(r[c.key])}</td>`).join("") + "</tr>").join("");
  return `<div class="table-wrap"><table class="${opts.nowrap ? "nowrap" : ""}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
const judgeChip = (j) => (j === "●" ? `<span class="chip chip-target">● 対象</span>` : `<span class="chip chip-excluded">× 対象外</span>`);
function stack(target, excluded) {
  const t = Number(target) || 0, x = Number(excluded) || 0, all = t + x || 1;
  return `<div class="bar" title="対象 ${num(t)} / 対象外 ${num(x)}"><span class="b-target" style="width:${(t / all) * 100}%"></span><span class="b-excluded" style="width:${(x / all) * 100}%"></span></div>`;
}
const legend = `<div class="legend"><span><i style="background:var(--target)"></i>対象（●）</span><span><i style="background:var(--excluded)"></i>対象外（×）</span></div>`;

// ── 案件管理 ───────────────────────────────────────────────
async function pageCases() {
  loading();
  const detail = await api("/api/cases");
  const { versions } = detail;
  if (!versions.length) { main.innerHTML = `<div class="state">案件がありません。ノートブック 02 を実行してください。</div>`; return; }
  const c = versions[0];
  const latest = versions.find((v) => v.version !== null);
  main.innerHTML = `
    <h1>案件管理</h1><p class="lead">市場措置対象号機の調査案件と、確定結果の版の一覧です。</p>
    <div class="grid cols-main-side">
      <div>
        <div class="card"><h2>案件</h2>${table([c], { cols: [
          { key: "request_no", label: "依頼番号" }, { key: "defect_name", label: "件名" },
          { key: "n_approx", label: "概算VIN", num: true, render: () => num(latest?.n_approx) },
          { key: "n_target", label: "対象台数", num: true, render: () => num(latest?.n_target) },
          { key: "n_excluded", label: "対象外台数", num: true, render: () => num(latest?.n_excluded) },
          { key: "version", label: "Ver", render: () => (latest ? `v${latest.version}` : "—") },
          { key: "status", label: "ステータス", render: (r) => `<span class="chip ${r.status === "完了" ? "chip-ok" : "chip-muted"}">${esc(r.status)}</span>` },
          { key: "n_warn", label: "要確認", render: (r) => (Number(r.n_warn) ? `<span class="chip chip-warn">要確認 ${esc(r.n_warn)}</span>` : "—") },
        ] })}</div>
        <div class="card"><h2>版の履歴</h2>${table(versions.filter((v) => v.version !== null), { cols: [
          { key: "version", label: "版", render: (r) => `v${esc(r.version)}` }, { key: "run_at", label: "確定日時", render: (r) => esc(when(r.run_at)) },
          { key: "n_approx", label: "概算VIN", num: true }, { key: "n_target", label: "対象", num: true }, { key: "n_excluded", label: "対象外", num: true },
        ], empty: "まだ確定結果がありません（05 を実行してください）" })}</div>
        <div class="card"><h2>特定条件</h2>${table(detail.conditions, { cols: [
          { key: "item", label: "項目" }, { key: "raw_text", label: "依頼書の記載" }, { key: "value", label: "特定条件" },
          { key: "status", label: "状態", render: (r) => `<span class="chip ${r.status === "OK" ? "chip-ok" : "chip-warn"}">${esc(r.status)}</span>` },
          { key: "message", label: "確認してほしいこと" },
        ] })}</div>
      </div>
      <div class="card"><h2>依頼内容 — ${esc(c.request_no)}</h2><p class="note">依頼書から読み取った全項目です。</p>
        <div class="kv">${detail.header.map((h) => `<div class="k">${esc(h.item)}</div><div>${esc(h.value)}</div>`).join("")}</div>
        <h2 style="margin-top:16px">部番</h2>${table(detail.parts, { cols: [
          { key: "source", label: "出所" }, { key: "part_no_text", label: "部番" }, { key: "applicability", label: "適用の情報" }] })}
      </div>
    </div>`;
}

// ── ダッシュボード ─────────────────────────────────────────
async function pageDashboard() {
  loading();
  const d = await api("/api/dashboard");
  const k = d.kpi, cm = d.reasons.find((r) => r.reason === "対策適応済み");
  main.innerHTML = `
    <h1>ダッシュボード</h1><p class="lead">確定結果（最新の版）の影響台数です。</p>
    <div class="kpis">
      <div class="kpi k-primary"><div class="label">影響車両台数（概算VIN）</div><div class="value">${num(k.n_approx)}</div></div>
      <div class="kpi k-target"><div class="label">措置対象（●）</div><div class="value">${num(k.n_target)}</div></div>
      <div class="kpi k-excluded"><div class="label">対象外（×）</div><div class="value">${num(k.n_excluded)}</div></div>
      <div class="kpi"><div class="label">うち対策適応済み</div><div class="value">${num(cm?.n)}</div></div>
    </div>
    <p class="fresh">出所：scope_result v${esc(d.version)}・${esc(when(k.run_at))} 時点（05 を実行したら、画面を再読み込みすると次の版が出ます）</p>
    <div class="grid cols-2">
      <div class="card"><h2>地域別の台数</h2>${legend}${table(d.region, { cols: [
        { key: "region", label: "地域" }, { key: "n", label: "概算VIN", num: true }, { key: "target", label: "対象", num: true },
        { key: "excluded", label: "対象外", num: true }, { key: "bar", label: "内訳", num: false, render: (r) => stack(r.target, r.excluded) }] })}</div>
      <div class="card"><h2>対象外の理由</h2>${table(d.reasons, { cols: [
        { key: "reason", label: "理由" }, { key: "n", label: "台数", num: true }] })}
        <p class="note">1 台に理由は 1 つ（最初に当たったもの）。対象 ＋ 対象外 ＝ 概算VIN。</p></div>
    </div>
    <div class="card"><h2>拠点（工場）× 型式別の台数</h2>${legend}${table(d.site_model, { cols: [
      { key: "plant", label: "工場" }, { key: "model_code", label: "型式" }, { key: "series_name", label: "車種" },
      { key: "n", label: "概算VIN", num: true }, { key: "target", label: "対象", num: true }, { key: "excluded", label: "対象外", num: true },
      { key: "bar", label: "内訳", num: false, render: (r) => stack(r.target, r.excluded) }] })}</div>`;
}

// ── ステップ表示 ───────────────────────────────────────────
async function pageSteps(selected = "3") {
  loading();
  const steps = await api("/api/steps");
  const cards = steps.map((s, i) => `${i ? '<span class="arrow">▸</span>' : ""}
    <div class="step ${s.status === "範囲外" ? "out" : "done"} ${s.id === selected ? "active" : ""}" data-step="${esc(s.id)}">
      <div class="s-name">${esc(s.name)}</div><div class="s-desc">${esc(s.desc)}</div>
      <div class="s-sum">${s.status === "範囲外" ? '<span class="chip chip-muted">範囲外</span>' : `<span class="chip chip-ok">完了</span> ${esc(s.summary)}`}</div>
    </div>`).join("");
  main.innerHTML = `<h1>ステップ表示</h1><p class="lead">工程をクリックすると、使ったデータと結果を表示します（③〜⑥は今のデータで計算し直した値です）。</p>
    <div class="card"><div class="pipeline">${cards}</div></div><div id="step-detail"></div>`;
  main.querySelectorAll(".step:not(.out)").forEach((el) => el.addEventListener("click", () => showStep(steps, el.dataset.step)));
  showStep(steps, selected);
}
function showStep(steps, id) {  // 中身は /api/steps で一緒に届いているので、押してもすぐ出る
  main.querySelectorAll(".step").forEach((el) => el.classList.toggle("active", el.dataset.step === id));
  const s = steps.find((x) => x.id === id);
  document.getElementById("step-detail").innerHTML = `<div class="card"><h2>${esc(s.name)} — ${esc(s.desc)}</h2><p class="note">使ったデータ：${esc(s.data)}</p>
    ${s.tables.map((t) => `<h2 style="margin-top:14px">${esc(t.title)}</h2>${table(t.rows)}`).join("")}</div>`;
}

// ── 検索 ───────────────────────────────────────────────────
const searchState = { vin: "", model: "", judgement: "", region: "" };
const searchQuery = () => new URLSearchParams(searchState).toString();
async function pageSearch() {
  loading();
  const qs = searchQuery();
  const d = await api(`/api/search?${qs}`);
  const opt = (list, cur) => `<option value="">すべて</option>` + list.filter(Boolean).map((v) => `<option ${v === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  main.innerHTML = `
    <h1>検索</h1><p class="lead">確定結果（v${esc(d.version)}）から車を探します。</p>
    <div class="grid" style="grid-template-columns: 260px minmax(0,1fr)">
      <div class="card filters"><h2>検索条件</h2>
        <label>VIN（部分一致）</label><input id="f-vin" value="${esc(searchState.vin)}" placeholder="例：XA1-11">
        <label>型式</label><select id="f-model">${opt(d.models, searchState.model)}</select>
        <label>判定</label><select id="f-judgement"><option value="">すべて</option>
          <option value="●" ${searchState.judgement === "●" ? "selected" : ""}>● 対象</option><option value="×" ${searchState.judgement === "×" ? "selected" : ""}>× 対象外</option></select>
        <label>地域</label><select id="f-region">${opt(d.regions, searchState.region)}</select>
        <button class="btn btn-primary" id="f-run">検索実行</button>
      </div>
      <div class="card">
        <div class="toolbar"><div><b style="font-size:20px">${num(d.total.n)}</b> 台（うち対象 ${num(d.total.target)} 台）<span class="note">・先頭 200 台を表示</span></div>
          <a class="btn" href="/api/search.csv?${qs}">CSV 出力</a></div>
        ${table(d.rows, { cols: [
          { key: "vin", label: "VIN" }, { key: "model_year", label: "年式", num: false }, { key: "production_serial", label: "打刻No", num: false },
          { key: "model_code", label: "型式" }, { key: "production_date", label: "製造日" }, { key: "country_name", label: "仕向け" },
          { key: "judgement", label: "判定", render: (r) => judgeChip(r.judgement) }, { key: "exclusion_reason", label: "対象外理由" }, { key: "notes", label: "注記" }],
          empty: "条件に合う車はありません", nowrap: true })}
      </div>
    </div>`;
  document.getElementById("f-run").addEventListener("click", () => {
    Object.assign(searchState, { vin: val("f-vin"), model: val("f-model"), judgement: val("f-judgement"), region: val("f-region") });
    pageSearch().catch(showError);
  });
}
const val = (id) => document.getElementById(id).value.trim();

// ── レポート出力 ───────────────────────────────────────────
async function pageReport() {
  loading();
  const d = await api("/api/report");
  const h = d.header, c = d.conclusion;
  const row = (k, v) => `<tr><th style="width:200px">${esc(k)}</th><td>${esc(v)}</td></tr>`;
  main.innerHTML = `
    <h1>レポート出力</h1><p class="lead">調査結果サマリーと、05 が出力した回答用リスト（Excel）です。</p>
    <div class="grid cols-main-side">
      <div class="doc">
        <h2>市場措置　調査結果サマリー</h2>
        <p class="note">依頼No：${esc(h["依頼No"])}　／　版 v${esc(d.version)}・${esc(when(c.run_at))} 時点</p>
        <h3>1. 案件</h3><table>${row("依頼件名", h["依頼件名"])}${row("件名（不具合名）", h["件名"])}${row("依頼日", h["依頼日"])}
          ${row("依頼元", `${h["部門"] || ""} ${h["依頼者"] || ""}`)}${row("会議体", h["会議体名"])}${row("重要度", h["重要度"])}</table>
        <h3>2. 結論</h3><p class="big">市場措置の対象　<b>${num(c.n_target)}</b> 台 <span class="note">（概算VIN ${num(c.n_approx)} 台のうち ● の合計）</span></p>
        <table>${row("対象の製作期間", `${c.date_from || "—"} 〜 ${c.date_to || "—"}`)}${row("対象号機（製造日の最初〜最後）", `${c.vin_first || "—"} 〜 ${c.vin_last || "—"}`)}
          ${row("対象型式", c.models)}${row("対象地域", c.regions)}${row("調査条件（対象期間）", h["対象期間"])}${row("調査条件（対象国名）", h["対象国名"])}</table>
      </div>
      <div class="card"><h2>回答用リスト（Excel）</h2>
        ${d.files.length ? d.files.map((f) => `<div class="toolbar"><span>${esc(f.name)}</span><a class="btn" href="/api/reports/${encodeURIComponent(f.name)}">ダウンロード</a></div>`).join("")
          : `<div class="state">まだファイルがありません（05 を実行してください）</div>`}
      </div>
    </div>`;
}

// ── 画面の切り替え ─────────────────────────────────────────
const PAGES = { cases: pageCases, dashboard: pageDashboard, steps: () => pageSteps(), search: pageSearch, report: pageReport };
function route() {
  const page = (location.hash || "#cases").slice(1);
  document.querySelectorAll(".sidenav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  return (PAGES[page] || pageCases)().catch(showError);
}
// 最初の画面が出たら、ほかの画面のデータを 1 つずつ先に取っておく
async function prefetch() {
  for (const path of ["/api/cases", "/api/dashboard", "/api/steps", `/api/search?${searchQuery()}`, "/api/report"]) {
    await api(path).catch(() => {});
  }
}
window.addEventListener("hashchange", route);
api("/api/meta").then((m) => {
  document.getElementById("source").textContent = `${m.catalog}.${m.schema}`;
  document.getElementById("user").textContent = m.user || "（ユーザー不明）";
  document.getElementById("identity").textContent = `データは${m.identity}で読み込んでいます`;
  if (m.warehouse_state && m.warehouse_state !== "RUNNING") {
    loadingNote = "（SQL ウェアハウスが止まっていたので起動しています。最初だけ 10〜20 秒かかります）";
    const el = document.getElementById("loading-note");
    if (el) el.textContent = loadingNote;
  }
}).catch(() => {});
route().then(() => { loadingNote = ""; prefetch(); });
