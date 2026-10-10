# CPRI ATRG - Automated Test Report Generation

Hackathon prototype for the Short Circuit Laboratory, CPRI Bengaluru. It is a web application for the
local network. It takes a power-transformer test job from the customer's request to a sealed, tamper-evident
test report in the CPRI format.

- four portals: Customer, Test Engineer, Test In-charge (admin) and Head of Division (HOD)
- every admin action that matters takes effect only after HOD approval; only the HOD's authorization issues a report
- any document - PDF, photo, Excel in any layout, CSV - is read automatically (Google Gemini, optional) at every
  upload point: request form, logsheets per test or all at once, work instruction; ATRG's own Excel templates
  are read without AI. Every value is shown beside its source and stays unconfirmed until a person checks it
- readings come in through Excel logsheets generated from a field registry, or from scanned / foreign documents
- calculations and IS 1180 rule checks are declared in YAML and evaluated safely
- report formats are data: upload a revised DOCX and it is mapped automatically, reviewed, approved and used
- atomic numbering (`HVD25S0847`, `CPRIBLRSCL25T1654`), optimistic locking, SHA-256 sealing,
  database triggers that freeze issued data, and a hash-chained activity log

## 1. Install

Python 3.10 or newer on Windows (macOS and Linux also work).

```bash
python -m venv .venv
```
```bash
.venv\Scripts\activate
```
```bash
pip install -r requirements.txt
```

**PDF export** is detected at start-up: LibreOffice (`soffice`) when installed, otherwise Microsoft Word
(as `docx2pdf` does). When neither is available, reports are served as DOCX with a notice. The result is
shown on the HOD system page.

## 2. Configure (optional)

```bash
copy .env.example .env
```

| Setting | Meaning |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key (kept only in `.env`, which is git-ignored; diagnostics show the last 4 characters). Leave empty to run fully offline with manual entry. |
| `AI_ENABLED` | `false` guarantees that no document is ever sent to Gemini, even with a key; users see "Automatic reading is turned off by the administrator". |
| `GEMINI_USE_VERTEX` | `auto` (default) tries AI Studio, then Vertex AI; `true` / `false` forces one. |
| `GEMINI_MODEL_FAST`, `GEMINI_MODEL_PRO` | Leave empty to use the newest Flash / Pro model the key can reach (found at start-up), or set model ids. |

Check the connection and see what is read from a document:

```bash
python manage.py check-ai
```
```bash
python manage.py check-ai --file reference/Logsheet_for_short_circuit.pdf --kind logsheet:sc
```

`python tools/ai_accuracy.py` reads all reference documents and writes the accuracy table to
`docs/ai_accuracy.md`.
| `ATRG_PORT` | Port (default 5000). |

## 3. Run

```bash
python seed.py --reset
```
```bash
python run.py
```

`seed.py` creates the demo accounts and registers report template v1. It also runs the real workflow for the
demo transformer twice: `CPRIBLRSCL25T1654` is issued and sealed, and `CPRIBLRSCL25T1655` is waiting in technical
review. `run.py` starts waitress on `0.0.0.0:5000` with 8 threads and prints the local and LAN addresses.

### Demo accounts

| Portal | Username | Password | Notes |
|---|---|---|---|
| HOD | `hod` | `hod12345` | approves admin actions and authorizes reports |
| Test In-charge | `admin` | `admin12345` | |
| Test In-charge | `admin2` | `admin23456` | waiting for HOD approval (in the HOD inbox) |
| Engineer | `engineer1` | `eng12345` | |
| Engineer | `engineer2` | `eng23456` | waiting for admin approval |
| Customer | `customer1` | `cust12345` | A.P. Transformers |

Account rules: customers are active immediately; engineers wait for an admin; admins wait for the HOD.
There is no public HOD registration. Create the first HOD with `python manage.py create-hod`; an HOD can
create further HODs on the Accounts page. Passwords need at least 8 characters.

## 4. Use from other computers (LAN)

1. Connect all computers to the same Wi-Fi or LAN. The laptop running `run.py` is the server.
2. Allow the port through Windows Firewall on the server (PowerShell as Administrator):

```bash
netsh advfirewall firewall add rule name="CPRI ATRG 5000" dir=in action=allow protocol=TCP localport=5000
```

3. Open the LAN address printed by `run.py` (for example `http://192.168.1.23:5000`) on the other computers.

Each role uses its own session key, so one browser can be signed in to all four portals in separate tabs.

## 5. Three-minute demo script

