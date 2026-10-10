-- =====================================================================
-- ATRG database schema (SQLite, WAL mode)
--
-- Integrity design:
--   * every editable record carries `version` for optimistic locking
--   * once a job is sealed (report issued) BEFORE triggers ABORT any
--     INSERT/UPDATE/DELETE that touches the job or its child rows -
--     this is enforced by the database itself, not only by Python code
--   * reports, uploaded files, corrections and the audit log are
--     append-only (UPDATE/DELETE always aborted)
-- =====================================================================

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('customer', 'engineer', 'admin', 'hod')),
    full_name     TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    company       TEXT,
    address       TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'disabled')),
    created_at    TEXT NOT NULL,
    approved_by   INTEGER REFERENCES users(id),
    approved_at   TEXT
);

-- One row per customer test request. After acceptance the same row is
-- the "job" that engineers work on and that is finally sealed.
CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES users(id),
    ref            TEXT UNIQUE,          -- REQ-2026-0042 (customer-facing reference)
    status         TEXT NOT NULL,
    template_id    INTEGER,              -- report template used when the report was generated
    sample_code    TEXT,                 -- HVD25S0847 (allocated on accept)
    report_no      TEXT,                 -- CPRIBLRSCL25T1654 (allocated on accept)
    amends_id      INTEGER REFERENCES requests(id),   -- amended report -> original job
    amendment_no   INTEGER NOT NULL DEFAULT 0,
    form_json      TEXT NOT NULL,        -- customer request form + proforma
    tests_json     TEXT NOT NULL,        -- list of requested test codes
    condition_json TEXT,                 -- sheet 3: physical condition / capability check
    wi_json        TEXT,                 -- work instruction (CPRI/QAF/02)
    reject_reason  TEXT,
    return_comment TEXT,
    created_at     TEXT NOT NULL,
    accepted_at    TEXT,
    accepted_by    INTEGER REFERENCES users(id),
    assigned_at    TEXT,
    locked_at      TEXT,
    issued_at      TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    updated_by     INTEGER REFERENCES users(id),
    updated_at     TEXT,
    is_sealed      INTEGER NOT NULL DEFAULT 0
);
-- numbers are never reused (amendments share the original sample code)
CREATE UNIQUE INDEX IF NOT EXISTS ux_requests_sample_code ON requests(sample_code) WHERE sample_code IS NOT NULL AND amends_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_requests_report_no ON requests(report_no) WHERE report_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS ix_requests_customer ON requests(customer_id);

CREATE TABLE IF NOT EXISTS job_engineers (
    job_id      INTEGER NOT NULL REFERENCES requests(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL CHECK (role IN ('responsible', 'additional')),
    station     TEXT NOT NULL DEFAULT '',        -- '' = all stations
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (job_id, user_id)
);

-- Every stored file (customer documents, engineer Excel uploads, reports).
-- Immutable: content is hashed and rows can never be changed.
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER REFERENCES requests(id),
    kind          TEXT NOT NULL,     -- request_document | logsheet | logsheet_scan | template
    station       TEXT,
    original_name TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL,
    uploaded_by   INTEGER REFERENCES users(id),
    uploaded_at   TEXT NOT NULL
);

-- Imported readings. imported_value is what the Excel file said and is
-- never edited by hand; current_value is what goes into the report.
CREATE TABLE IF NOT EXISTS readings (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES requests(id),
    station        TEXT NOT NULL,
    key            TEXT NOT NULL,
    imported_value TEXT,
    current_value  TEXT,
    unit           TEXT,
    source         TEXT NOT NULL DEFAULT 'excel',   -- excel | ai | manual
    source_file_id INTEGER REFERENCES files(id),
    source_cell    TEXT,
    ai_confidence  REAL,
    verified_by    INTEGER REFERENCES users(id),    -- set when an AI-extracted value is confirmed
    verified_at    TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    updated_by     INTEGER REFERENCES users(id),
    updated_at     TEXT,
    UNIQUE (job_id, key)
);

-- Append-only history of every manual correction / re-import.
CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES requests(id),
    reading_id INTEGER NOT NULL REFERENCES readings(id),
    key        TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    reason     TEXT NOT NULL,
    user_id    INTEGER REFERENCES users(id),
    ts         TEXT NOT NULL
);

-- Per-station verification flag set by the engineer.
CREATE TABLE IF NOT EXISTS job_tests (
    job_id      INTEGER NOT NULL REFERENCES requests(id),
    station     TEXT NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0,
    verified_by INTEGER REFERENCES users(id),
    verified_at TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (job_id, station)
);

