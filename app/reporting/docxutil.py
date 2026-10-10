"""
Low-level python-docx / lxml helpers shared by the parser and the compiler.
All edits keep the formatting (run properties) of the original text.
"""
import copy
import re

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
MC_ALT = "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"


def own_t_nodes(p):
    """w:t nodes that belong to paragraph p itself (not to nested text-box paragraphs)."""
    return [t for t in p.iter(qn("w:t")) if next(t.iterancestors(qn("w:p")), None) is p]


def p_text(p):
    return "".join(t.text or "" for t in own_t_nodes(p))


def all_text(el):
    return "".join(t.text or "" for t in el.iter(qn("w:t")))


def norm(text):
    """Normalised text for fingerprints and fuzzy matching."""
    t = (text or "").replace("º", "°").replace("–", "-").replace("’", "'").replace(" ", " ")
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def regex_replace_p(p, pattern, repl, count=0, only_occurrence=None):
    """
    Regex replace across the runs of one paragraph, keeping the formatting of the
    run where each match starts. `only_occurrence` replaces just the n-th match (0-based).
    """
    nodes = own_t_nodes(p)
    if not nodes:
        return 0
    full = "".join(t.text or "" for t in nodes)
    matches = list(re.finditer(pattern, full))
    if only_occurrence is not None:
        matches = matches[only_occurrence:only_occurrence + 1]
    elif count:
        matches = matches[:count]
    for m in reversed(matches):
        new = m.expand(repl) if isinstance(repl, str) else repl(m)
        start, end = m.start(), m.end()
        pos, first = 0, True
        for t in nodes:
            txt = t.text or ""
            a, b = pos, pos + len(txt)
            pos = b
            if start == end:                       # pure insertion
                if a <= start <= b and first:
                    t.text = txt[:start - a] + new + txt[start - a:]
                    t.set(XML_SPACE, "preserve")
                    first = False
                continue
            if b <= start or a >= end:
                continue
            s0, s1 = max(start, a) - a, min(end, b) - a
            t.text = (txt[:s0] + new + txt[s1:]) if first else (txt[:s0] + txt[s1:])
            first = False
            t.set(XML_SPACE, "preserve")
    return len(matches)


def unique_cells(row):
    """python-docx repeats merged cells; keep one entry per w:tc, with its first grid column."""
    out, seen = [], set()
    for gi, c in enumerate(row.cells):
        if id(c._tc) in seen:
            continue
        seen.add(id(c._tc))
        out.append((gi, c))
    return out


def set_tc_text(tc, text):
    """Replace the whole content of a table cell with `text`, keeping the first run's formatting."""
    paras = tc.findall(qn("w:p"))
    p0 = paras[0]
    for extra in paras[1:]:
        tc.remove(extra)
    runs = p0.findall(qn("w:r"))
    rpr = None
    if runs:
        rpr = runs[0].find(qn("w:rPr"))
    else:
        ppr = p0.find(qn("w:pPr"))
        mark = ppr.find(qn("w:rPr")) if ppr is not None else None
        if mark is not None:
            rpr = copy.deepcopy(mark)
            rpr.tag = qn("w:rPr")
    for child in list(p0):
        if child.tag != qn("w:pPr"):
            p0.remove(child)
    if rpr is not None:
        # some formats mark "fill here" cells in red / bold / underline - values print in normal black
        color = rpr.find(qn("w:color"))
        if color is not None and color.get(qn("w:val"), "").upper() in ("FF0000", "C00000", "RED"):
            rpr = copy.deepcopy(rpr)
            for tag in ("w:color", "w:u", "w:b", "w:bCs"):
                for el in rpr.findall(qn(tag)):
                    rpr.remove(el)
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = OxmlElement("w:t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    r.append(t)
    p0.append(r)


def make_tag_p(text):
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    r.append(t)
    p.append(r)
    return p


def tag_row(tr, tag, where):
    """Insert a docxtpl {%tr ... %} row before/after the w:tr element."""
    new = copy.deepcopy(tr)
    for i, tc in enumerate(new.findall(qn("w:tc"))):
        set_tc_text(tc, tag if i == 0 else "")
    if where == "before":
        tr.addprevious(new)
    else:
        tr.addnext(new)


def is_blank_p(el):
    if el.tag != qn("w:p"):
        return False
    if p_text(el).strip():
        return False
    for bad in ("w:drawing", "w:pict", "w:object", "w:sectPr", "w:br"):
        if el.find(".//" + qn(bad)) is not None:
            return False
    return el.find(".//" + MC_ALT) is None


def set_page_break_before(p):
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p.insert(0, ppr)
    if ppr.find(qn("w:pageBreakBefore")) is not None:
        return
    pb = OxmlElement("w:pageBreakBefore")
    anchor = None
    for tag in ("w:pStyle", "w:keepNext", "w:keepLines"):
        el = ppr.find(qn(tag))
        if el is not None:
            anchor = el
    if anchor is not None:
        anchor.addnext(pb)
    else:
        ppr.insert(0, pb)
