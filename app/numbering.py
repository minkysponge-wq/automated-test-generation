"""
Automatic, atomic, never-reused numbering.

    CPRI sample code : {sample_prefix}{YY}S{NNNN}   e.g. HVD25S0847
    Test report no.  : {lab_code}{YY}T{NNNN}         e.g. CPRIBLRSCL25T1654
    Oscillograms     : {report_no}S001 ...  graphs {report_no}G01 ...  photos {report_no}P01 ...

Each sequence lives in the `counters` table (one row per name+year). The caller
MUST hold a write transaction (BEGIN IMMEDIATE via db.tx) so the
read-increment-write is serialised across threads and processes. Unique
indexes on requests.sample_code / requests.report_no are the last line of defence.
"""


def allocate(conn, name, year):
    """Increment and return the next value of counter (name, year). Requires an open transaction."""
    if not conn.in_transaction:
        raise RuntimeError("numbering.allocate() must run inside db.tx() (BEGIN IMMEDIATE)")
    cur = conn.execute("UPDATE counters SET value = value + 1 WHERE name=? AND year=?", (name, year))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO counters(name, year, value) VALUES (?,?,1)", (name, year))
        return 1
    return conn.execute("SELECT value FROM counters WHERE name=? AND year=?", (name, year)).fetchone()[0]


def next_sample_code(conn, settings, year):
    n = allocate(conn, "sample_code", year)
    return f"{settings['sample_prefix']}{year % 100:02d}S{n:04d}"


def next_report_no(conn, settings, year):
    n = allocate(conn, "report_no", year)
    return f"{settings['lab_code']}{year % 100:02d}T{n:04d}"


def base_report_no(report_no):
    """Strip an amendment suffix: CPRIBLRSCL25T1654/A1 -> CPRIBLRSCL25T1654."""
    return report_no.split("/")[0]


def oscillogram_no(report_no, n):
    return f"{base_report_no(report_no)}S{n:03d}"


def graph_no(report_no, n):
    return f"{base_report_no(report_no)}G{n:02d}"


def photo_no(report_no, n):
    return f"{base_report_no(report_no)}P{n:02d}"


def ulr_no(settings, report_no):
    """NABL ULR printed in the footer: ULR-TC5452 + YY + 0 + SCLT + NNNN + F."""
    base = base_report_no(report_no)
    yy, seq = base[len(settings["lab_code"]):].split("T")
    return f"{settings['ulr_prefix']}{yy}0SCLT{seq}F"