| Time | Who | Do |
|---|---|---|
| 0:00 | HOD | `/hod`: show the approval inbox (pending admin account) and Settings. Switch on "Accept test request" so it also needs the HOD. |
| 0:20 | Customer | `/customer`, New request. Upload `reference/Customer_request_form.pdf`. Press Read document: with a Gemini key the form fills itself beside the scan; "Please check" fields are outlined amber and focusing a field highlights its source. Without a key, read `sample_data/customer_request_filled.xlsx` the same way (no AI needed). Select all tests, accept the declaration, submit. |
| 0:50 | Admin | `/admin`: open the request, Accept (goes to the HOD); HOD approves; admin assigns P. Naveenkumar. |
| 1:10 | Engineer | `/engineer`: on each tab upload the matching `sample_data/*.xlsx`. Rule checks turn to Pass. Correct one value with a reason, mark each test verified, Lock job (confirmation states what is frozen). |
| 1:50 | Admin | Technical review: rule results with limits, correction history, Generate preview, Forward for authorization. |
| 2:10 | HOD | Approvals: open the request, open the preview, Authorize and issue. The report is sealed. |
| 2:25 | Customer | Download the PDF, Verify integrity. HOD: Activity log, Verify chain. |
| 2:40 | Admin | Report templates: upload a revised format (`e2e_output/Transformer_Test_report_format_revised.docx`, created by the end-to-end test). About 100 % is mapped automatically; send it to the HOD, who activates it. Forward `CPRIBLRSCL25T1655`; the HOD authorizes; the report uses the new layout. |

## 6. How to change the report format

1. Prepare the new DOCX in Word. Leave value cells empty or put `XXXX` where values go. Keep "TEST REPORT"
   at the top of each sheet, or set another sheet marker in the template settings.
2. Admin portal, **Report templates**, upload it. The system parses it and maps every value place to the
   field registry. It reuses the mapping of the active version, matches labels by fuzzy comparison and can
   optionally ask Gemini. It then compiles the template and makes a dry run with the demo transformer.
3. Open the draft. Green places are mapped, amber ones have low confidence, red ones are required and
   unmapped. Click a place to bind it to a field, a fixed text or a safe expression. Add or adjust
   **sections**: anchored by text, a section is printed only when its tests are requested. Sheet numbers,
   the schedule of tests and the document counts are computed automatically.
4. **Compile and dry run** until no place is left unfilled, then **Send to HOD for activation**.
5. After HOD approval the new version is used for every report generated from then on. Issued reports keep
   the template id, version and hash they were made with; retired templates are never deleted.

A new value that does not exist yet is added under **Field registry** (also HOD-approved). A new reading
appears automatically in the Excel logsheet of its station. A computed value is defined with a formula.
New rule checks go into `mappings/rules.yaml`.

To rebuild template v1 from the reference format: `python manage.py import-template`
(review decisions are in `report_templates/v1/review.yaml`).

## 7. Tests

```bash
python -m pytest
```

Covers numbering under 50 concurrent allocations, the sealed-record database triggers, the audit chain and
tamper detection, and the Excel round-trip. It also covers report completeness and key values (HV rise
39.7 °C, ΔX −0.163 %, NLC 1.28 %), approval enforcement (an admin can not issue without the HOD, an engineer
can not edit after lock, optional approval toggles), registry changes reaching the Excel template, template
adaptability, the rules, and document intake (own templates are read with a client that raises, so no AI
call is possible; `AI_ENABLED=false` falls back to manual entry). The live accuracy test runs only with a key
and `set ATRG_TEST_AI=true`.

End-to-end browser run (server running, after `python seed.py --reset`):

```bash
python -m playwright install chromium
```
```bash
python tests/e2e/e2e_flow.py
```

With `set ATRG_E2E_AI=true` it also reads the scanned request form, the scanned routine logsheet and a
foreign-layout workbook and saves the review screens (`check1/3/5_*.png`). The run covers the whole flow,
including the template change, and writes screenshots of every main screen
at 1440×900 and 1280×720, plus the issued PDF pages, to `e2e_output/`.

## 8. Project layout

```
run.py  manage.py  config.py  seed.py  requirements.txt  .env.example
app/
  __init__.py  db.py  schema.sql  auth.py  workflow.py  approvals.py  approval_types.py  notify.py
  numbering.py  jobs.py  excel_io.py  rules.py  report.py  audit.py  integrity.py  fields.py  request_form.py
  ai/         client.py  intake.py  template_mapper.py  prompts/ (versioned .md)
  reporting/  docx_parser.py  automapper.py  sections.py  compiler.py  generator.py  registry.py  templates.py
  routes/     public.py  customer.py  engineer.py  admin.py  hod.py  api.py  common.py
  templates/  static/
mappings/          fields.yaml (registry seed)  tests.yaml (test catalogue)  rules.yaml
report_templates/  v1/ source.docx  review.yaml  mapping.json  compiled.docx  dry_run.docx
tools/             import_template.py  make_excel_templates.py
sample_data/       filled logsheets + demo request for transformer Sl. No. 1098
reference/         the scanned CPRI formats and the original report template
docs/              PLAN.md  architecture.md
tests/             pytest suite + e2e/e2e_flow.py
```

See [docs/architecture.md](docs/architecture.md) for the module, data-flow and approval-flow diagrams.
