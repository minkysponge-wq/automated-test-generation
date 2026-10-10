"""
Optional Gemini proposals for report-template slots the deterministic auto-mapper left unmapped.
Input: the slots' label context + the registry fields that have a report label.
Output: [{slot_id, field_key, confidence}] - the admin still reviews them in the mapping editor.
"""
from typing import List, Optional

from pydantic import BaseModel

from . import client

PROMPT_VERSION = "template_mapping_v1"
SYSTEM = """You map the empty value places ("slots") of a transformer test report template to data fields.
Each slot is described by its row label, column header, table title and heading.
Choose the single best field key from the list for each slot, or null when no field fits.
Give a confidence 0..1. Never invent field keys."""


class Proposal(BaseModel):
    slot_id: str
    field_key: Optional[str] = None
    confidence: float = 0.0


class Proposals(BaseModel):
    proposals: List[Proposal]


def propose(slots, candidates, conn=None):
    if not client.available() or not slots:
        return []
    lines = ["Slots:"]
    for s in slots[:150]:
        c = s["context"]
        lines.append(f"- {s['id']}: row='{c.get('row', '')}' column='{c.get('col', '')}' "
                     f"title='{c.get('title', '')[:60]}' heading='{c.get('heading', '')[:60]}'")
    lines.append("Fields:")
    for c in candidates:
        lines.append(f"- {c['key']}: {c['label']} (report label: {c['row']} | {c['col']})")
    data, _meta = client.extract_json(["\n".join(lines)], Proposals, SYSTEM, "fast", PROMPT_VERSION, conn, None,
                                      "report_template")
    return data.get("proposals", [])
