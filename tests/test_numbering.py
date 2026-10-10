"""Atomic numbering: 50 concurrent allocations must never collide."""
import threading

from app import db, numbering
from app.db import tx


def test_50_concurrent_allocations_are_unique(seeded):
    settings = dict(__import__("config").DEFAULT_SETTINGS)
    results, errors = [], []
    barrier = threading.Barrier(50)

    def worker():
        conn = db.connect()          # each thread = its own connection, like a separate request
        try:
            barrier.wait()
            with tx(conn):
                results.append((numbering.next_sample_code(conn, settings, 2031),
                                numbering.next_report_no(conn, settings, 2031)))
        except Exception as exc:     # pragma: no cover - failure path
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    samples = [s for s, _ in results]
    reports = [r for _, r in results]
    assert len(set(samples)) == 50 and len(set(reports)) == 50
    assert sorted(samples) == [f"HVD31S{n:04d}" for n in range(1, 51)]
    assert sorted(reports) == [f"CPRIBLRSCL31T{n:04d}" for n in range(1, 51)]


def test_number_formats_and_derived_numbers():
    s = dict(__import__("config").DEFAULT_SETTINGS)
    assert numbering.oscillogram_no("CPRIBLRSCL25T1654", 2) == "CPRIBLRSCL25T1654S002"
    assert numbering.graph_no("CPRIBLRSCL25T1654/A1", 1) == "CPRIBLRSCL25T1654G01"
    assert numbering.photo_no("CPRIBLRSCL25T1654", 4) == "CPRIBLRSCL25T1654P04"
    assert numbering.ulr_no(s, "CPRIBLRSCL25T1654") == "ULR-TC5452250SCLT1654F"


def test_allocation_requires_transaction(seeded):
    import pytest
    conn = db.connect()
    with pytest.raises(RuntimeError):
        numbering.allocate(conn, "sample_code", 2032)
    conn.close()


def test_demo_job_got_expected_numbers(seeded, conn):
    job = conn.execute("SELECT * FROM requests WHERE id=?", (seeded["job_id"],)).fetchone()
    assert job["sample_code"] == "HVD25S0847"
    assert job["report_no"] == "CPRIBLRSCL25T1654"
