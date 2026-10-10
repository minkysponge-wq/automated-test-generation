/* CPRI ATRG - plain JavaScript (no build step) */
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

  // fetch wrapper: sends CSRF token, returns {status, data}
  async function post(url, body, isForm) {
    const opts = { method: "POST", headers: { "X-CSRF-Token": csrf, "X-Requested-With": "fetch" } };
    if (isForm) { opts.body = body; } else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body || {}); }
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { data = { ok: false, error: "Unexpected server response" }; }
    return { status: res.status, data };
  }

  // ---------------------------------------------------------------- tests: select all / group toggles
  const all = $("#select-all");
  function syncToggles() {
    $$(".js-group").forEach(g => {
      const items = $$(`.js-test[data-group="${g.dataset.group}"]`);
      g.checked = items.length > 0 && items.every(i => i.checked);
      g.indeterminate = !g.checked && items.some(i => i.checked);
    });
    if (all) {
      const items = $$(".js-test");
      all.checked = items.every(i => i.checked);
      all.indeterminate = !all.checked && items.some(i => i.checked);
    }
  }
  if (all) {
    all.addEventListener("change", () => { $$(".js-test").forEach(i => (i.checked = all.checked)); syncToggles(); });
    $$(".js-group").forEach(g => g.addEventListener("change", () => {
      $$(`.js-test[data-group="${g.dataset.group}"]`).forEach(i => (i.checked = g.checked)); syncToggles();
    }));
    $$(".js-test").forEach(i => i.addEventListener("change", syncToggles));
    syncToggles();
  }

  // ---------------------------------------------------------------- drawings rows
  const addDrawing = $("#add-drawing");
  function drawingRow(d) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td><input name="drawing_number"></td><td><input name="drawing_sheet"></td><td><input name="drawing_rev"></td>' +
      '<td><button type="button" class="btn btn-sm js-delrow">✕</button></td>';
    if (d) { const ins = $$("input", tr); ins[0].value = d.number || ""; ins[1].value = d.sheet || ""; ins[2].value = d.rev || ""; }
    $("#drawings tbody").appendChild(tr);
  }
  if (addDrawing) addDrawing.addEventListener("click", () => drawingRow());
  document.addEventListener("click", e => {
    if (e.target.classList.contains("js-delrow")) {
      const body = e.target.closest("tbody");
      if (body.rows.length > 1) e.target.closest("tr").remove(); else $$("input", e.target.closest("tr")).forEach(i => (i.value = ""));
    }
  });

  // ---------------------------------------------------------------- request Excel auto-fill
  const reqExcel = $("#request-excel");
  if (reqExcel) reqExcel.addEventListener("change", async () => {
    if (!reqExcel.files.length) return;
    const fd = new FormData(); fd.append("file", reqExcel.files[0]);
    const status = $("#excel-status"); status.textContent = "Reading…";
    const { data } = await post(reqExcel.dataset.url, fd, true);
    if (!data.ok) { status.textContent = data.error || "Failed"; return; }
    const form = $("#request-form");
    let n = 0;
    Object.entries(data.fields).forEach(([name, value]) => {
      const els = $$(`[name="${name}"]`, form);
      els.forEach(el => {
        if (el.type === "radio") { el.checked = (el.value === value); }
        else if (el.tagName === "SELECT") { const opt = Array.from(el.options).find(o => o.value === value || o.text === value); if (opt) el.value = opt.value; }
        else el.value = value;
      });
      if (els.length) n++;
    });
    if (data.drawings.length) { $("#drawings tbody").innerHTML = ""; data.drawings.forEach(d => drawingRow(d)); }
    $$(".js-test").forEach(i => (i.checked = data.tests.includes(i.name.replace("test_", ""))));
    syncToggles();
    status.textContent = `✓ Filled ${n} fields, ${data.drawings.length} drawings, ${data.tests.length} tests from Excel. Please review and tick the declaration.`;
  });

  // ---------------------------------------------------------------- engineer: correct a reading
  const dlg = $("#edit-dialog");
  let editing = null;
  document.addEventListener("click", e => {
    const b = e.target.closest(".js-edit");
    if (!b || !dlg) return;
    editing = b;
    $("#edit-label").textContent = `${b.dataset.label} - current value: ${b.dataset.value}`;
    $("#edit-value").value = b.dataset.value;
    $("#edit-reason").value = "";
    $(".dialog-err", dlg).textContent = "";
    dlg.showModal();
    $("#edit-value").select();
  });
  if (dlg) {
    $("#edit-cancel").addEventListener("click", () => dlg.close());
    $("#edit-form").addEventListener("submit", async ev => {
      ev.preventDefault();
      const value = $("#edit-value").value.trim(), reason = $("#edit-reason").value.trim();
      if (!reason) { $(".dialog-err", dlg).textContent = "A reason is mandatory."; return; }
      $("#edit-save").disabled = true;
      const { status, data } = await post(editing.dataset.url, { value, reason, version: editing.dataset.version });
      $("#edit-save").disabled = false;
      if (!data.ok) {
        $(".dialog-err", dlg).textContent = (status === 409 ? "⚠ " : "") + data.error;
        return;
      }
      dlg.close();
      window.location.reload();   // refresh grid, rule badges and verification state
    });
  }
  document.addEventListener("click", async e => {
    const b = e.target.closest(".js-history");
    if (!b) return;
    const res = await fetch(b.dataset.url, { headers: { "X-Requested-With": "fetch" } });
    const data = await res.json();
    const rows = data.history.map(h => `<tr><td>${h.ts.slice(0, 16).replace("T", " ")}</td><td>${h.full_name || ""}</td>` +
      `<td class="mono"><s>${h.old_value ?? ""}</s> → <b>${h.new_value ?? ""}</b></td><td>${h.reason}</td></tr>`).join("");
    $("#history-body").innerHTML = `<table class="tbl compact"><tr><th>When</th><th>Who</th><th>Change</th><th>Reason</th></tr>${rows}</table>`;
    $("#history-dialog").showModal();
  });

  // ---------------------------------------------------------------- status polling (every 10 s)
  $$("[data-poll]").forEach(el => {
    const note = $(".poll-note", el);
    let version = String(el.dataset.version);
    async function tick() {
      try {
        const res = await fetch(el.dataset.poll, { headers: { "X-Requested-With": "fetch" } });
        if (!res.ok) return;
        const s = await res.json();
        if (String(s.version) !== version) {
          note.classList.add("changed");
          note.innerHTML = `⚠ This job was changed by ${s.updated_by || "another user"} at ${(s.updated_at || "").slice(11, 16)} - now <b>${s.label}</b>${s.locked ? " (locked)" : ""}. <a href="">Reload</a> before editing.`;
        } else {
          note.textContent = `Live status: ${s.label}${s.locked ? " · 🔒 locked" : ""} · checked ${new Date().toLocaleTimeString()}`;
        }
      } catch (e) { /* offline - try again next tick */ }
    }
    tick();
    setInterval(tick, 10000);
  });

  // ---------------------------------------------------------------- integrity verification
  document.addEventListener("click", async e => {
    const b = e.target.closest(".js-verify");
    if (!b) return;
    const box = b.closest(".verify-box"), out = $(".verify-result", box);
    out.textContent = "Recomputing hashes…";
    const { data } = await post(box.dataset.url, {});
    if (!data.ok) { out.textContent = data.error; return; }
    out.innerHTML = `<div class="verdict ${data.valid ? "good" : "bad"}">${data.valid ? "✅ Report is intact - all hashes match." : "❌ Integrity check FAILED"}</div>` +
      "<ul>" + data.checks.map(c => `<li>${c.ok ? "✅" : "❌"} ${c.label}</li>`).join("") + "</ul>";
  });

  // ---------------------------------------------------------------- audit chain verify
  const vc = $("#verify-chain");
  if (vc) vc.addEventListener("click", async () => {
    const { data } = await post(vc.dataset.url, {});
    $("#chain-result").innerHTML = `<div class="verdict ${data.ok ? "good" : "bad"}">${data.ok
      ? `✅ Audit chain intact - ${data.rows} rows verified.` : `❌ Chain broken at row #${data.bad_id}`}</div>`;
  });

  // ---------------------------------------------------------------- admin live preview
  const lp = $("#load-preview");
  if (lp) lp.addEventListener("click", () => {
    const fr = $("#preview-frame");
    fr.hidden = false;
    lp.textContent = "Generating… (a few seconds)";
    fr.onload = () => (lp.textContent = "↻ Refresh preview");
    fr.src = lp.dataset.src + "?t=" + Date.now();
  });
})();
