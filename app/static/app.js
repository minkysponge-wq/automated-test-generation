/* CPRI ATRG - plain JavaScript, no build step. */
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function post(url, body, isForm) {
    const opts = { method: "POST", headers: { "X-CSRF-Token": csrf, "X-Requested-With": "fetch" } };
    if (isForm) { opts.body = body; } else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body || {}); }
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { data = { ok: false, error: "Unexpected server response (" + res.status + ")" }; }
    return { status: res.status, data };
  }
  async function getJSON(url) {
    const res = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
    return res.json();
  }


  // ---------------------------------------------------------------- dismissible flash messages
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".flash-close");
    if (b) b.closest(".flash").remove();
  });

  // ---------------------------------------------------------------- clickable table rows
  document.addEventListener("click", (e) => {
    const tr = e.target.closest("tr.row-link");
    if (tr && !e.target.closest("a, button, input, form")) window.location = tr.dataset.href;
  });

  // ---------------------------------------------------------------- confirmation modal for irreversible actions
  const cdlg = $("#confirm-dialog");
  let pending = null;
  document.addEventListener("submit", (e) => {
    const form = e.target;
    const btn = e.submitter;
    const src = (btn && btn.dataset.confirm) ? btn : (form.dataset.confirm ? form : null);
    if (!src || !cdlg || form.dataset.confirmed === "1") return;
    e.preventDefault();
    pending = { form, btn };
    $("#confirm-title").textContent = src.dataset.confirmTitle || "Confirm";
    $("#confirm-text").textContent = src.dataset.confirm;
    cdlg.showModal();
  });
  if (cdlg) {
    $("#confirm-cancel").addEventListener("click", () => { cdlg.close(); pending = null; });
    $("#confirm-ok").addEventListener("click", () => {
      cdlg.close();
      if (!pending) return;
      const { form, btn } = pending;
      form.dataset.confirmed = "1";
      if (btn && btn.name) {           // keep the clicked button's name/value (e.g. decision=approve)
        const h = document.createElement("input");
        h.type = "hidden"; h.name = btn.name; h.value = btn.value; form.appendChild(h);
      }
      form.submit();
    });
  }

  // ---------------------------------------------------------------- tests: select all / group toggles
  const all = $("#select-all");
  function syncToggles() {
    $$(".js-group").forEach((g) => {
      const items = $$(`.js-test[data-group="${g.dataset.group}"]`);
      g.checked = items.length > 0 && items.every((i) => i.checked);
      g.indeterminate = !g.checked && items.some((i) => i.checked);
    });
    if (all) {
      const items = $$(".js-test");
      all.checked = items.every((i) => i.checked);
      all.indeterminate = !all.checked && items.some((i) => i.checked);
    }
  }
  if (all) {
    all.addEventListener("change", () => { $$(".js-test").forEach((i) => (i.checked = all.checked)); syncToggles(); });
    $$(".js-group").forEach((g) => g.addEventListener("change", () => {
      $$(`.js-test[data-group="${g.dataset.group}"]`).forEach((i) => (i.checked = g.checked)); syncToggles();
    }));
    $$(".js-test").forEach((i) => i.addEventListener("change", syncToggles));
    syncToggles();
  }

  // ---------------------------------------------------------------- drawings rows
  function drawingRow(d) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td><input name="drawing_number" aria-label="Drawing number"></td><td><input name="drawing_sheet" aria-label="Sheet number"></td>' +
      '<td><input name="drawing_rev" aria-label="Revision"></td><td><button type="button" class="btn btn-xs js-delrow">Remove</button></td>';
    if (d) { const ins = $$("input", tr); ins[0].value = d.number || ""; ins[1].value = d.sheet || ""; ins[2].value = d.rev || ""; }
    $("#drawings tbody").appendChild(tr);
  }
  $("#add-drawing")?.addEventListener("click", () => drawingRow());
  document.addEventListener("click", (e) => {
    if (!e.target.classList.contains("js-delrow")) return;
    const body = e.target.closest("tbody");
    if (body.rows.length > 1) e.target.closest("tr").remove(); else $$("input", e.target.closest("tr")).forEach((i) => (i.value = ""));
  });

  // ---------------------------------------------------------------- document reading (shared review component)
  function pollTaskUI(id, statusEl, onDone) {
    const timer = setInterval(async () => {
      let t;
      try { t = await getJSON("/api/ai/tasks/" + id); } catch (e) { return; }
      if (t.status === "queued") statusEl.textContent = "Reading document - waiting for a free reader";
      if (t.status === "running") statusEl.textContent = t.progress || "Reading document";
      if (t.status === "done" || t.status === "failed") { clearInterval(timer); onDone(t); }
    }, 1500);
  }
  // source viewer: page images or the spreadsheet as an HTML table; highlight the source of a value
  function showSource(view, data) {
    if (!view) return;
    view.innerHTML = (data.pages || []).map((u, i) => `<img src="${esc(u)}" alt="Page ${i + 1}" data-page="${i + 1}">`).join("") + (data.html || "");
  }
  function highlightSource(view, src) {
    if (!view || !src) return;
    view.querySelectorAll(".hl").forEach((x) => x.classList.remove("hl"));
    let el = null;
    const m = /pdf_page\s*(\d+)|page\s*(\d+)/i.exec(src);
    if (m) el = view.querySelector(`img[data-page="${m[1] || m[2]}"]`);
    else el = view.querySelector(`td[data-cell="${src.replace(/"/g, "")}"]`) ||
              Array.from(view.querySelectorAll("td[data-cell]")).find((td) => td.dataset.cell.endsWith("!" + src.split("!").pop()));
    if (el) { el.classList.add("hl"); el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" }); }
  }

  // ---- customer request form
  const form = $("#request-form");
  const custReader = $("#reader");
  const autoFilled = new Set();
  function mark(name, info) {
    const wrap = form && form.querySelector(`[data-field="${name}"]`);
    if (!wrap) return;
    wrap.classList.remove("check-field");
    wrap.querySelectorAll(".ai-mark").forEach((x) => x.remove());
    wrap.querySelectorAll("input, select, textarea").forEach((x) => x.classList.remove("read-field"));
    if (!info) return;
    const s = document.createElement("span");
    if (info === "missing") { s.className = "ai-mark missing"; s.textContent = "Not found"; wrap.appendChild(s); return; }
    if (info.check) {
      wrap.classList.add("check-field");
      s.className = "ai-mark check";
      s.textContent = `Please check - confidence ${Math.round(info.confidence * 100)}%` + (info.source ? `, ${info.source}` : "") +
        (info.evidence_text ? `: read as "${info.evidence_text}"` : "");
    } else {
      s.className = "ai-mark ok";
      s.textContent = "Read automatically" + (info.source ? ` (${info.source})` : "");
      wrap.querySelectorAll("input, select, textarea").forEach((x) => x.classList.add("read-field"));
    }
    wrap.dataset.source = info.source || "";
    wrap.appendChild(s);
  }
  function setField(name, value, info) {
    if (!form) return false;
    const els = $$(`[name="${name}"]`, form);
    if (!els.length) return false;
    els.forEach((el) => {
      if (el.type === "radio") el.checked = (el.value === value);
      else if (el.tagName === "SELECT") {
        const opt = Array.from(el.options).find((o) => o.value === value || o.text === value);
        if (opt) el.value = opt.value;
      } else el.value = value;
    });
    autoFilled.add(name);
    mark(name, info || { confidence: 1, check: false });
    return true;
  }
  function applyFill(data) {
    let n = 0, checks = 0;
    Object.entries(data.fields || {}).forEach(([name, v]) => {
      const info = (v && typeof v === "object") ? v : { value: v, confidence: 1 };
      if (info.value === null || info.value === undefined || info.value === "") return;
      if (setField(name, String(info.value), info)) { n++; if (info.check) checks++; }
    });
    let missing = 0;
    $$("[data-field]", form).forEach((w) => {
      const req = w.querySelector(".req");
      if (req && !autoFilled.has(w.dataset.field)) { mark(w.dataset.field, "missing"); missing++; }
    });
    if ((data.drawings || []).length) { $("#drawings tbody").innerHTML = ""; data.drawings.forEach((d) => drawingRow(d)); }
    if ((data.tests || []).length) $$(".js-test").forEach((i) => (i.checked = data.tests.includes(i.name.replace("test_", ""))));
    syncToggles();
    return { n, checks, missing };
  }
  if (custReader) {
    const status = $("#read-status"), summary = $("#read-summary");
    let fileId = null;
    async function run(useFile, forcePro) {
      const fd = new FormData();
      if (useFile) {
        const input = $("#read-file");
        if (!input.files.length) { status.textContent = "Choose the document first."; return; }
        fd.append("file", input.files[0]);
      } else fd.append("file_id", fileId);
      if (forcePro) fd.append("force_pro", "1");
      $$("#reader button").forEach((b) => (b.disabled = true));
      status.textContent = "Reading document";
      const { data } = await post(custReader.dataset.url, fd, true);
      if (!data.ok) { status.textContent = data.error; $("#read-btn").disabled = false; return; }
      fileId = data.file_id;
      $("#source_file_id").value = data.file_id;
      pollTaskUI(data.task_id, status, (t) => {
        $("#read-btn").disabled = false;
        if (t.status === "failed") { status.textContent = t.error || "Reading failed."; $("#reread-btn").disabled = false; return; }
        const r = t.result || {};
        $("#scan-layout").classList.remove("no-scan");
        $("#scan-panel").classList.remove("hidden");
        showSource($("#source-view"), r);
        $("#scan-summary").textContent = r.pages && r.pages.length ? `${r.pages.length} page(s)` : "spreadsheet";
        const { n, checks, missing } = applyFill(r);
        summary.textContent = `${n} fields read, ${checks} need checking, ${missing} not found.`;
        if (r.ai_skipped) status.textContent = r.ai_skipped + ". Please fill in the form.";
        else if (r.method === "template") status.textContent = "Read from the ATRG Excel request template (no AI). Review and submit.";
        else status.textContent = `Read by ${r.model || "Gemini"}${r.retry_model ? " (uncertain fields re-read with " + r.retry_model + ")" : ""}. Review every value, accept the declaration and submit.`;
        $("#reread-btn").disabled = !!r.ai_skipped || r.method === "template";
        $("#clear-btn").disabled = false;
      });
    }
    $("#read-btn").addEventListener("click", () => run(true, false));
    $("#reread-btn").addEventListener("click", () => run(false, true));
    $("#clear-btn").addEventListener("click", () => {
      autoFilled.forEach((name) => {
        $$(`[name="${name}"]`, form).forEach((el) => { if (el.type === "radio") el.checked = false; else el.value = ""; });
        mark(name, null);
      });
      $$("[data-field]", form).forEach((w) => mark(w.dataset.field, null));
      autoFilled.clear();
      summary.textContent = "";
      status.textContent = "Auto-filled values cleared.";
      $("#clear-btn").disabled = true;
    });
    form.addEventListener("focusin", (e) => {
      const w = e.target.closest("[data-field]");
      if (w && w.dataset.source) highlightSource($("#source-view"), w.dataset.source);
    });
  }

  // ---- engineer test tabs, multi-logsheet document, admin work-instruction cross-check
  $$(".reader").forEach((box) => {
    if (box.id === "reader") return;
    const status = box.querySelector(".read-status");
    async function run(useFile, forcePro) {
      const fd = new FormData();
      if (useFile) {
        const input = box.querySelector(".read-file");
        if (!input.files.length) { status.textContent = "Choose the document first."; return; }
        fd.append("file", input.files[0]);
      } else fd.append("file_id", box.dataset.file);
      if (box.dataset.station && box.dataset.station !== "wi") fd.append("station", box.dataset.station);
      if (forcePro) fd.append("force_pro", "1");
      box.querySelectorAll("button").forEach((b) => (b.disabled = true));
      status.textContent = "Reading document";
      const { data } = await post(box.dataset.url, fd, true);
      if (!data.ok) { status.textContent = data.error; box.querySelectorAll("button").forEach((b) => (b.disabled = false)); return; }
      pollTaskUI(data.task_id, status, (t) => {
        box.querySelectorAll("button").forEach((b) => (b.disabled = false));
        if (t.status === "failed") { status.textContent = t.error || "Reading failed."; return; }
        const r = t.result || {};
        if (box.dataset.station === "wi") return showWiCompare(box, r);
        if (r.ai_skipped) { status.textContent = r.ai_skipped + "."; return; }
        const s = r.summary || {};
        if (!r.parts && !s.read) {
          status.textContent = "No values for this test were found in that document. Is it the logsheet of another test? " +
            "Open the matching test tab, or use \"Read all logsheets\" at the top of the job to sort it automatically.";
          return;
        }
        status.textContent = r.parts ? `Sorted into ${Object.keys(r.parts).length} logsheet(s). Reloading.`
          : `${s.read} fields read, ${s.check} need checking, ${s.not_found} not found. Reloading.`;
        setTimeout(() => window.location.reload(), 900);
      });
    }
    box.querySelector(".read-btn")?.addEventListener("click", () => run(true, false));
    box.querySelector(".reread-btn")?.addEventListener("click", () => run(false, true));
  });
  function showWiCompare(box, r) {
    const wi = JSON.parse(box.querySelector(".wi-compare").dataset.wi || "{}");
    const rows = Object.entries(r.fields || {}).map(([k, v]) => {
      const sys = Array.isArray(wi[k]) ? wi[k].join("; ") : (wi[k] || "");
      const same = sys && String(v.value).replace(/\W/g, "").toLowerCase() === String(sys).replace(/\W/g, "").toLowerCase();
      return `<tr><td>${esc(k)}</td><td>${esc(v.value)}</td><td>${esc(sys)}</td><td><span class="badge ${same ? "green" : "amber"}">${same ? "Matches" : "Check"}</span></td></tr>`;
    }).join("");
    box.querySelector(".read-status").textContent = `${Object.keys(r.fields || {}).length} fields read from the scanned work instruction.`;
    box.querySelector(".wi-compare").innerHTML = `<table class="table compact"><thead><tr><th>Field</th><th>Scanned</th><th>In ATRG</th><th></th></tr></thead>${rows}</table>`;
  }
  // engineer source drawer
  const drawer = $("#source-drawer");
  let drawerUrl = null;
  async function openDrawer(url, src) {
    if (!drawer) return;
    if (drawerUrl !== url) {
      const d = await getJSON(url);
      $("#drawer-title").textContent = d.name || "Source document";
      showSource($("#drawer-body"), d);
      drawerUrl = url;
    }
    drawer.classList.remove("hidden");
    document.body.classList.add("drawer-open");
    if (src) setTimeout(() => highlightSource($("#drawer-body"), src), 60);
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".js-source, .js-goto");
    if (b) openDrawer(b.dataset.url, b.dataset.src);
  });
  $("#drawer-close")?.addEventListener("click", () => { drawer.classList.add("hidden"); document.body.classList.remove("drawer-open"); });

  // ---------------------------------------------------------------- engineer: correct a reading / history
  const dlg = $("#edit-dialog");
  let editing = null;
  document.addEventListener("click", (e) => {
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
    $("#edit-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const value = $("#edit-value").value.trim(), reason = $("#edit-reason").value.trim();
      if (!reason) { $(".dialog-err", dlg).textContent = "A reason is required."; return; }
      $("#edit-save").disabled = true;
      const { data } = await post(editing.dataset.url, { value, reason, version: editing.dataset.version });
      $("#edit-save").disabled = false;
      if (!data.ok) { $(".dialog-err", dlg).textContent = data.error; return; }
      dlg.close();
      window.location.reload();
    });
  }
  document.addEventListener("click", async (e) => {
    const b = e.target.closest(".js-history");
    if (!b) return;
    const data = await getJSON(b.dataset.url);
    const rows = data.history.map((h) => `<tr><td class="nowrap">${esc(h.ts.slice(0, 16).replace("T", " "))}</td><td>${esc(h.full_name || "")}</td>` +
      `<td class="num"><span class="was">${esc(h.old_value)}</span> to <strong>${esc(h.new_value)}</strong></td><td>${esc(h.reason)}</td></tr>`).join("");
    $("#history-body").innerHTML = `<table class="table compact"><thead><tr><th>When</th><th>Who</th><th class="num">Change</th><th>Reason</th></tr></thead>${rows}</table>`;
    $("#history-dialog").showModal();
  });

  // ---------------------------------------------------------------- live status polling (every 10 s)
  $$("[data-poll]").forEach((el) => {
    const note = $(".poll-note", el);
    if (!note) return;
    const version = String(el.dataset.version);
    async function tick() {
      try {
        const s = await getJSON(el.dataset.poll);
        if (String(s.version) !== version) {
          note.innerHTML = `This record was changed by ${esc(s.updated_by || "another user")} at ${esc((s.updated_at || "").slice(11, 16))}; ` +
            `it is now <strong>${esc(s.label)}</strong>. <a href="">Reload to continue.</a>`;
          note.style.color = "var(--amber)";
        } else {
          note.textContent = `Status: ${s.label}${s.locked ? " (locked)" : ""}. Checked at ${new Date().toLocaleTimeString()}.`;
        }
      } catch (e) { /* offline: try again */ }
    }
    tick();
    setInterval(tick, 10000);
  });

  // ---------------------------------------------------------------- integrity verification
  document.addEventListener("click", async (e) => {
    const b = e.target.closest(".js-verify");
    if (!b) return;
    const box = b.closest(".verify-box"), out = $(".verify-result", box);
    out.textContent = "Recomputing hashes...";
    const { data } = await post(box.dataset.url, {});
    if (!data.ok) { out.textContent = data.error; return; }
    out.innerHTML = `<div class="callout ${data.valid ? "good" : "bad"}" style="margin:8px 0">${data.valid ? "Verified: the report and its data are unchanged since issue." : "Mismatch: the integrity check failed."}</div>` +
      `<table class="table compact">${data.checks.map((c) => `<tr><td>${esc(c.label)}</td><td><span class="badge ${c.ok ? "green" : "red"}">${c.ok ? "Verified" : "Mismatch"}</span></td></tr>`).join("")}</table>`;
  });

  // ---------------------------------------------------------------- audit chain verification (HOD)
  const vc = $("#verify-chain");
  if (vc) vc.addEventListener("click", async () => {
    const { data } = await post(vc.dataset.url, {});
    $("#chain-result").innerHTML = `<div class="callout ${data.ok ? "good" : "bad"}">${data.ok
      ? `Audit chain verified: ${data.rows} entries, no gaps or edits.` : `Mismatch: the chain is broken at entry ${data.bad_id}.`}</div>`;
  });

  // ---------------------------------------------------------------- admin live preview
  const lp = $("#load-preview");
  if (lp) lp.addEventListener("click", async () => {
    const fr = $("#preview-frame");
    lp.disabled = true;
    lp.textContent = "Generating...";
    try {
      const res = await fetch(lp.dataset.src + "?t=" + Date.now());
      const blob = await res.blob();
      if (blob.type.includes("pdf")) { fr.src = URL.createObjectURL(blob); fr.classList.remove("hidden"); }
      else { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "preview.docx"; a.click(); }
      lp.textContent = "Refresh preview";
    } catch (e) { lp.textContent = "Preview failed - retry"; }
    lp.disabled = false;
  });

  // ---------------------------------------------------------------- template mapping editor
  const doc = $("#doc");
  const sdlg = $("#slot-dialog");
  let slotEl = null;
  function showKind(kind) {
    $$(".js-kind", sdlg).forEach((x) => x.classList.toggle("hidden", !x.classList.contains("js-kind-" + kind)));
  }
  if (doc && sdlg) {
    const editable = doc.dataset.editable === "1";
    function open(el) {
      if (!editable) return;
      slotEl = el;
      $("#slot-context").textContent = el.dataset.context;
      const kind = el.dataset.kind || "field";
      $$('input[name="slot_kind"]', sdlg).forEach((r) => (r.checked = r.value === kind));
      $("#slot-field").value = el.dataset.key || "";
      $("#slot-text").value = el.dataset.text || "";
      $("#slot-expr").value = el.dataset.expr || "";
      $("#slot-error").textContent = "";
      showKind(kind);
      sdlg.showModal();
    }
    doc.addEventListener("click", (e) => { const el = e.target.closest(".slot"); if (el) open(el); });
    doc.addEventListener("keydown", (e) => { const el = e.target.closest(".slot"); if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(el); } });
    $$('input[name="slot_kind"]', sdlg).forEach((r) => r.addEventListener("change", () => showKind(r.value)));
    $("#slot-cancel").addEventListener("click", () => sdlg.close());
    async function save(kind) {
      const body = { slot_id: slotEl.dataset.slot, fp: slotEl.dataset.fp, kind };
      if (kind === "field") body.key = $("#slot-field").value.trim();
      if (kind === "text") body.text = $("#slot-text").value;
      if (kind === "expr") body.expr = $("#slot-expr").value.trim();
      const { data } = await post(doc.dataset.slotUrl, body);
      if (!data.ok) { $("#slot-error").textContent = data.error; return; }
      sdlg.close();
      window.location.reload();
    }
    $("#slot-clear").addEventListener("click", () => save("clear"));
    $("#slot-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const kind = ($('input[name="slot_kind"]:checked', sdlg) || {}).value || "field";
      save(kind);
    });
  }
})();
