"""
Generate the Excel logsheet templates and the demo sample data.

    python tools/make_excel_templates.py

Outputs
    templates/excel/<station>.xlsx          blank templates with {{key}} tokens + defined names
    templates/excel/customer_request_template.xlsx
    sample_data/<station>.xlsx              filled with the real demo transformer readings
    sample_data/customer_request_filled.xlsx

Everything is driven by mappings/fields.yaml (layout hints: sheet/section/row/col)
and sample_data/demo_values.yaml.
"""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from app import excel_io, fields, request_form  # noqa: E402
from openpyxl.workbook.defined_name import DefinedName  # noqa: E402,F401

# Series no. / sample code are left blank in the sample files so they can be uploaded to any demo job
# (ATRG refuses a logsheet whose header sample code belongs to a different job).
DEMO_HEADER = {"series_no": "", "sample_code": "", "customer": "M/s. A.P. Transformers, Chennai",
               "rating": "250 kVA, 11000/433 V, 3-phase", "serial_no": "1098"}


def main():
    os.makedirs(config.EXCEL_TEMPLATE_DIR, exist_ok=True)
    os.makedirs(config.SAMPLE_DATA_DIR, exist_ok=True)
    with open(os.path.join(config.SAMPLE_DATA_DIR, "demo_values.yaml"), encoding="utf-8") as fh:
        demo = yaml.safe_load(fh)

    for station, meta in fields.stations().items():
        wb = excel_io.build_station_template(station)
        out = os.path.join(config.EXCEL_TEMPLATE_DIR, meta["file"])
        wb.save(out)
        print("template ", out)
        # sample data = template + header + demo values
        wb = excel_io.build_station_template(station)
        excel_io.fill_header(wb, station, DEMO_HEADER)
        vals = {k: v for k, v in demo.items() if fields.field(k) and fields.field(k)["station"] == station}
        excel_io.fill_values(wb, vals)
        # unused optional cells: clear the token so the sheet looks clean
        for f in fields.station_fields(station):
            if f["key"] not in vals and ".hdr." not in f["key"]:
                excel_io.fill_values(wb, {f["key"]: None})
        excel_io.fill_values(wb, {f"{station}.hdr.{k}": (v or None) for k, v in DEMO_HEADER.items()})
        out = os.path.join(config.SAMPLE_DATA_DIR, meta["file"])
        wb.save(out)
        print("sample   ", out, len(vals), "values")

    wb = excel_io.build_request_template(request_form.SECTIONS)
    wb.save(os.path.join(config.EXCEL_TEMPLATE_DIR, "customer_request_template.xlsx"))
    # filled request for the demo
    wb = excel_io.build_request_template(request_form.SECTIONS)
    ws = wb["Request"]
    d = request_form.demo_form()
    values = {}
    for k, v in d.items():
        if k == "drawings":
            for i, dr in enumerate(v, start=1):
                values[f"drawing{i}_number"], values[f"drawing{i}_sheet"], values[f"drawing{i}_rev"] = dr["number"], dr["sheet"], dr["rev"]
        else:
            values[k] = v
    for code in fields.tests():
        values[f"test_{code}"] = "Yes"
    for name, v in values.items():
        dn = wb.defined_names.get("req_" + name)
        if dn is None:
            continue
        title, coord = next(iter(dn.destinations))
        ws[coord.replace("$", "")].value = v
    wb.save(os.path.join(config.SAMPLE_DATA_DIR, "customer_request_filled.xlsx"))
    print("request templates written")


if __name__ == "__main__":
    main()