-- Results of app/rules.py (recomputed after every save).
CREATE TABLE IF NOT EXISTS test_results (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES requests(id),
    station     TEXT NOT NULL,
    rule_code   TEXT NOT NULL,
    label       TEXT NOT NULL,
    value       TEXT,
    limit_text  TEXT,
    formula     TEXT,
    passed      INTEGER,          -- 1 pass, 0 fail, NULL = calculated value only
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_test_results_job ON test_results(job_id);

-- Issued (sealed) reports.
CREATE TABLE IF NOT EXISTS reports (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL REFERENCES requests(id),
    report_no        TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL DEFAULT 'original',   -- original | amended
    amends_report_id INTEGER REFERENCES reports(id),
    snapshot_json    TEXT NOT NULL,
    sha256_snapshot  TEXT NOT NULL,
    sha256_docx      TEXT NOT NULL,
    sha256_pdf       TEXT,
    docx_path        TEXT NOT NULL,
    pdf_path         TEXT,
    template_id      INTEGER,
    template_version INTEGER,
    template_sha     TEXT,
    issued_by        INTEGER REFERENCES users(id),     -- HOD who authorised
    forwarded_by     INTEGER REFERENCES users(id),     -- admin who forwarded
    issued_at        TEXT NOT NULL
);


-- Admin actions that need HOD sign-off. Applied only when approved.
CREATE TABLE IF NOT EXISTS approval_requests (
    id           INTEGER PRIMARY KEY,
    type         TEXT NOT NULL,
    entity_type  TEXT,
    entity_id    TEXT,
    summary      TEXT,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'failed')),
    requested_by INTEGER REFERENCES users(id),
    requested_at TEXT NOT NULL,
    decided_by   INTEGER REFERENCES users(id),
    decided_at   TEXT,
    comment      TEXT,
    result_json  TEXT,
    version      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approval_requests(status);
CREATE INDEX IF NOT EXISTS ix_approvals_entity ON approval_requests(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),   -- a specific user, or NULL + role for everyone in a role
    role       TEXT,
    message    TEXT NOT NULL,
    link       TEXT,
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, read_at);

-- Field registry (seeded from mappings/fields.yaml). `rev` increments on every approved change.
CREATE TABLE IF NOT EXISTS field_registry (
    key          TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    unit         TEXT,
    dtype        TEXT NOT NULL DEFAULT 'number',
    dp           INTEGER,
    source       TEXT NOT NULL,
    test_code    TEXT,
    station      TEXT,
    required     INTEGER NOT NULL DEFAULT 0,
    formula      TEXT,
    stage        TEXT,
    ui           TEXT,
    options_json TEXT,
    section      TEXT,
    sheet        TEXT,
    row_label    TEXT,
    col_label    TEXT,
    report_label TEXT,
    multi        INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    extra_json   TEXT
);

CREATE TABLE IF NOT EXISTS test_catalogue (
    code           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    grp            TEXT NOT NULL CHECK (grp IN ('special', 'type', 'routine')),
    clause         TEXT,
    standard       TEXT,
    station        TEXT,
    excel_template TEXT,
    report_section TEXT,
    display_order  INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stations (
    code     TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    logsheet TEXT,
    file     TEXT
);

-- Background AI jobs (Gemini). The UI polls /api/ai/tasks/<id>.
CREATE TABLE IF NOT EXISTS ai_tasks (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,             -- request_form | logsheet | template_mapping
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'disabled')),
    input_json  TEXT,
    result_json TEXT,
    error       TEXT,
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

-- Traceability of every AI call (raw response, model, prompt version, latency).
CREATE TABLE IF NOT EXISTS ai_extractions (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER REFERENCES ai_tasks(id),
    kind           TEXT NOT NULL,
    file_id        INTEGER REFERENCES files(id),
    model          TEXT,
    prompt_version TEXT,
    latency_ms     INTEGER,
    tokens         INTEGER,
    raw_response   TEXT,
    created_at     TEXT NOT NULL
);

-- Versioned report templates. Never deleted; an active/retired template is frozen.
CREATE TABLE IF NOT EXISTS report_templates (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    equipment_type TEXT NOT NULL,
    version        INTEGER NOT NULL,
    source_path    TEXT NOT NULL,
    source_docx_sha TEXT NOT NULL,
    compiled_path  TEXT,
    compiled_sha   TEXT,
    mapping_json   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'pending_hod', 'active', 'retired')),
    notes          TEXT,
    created_by     INTEGER REFERENCES users(id),
    created_at     TEXT NOT NULL,
    approved_by    INTEGER REFERENCES users(id),
    approved_at    TEXT,
    version_row    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (equipment_type, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_template ON report_templates(equipment_type) WHERE status = 'active';

-- Atomic per-year counters for sample codes and report numbers.
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT NOT NULL,
    year  INTEGER NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (name, year)
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by INTEGER,
    updated_at TEXT
);

-- Hash-chained, append-only activity log.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    user_id     INTEGER,
    username    TEXT,
    role        TEXT,
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    before_json TEXT,
    after_json  TEXT,
    ip          TEXT,
    user_agent  TEXT,
    prev_hash   TEXT NOT NULL,
    row_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log(entity_type, entity_id);

