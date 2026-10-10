# ATRG v2 - build plan

v2 evolves the working v1 codebase (Flask + SQLite + docxtpl). It is not a rewrite: the proven pieces stay
(numbering, audit chain, sealing triggers, Excel importer, report rendering) and are generalised.
Work happens on the local `dev` branch and is not committed until the owner approves.

## What changes from v1

| Area | v1 | v2 |
|---|---|---|
| Roles | customer, engineer, admin | + **HOD** portal (`/hod`), approval chain customer → active, engineer → admin approves, admin → HOD approves, HOD via `manage.py create-hod` |
| Approvals | none | `approvals.py`: registry `{type: (validate, prepare, apply)}`, `approval_requests` table, HOD inbox, optional toggles |
| States | ... UNDER_REVIEW → ISSUED | ... LOCKED_BY_ENGINEER → **ADMIN_REVIEW → PENDING_HOD** → ISSUED (only an HOD approval seals) |
| Field registry | `mappings/fields.yaml` read directly | `field_registry` DB table seeded from YAML (readings, request, proforma, computed, system fields); editable by admin, activation HOD-approved; drives forms, Excel, AI schema, rules, report |
| Test catalogue | inside fields.yaml | `mappings/tests.yaml` → `test_catalogue` table |
| Rules | Python code | `mappings/rules.yaml` evaluated with **simpleeval** (no `eval`), computed fields as registry formulas |
| Report | one hand-built template | **versioned templates**: DOCX parser → auto-mapper (placeholders → previous-version fingerprints → rapidfuzz labels → optional Gemini) → mapping editor → compiler → generator; v1 built through the same pipeline by `tools/import_template.py`; adaptability test |
| AI | none | Gemini (google-genai) in a background pool (`ai_tasks`): request PDF reader (Pydantic schema, confidence, evidence), logsheet reader, template-mapping proposals; fully optional |
| UI | cards, emojis | institutional console: left sidebar, IBM Plex Sans, tables, no emojis, confirmation modals, print CSS |

## Data model additions
`users.role` + `hod`; `requests.ref` (REQ-YYYY-NNNN), `requests.template_id`; `approval_requests`; `notifications`;
`field_registry`; `test_catalogue`; `readings.source/ai_confidence/verified_by`; `ai_tasks`; `ai_extractions`;
`report_templates` (draft / pending_hod / active / retired, never deleted); `reports.template_id/version/sha`.
Triggers: sealed-job protection as v1, plus `report_templates` cannot be deleted and an active/retired template's
compiled file and mapping cannot change.

## Approval types
Always: `issue_report`, `template_activation`, `registry_change`, `amendment`, `admin_account`, `settings_change`.
Optional (HOD settings, default off): `reject_request`, `assign_engineers`, `return_job`, `accept_request`.
Generic flow: admin action → `approvals.submit()` (or direct apply when not required) → HOD approves → `prepare`
(outside the write lock, e.g. report generation) → `apply` inside `BEGIN IMMEDIATE` → audit + notification.

## Report template pipeline
1. **Parse** body/header/footer: blocks (paragraphs, tables), slots = empty value cells, `XXXX` dummies, `{{placeholders}}`,
   unit-suffixed inline numbers; each slot gets a context (table title, column header, row label, paragraph text) and a
   fingerprint. Sheets are found by a marker paragraph ("TEST REPORT").
2. **Auto-map**: exact placeholder → fingerprint of the active template's mapping → rapidfuzz against registry
   `report_label`s → Gemini proposals (optional). Loop tables (ratio taps, drawings) and the schedule-of-tests table are
   detected structurally.
3. **Edit**: HTML rendering of the document with slots highlighted; picker for field / text / expression / loop;
   sections (anchored by text, so reordering is safe) tied to test codes.
4. **Compile** to a docxtpl DOCX (`{{ S.slot }}`, `{%tr for %}`, `{%p if %}`), page break per sheet, computed
   "Sheet X of N", footer seal.
5. **Dry run** with demo data → list unfilled slots → submit to HOD → activate (previous active is retired).

## Build order (stages)
1. Schema, auth, 4 portals, approval engine, notifications.
2. Request form from registry + tracking; numbering + workflow with HOD states.
3. Field registry + test catalogue in DB; Excel templates generated live from the registry; importer (+source/AI flags).
4. Rules in YAML with simpleeval.
5. Reporting package (parser, auto-mapper, compiler, generator, sections) + v1 template via `tools/import_template.py`.
6. Sealing with template id/version/hash; audit additions; HOD dashboard and system status; backups.
7. AI: client + task pool, request reader, logsheet reader, template mapper.
8. UI polish with Playwright screenshots (1440x900, 1280x720).
9. Seed, pytest (incl. approvals, template adaptability, AI reader skip), Playwright E2E, README.

## Decisions on ambiguous points
- The E2E list has "admin accepts → HOD approves → admin assigns": implemented by the optional `accept_request`
  approval toggle, which the E2E switches on.
- Gemini models come from `.env` (`GEMINI_MODEL_FAST=gemini-3.8-flash`, `GEMINI_MODEL_PRO=gemini-3.1-pro-preview`,
  checked against Google's model list in October 2026). The SDK's `generate_content` with `response_mime_type` +
  `response_schema` is still fully supported and is used as the spec requires.
- The v1 customer-request Excel import is kept as an extra convenience next to the PDF and manual paths.
- Passwords: minimum 8 characters for every role.
