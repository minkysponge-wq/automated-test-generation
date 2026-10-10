"""
Hashing and integrity verification for sealed reports.
"""
import hashlib
import json
import os
import stat


def canonical_json(obj):
    """Deterministic JSON: sorted keys, no whitespace -> stable hash."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_read_only(path):
    """Sealed files are stored read-only on disk."""
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def verify_report(conn, report):
    """
    Recompute every hash of an issued report and compare with what was sealed.
    Also re-builds the snapshot from the live database to prove the underlying
    data has not changed since issue.
    """
    from . import report as report_mod  # local import: report imports this module

    checks = []
    checks.append(("Snapshot JSON hash", sha256_text(report["snapshot_json"]) == report["sha256_snapshot"]))
    for label, path_key, hash_key in (("DOCX file hash", "docx_path", "sha256_docx"),
                                      ("PDF file hash", "pdf_path", "sha256_pdf")):
        path = report[path_key]
        if not path:
            continue
        ok = os.path.exists(path) and sha256_file(path) == report[hash_key]
        checks.append((label, ok))
    live = report_mod.build_snapshot(conn, report["job_id"], for_verify=True)
    sealed = json.loads(report["snapshot_json"])
    # compare only the data portion (issue metadata is added at sealing time)
    checks.append(("Database matches sealed snapshot", canonical_json(live["data"]) == canonical_json(sealed["data"])))
    job = conn.execute("SELECT is_sealed FROM requests WHERE id=?", (report["job_id"],)).fetchone()
    checks.append(("Job sealed flag set", bool(job and job["is_sealed"])))
    return {"ok": all(ok for _, ok in checks), "checks": checks}