-- =====================================================================
-- Triggers: sealed-record protection
-- =====================================================================
CREATE TRIGGER IF NOT EXISTS trg_requests_sealed_upd BEFORE UPDATE ON requests
WHEN OLD.is_sealed = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: job is sealed - an issued report can not be modified'); END;

CREATE TRIGGER IF NOT EXISTS trg_requests_sealed_del BEFORE DELETE ON requests
WHEN OLD.is_sealed = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: job is sealed - an issued report can not be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_readings_sealed_ins BEFORE INSERT ON readings
WHEN (SELECT is_sealed FROM requests WHERE id = NEW.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: readings of a sealed job can not be added'); END;

CREATE TRIGGER IF NOT EXISTS trg_readings_sealed_upd BEFORE UPDATE ON readings
WHEN (SELECT is_sealed FROM requests WHERE id = OLD.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: readings of a sealed job can not be modified'); END;

CREATE TRIGGER IF NOT EXISTS trg_readings_sealed_del BEFORE DELETE ON readings
WHEN (SELECT is_sealed FROM requests WHERE id = OLD.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: readings of a sealed job can not be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_results_sealed_ins BEFORE INSERT ON test_results
WHEN (SELECT is_sealed FROM requests WHERE id = NEW.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: results of a sealed job can not be added'); END;

CREATE TRIGGER IF NOT EXISTS trg_results_sealed_upd BEFORE UPDATE ON test_results
WHEN (SELECT is_sealed FROM requests WHERE id = OLD.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: results of a sealed job can not be modified'); END;

CREATE TRIGGER IF NOT EXISTS trg_results_sealed_del BEFORE DELETE ON test_results
WHEN (SELECT is_sealed FROM requests WHERE id = OLD.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: results of a sealed job can not be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_jobtests_sealed_ins BEFORE INSERT ON job_tests
WHEN (SELECT is_sealed FROM requests WHERE id = NEW.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: job is sealed'); END;

CREATE TRIGGER IF NOT EXISTS trg_jobtests_sealed_upd BEFORE UPDATE ON job_tests
WHEN (SELECT is_sealed FROM requests WHERE id = OLD.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: job is sealed'); END;

CREATE TRIGGER IF NOT EXISTS trg_jobeng_sealed_ins BEFORE INSERT ON job_engineers
WHEN (SELECT is_sealed FROM requests WHERE id = NEW.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: job is sealed'); END;

CREATE TRIGGER IF NOT EXISTS trg_jobeng_sealed_del BEFORE DELETE ON job_engineers
WHEN (SELECT is_sealed FROM requests WHERE id = OLD.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: job is sealed'); END;

CREATE TRIGGER IF NOT EXISTS trg_corrections_sealed_ins BEFORE INSERT ON corrections
WHEN (SELECT is_sealed FROM requests WHERE id = NEW.job_id) = 1
BEGIN SELECT RAISE(ABORT, 'SEALED: job is sealed'); END;

-- append-only tables -------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_reports_no_upd BEFORE UPDATE ON reports
BEGIN SELECT RAISE(ABORT, 'SEALED: issued reports are immutable - create an amended report'); END;

CREATE TRIGGER IF NOT EXISTS trg_reports_no_del BEFORE DELETE ON reports
BEGIN SELECT RAISE(ABORT, 'SEALED: issued reports can not be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_files_no_upd BEFORE UPDATE ON files
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: stored files can not be modified'); END;

CREATE TRIGGER IF NOT EXISTS trg_files_no_del BEFORE DELETE ON files
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: stored files can not be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_corrections_no_upd BEFORE UPDATE ON corrections
BEGIN SELECT RAISE(ABORT, 'APPEND-ONLY: corrections can not be modified'); END;

CREATE TRIGGER IF NOT EXISTS trg_corrections_no_del BEFORE DELETE ON corrections
BEGIN SELECT RAISE(ABORT, 'APPEND-ONLY: corrections can not be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_templates_no_del BEFORE DELETE ON report_templates
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: report templates are never deleted - retire them instead'); END;

CREATE TRIGGER IF NOT EXISTS trg_templates_frozen BEFORE UPDATE OF mapping_json, compiled_path, compiled_sha, source_path, source_docx_sha ON report_templates
WHEN OLD.status IN ('active', 'retired')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: an active or retired template can not be changed - create a new version'); END;

CREATE TRIGGER IF NOT EXISTS trg_approvals_no_del BEFORE DELETE ON approval_requests
BEGIN SELECT RAISE(ABORT, 'APPEND-ONLY: approval requests can not be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_upd BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'APPEND-ONLY: audit log can not be modified'); END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_del BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'APPEND-ONLY: audit log can not be deleted'); END;
