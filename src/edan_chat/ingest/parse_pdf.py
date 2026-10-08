"""Geometry-based parser for the CEI "EDAN 2025 – résultats nationaux détaillés" PDF.

Why not `extract_tables()`?  On this PDF it silently drops rows at page bottoms and
splits some merged cells (e.g. circonscription 006, 115).  Instead we:

1. assign every word to a column using the fixed x-boundaries of the table header
   (identical on all 35 pages),
2. cut the page into *circonscription blocks* and *candidate blocks* using the
   horizontal ruling lines drawn in the respective columns,
3. attach each candidate block to the circonscription block that contains it,
4. stitch blocks that are split across a page break (the block without an id
   continues the neighbouring one),
5. rebuild the vertical region labels from their characters (bottom-to-top).

Repeated headers / footers ("Page N de 35") are excluded because only words that
fall *inside* body blocks are read.  Everything is validated afterwards
(see `validate.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

# x-ranges (PDF points) of each column, taken from the header row cells.
COLS: dict[str, tuple[float, float]] = {
    "region": (20, 41),
    "circ": (41, 280),  # id (3 digits, left) + name
    "nb_bv": (280, 320),
    "inscrits": (320, 368),
    "votants": (368, 416),
    "taux_participation": (416, 460),
    "bulletins_nuls": (460, 509),
    "suffrages_exprimes": (509, 557),
    "bulletins_blancs": (557, 605),
    "bulletins_blancs_pct": (605, 649),
    "party": (649, 752),
    "candidate": (752, 1032),
    "score": (1032, 1080),
    "score_pct": (1080, 1126),
    "elu": (1126, 1172),
}
TURNOUT_FIELDS = [
    "nb_bv", "inscrits", "votants", "taux_participation", "bulletins_nuls",
    "suffrages_exprimes", "bulletins_blancs", "bulletins_blancs_pct",
]
ID_RE = re.compile(r"^\d{3}$")
EDGE_DEDUP = 3.0


@dataclass
class Block:
    page: int
    top: float
    bottom: float
    words: list[dict] = field(default_factory=list)

    def col_text(self, col: str) -> str:
        x0, x1 = COLS[col]
        ws = [w for w in self.words if x0 <= (w["x0"] + w["x1"]) / 2 < x1]
        ws.sort(key=lambda w: (round(w["top"]), w["x0"]))
        return " ".join(w["text"] for w in ws).strip()


def parse_int(s: str) -> int | None:
    s = s.replace(" ", "").replace(" ", "")
    return int(s) if s.isdigit() else None


def parse_pct(s: str) -> float | None:
    s = s.replace("%", "").replace(",", ".").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def _dedup(ys: list[float]) -> list[float]:
    out: list[float] = []
    for y in sorted(ys):
        if not out or y - out[-1] > EDGE_DEDUP:
            out.append(y)
    return out


def _h_edges(page, x_from: float, x_to: float) -> list[float]:
    return [
        e["top"] for e in page.edges
        if e["orientation"] == "h" and e["x0"] <= x_from and e["x1"] >= x_to
    ]


def _blocks(page, page_no: int, words, edges, body_top, body_bottom) -> list[Block]:
    ys = [y for y in _dedup(edges) if body_top - 1 <= y <= body_bottom]
    if not ys or ys[0] > body_top + 1:
        ys.insert(0, body_top)
    if ys[-1] < body_bottom - 1:
        ys.append(body_bottom)
    blocks = []
    for top, bottom in zip(ys, ys[1:]):
        bw = [w for w in words if top <= (w["top"] + w["bottom"]) / 2 < bottom]
        if bw:
            blocks.append(Block(page_no, top, bottom, bw))
    return blocks


def _region_label(page, top: float, bottom: float) -> str:
    x0, x1 = COLS["region"]
    chars = [c for c in page.chars if x0 <= c["x0"] < x1 and top <= c["top"] < bottom]
    # Labels are rotated 90°: reading order is bottom -> top.
    txt = "".join(c["text"] for c in sorted(chars, key=lambda c: -c["top"]))
    txt = re.sub(r"\s*-\s*", "-", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _page_layout(page):
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    # Header ends with the "NOMBRE  %" line (sub-header of BULL. BLANCS).
    header_bottom = max(w["bottom"] for w in words if w["text"] == "NOMBRE") + 1
    footer = [w for w in words if w["text"] == "Page" and w["top"] > page.height - 60]
    body_bottom = (min(w["top"] for w in footer) - 1) if footer else page.height
    body = [w for w in words if header_bottom <= w["top"] < body_bottom]
    return body, header_bottom, body_bottom


def _absorb(target: dict, orphan: dict, prepend: bool) -> None:
    if prepend:
        target["candidates"][:0] = orphan["candidates"]
    else:
        target["candidates"].extend(orphan["candidates"])
    if orphan["page"] != target["page"]:
        target.setdefault("extra_pages", set()).add(orphan["page"])
    if not target["region"] and orphan["region"]:
        target["region"] = orphan["region"]
    for f, v in orphan["turnout"].items():
        if v and not target["turnout"].get(f):
            target["turnout"][f] = v
    if orphan["name"]:
        parts = [orphan["name"], target["name"]] if prepend else [target["name"], orphan["name"]]
        target["name"] = " ".join(p for p in parts if p)


def _residual(block: dict, extra: list[dict]) -> int:
    """|sum(votes) - (exprimés - blancs)| if block+extra were one circonscription."""
    t = dict(block["turnout"])
    for o in extra:
        for f, v in o["turnout"].items():
            t[f] = t.get(f) or v
    expr, blancs = parse_int(t["suffrages_exprimes"]), parse_int(t["bulletins_blancs"])
    if expr is None or blancs is None:
        return 10**9
    votes = sum(parse_int(k["score"]) or 0 for b in [block, *extra] for k in b["candidates"])
    return abs(votes - (expr - blancs))


def _stitch_orphans(blocks: list[dict], issues: list[str]) -> list[dict]:
    """Attach id-less blocks to a neighbouring circonscription.

    A merged cell can be cut by a stray ruling line or by a page break, leaving
    blocks without a circ id.  For each run of such orphans between two id'd
    blocks A and B, choose the split point (first k -> A, rest -> B) that makes
    the vote arithmetic of A and B consistent (sum(votes) == exprimés - blancs).
    """
    anchors = [i for i, b in enumerate(blocks) if b["circ_id"]]
    for a_pos, b_pos in zip([None, *anchors], [*anchors, None]):
        lo = 0 if a_pos is None else a_pos + 1
        hi = len(blocks) if b_pos is None else b_pos
        run = blocks[lo:hi]
        if not run:
            continue
        A = blocks[a_pos] if a_pos is not None else None
        B = blocks[b_pos] if b_pos is not None else None
        best_k, best_err = None, None
        for k in range(len(run) + 1):
            if (k and A is None) or (k < len(run) and B is None):
                continue
            err = (_residual(A, run[:k]) if A else 0) + (_residual(B, run[k:]) if B else 0)
            if best_err is None or err < best_err:
                best_k, best_err = k, err
        if best_k is None:
            issues.extend(f"p{o['page']}: unattached block y={o['top']:.0f}" for o in run)
            continue
        # A non-zero best_err can be transient (B may still receive later orphans);
        # final arithmetic is verified globally in validate.py.
        for o in run[:best_k]:
            _absorb(A, o, prepend=False)
        for o in reversed(run[best_k:]):
            _absorb(B, o, prepend=True)
    return [b for b in blocks if b["circ_id"]]


def parse_pdf(pdf_path: Path) -> dict:
    """Return {"national": {...}, "circonscriptions": [...], "candidates": [...], "issues": [...]}."""
    issues: list[str] = []
    circ_blocks: list[dict] = []  # in document order, with attached candidate blocks
    national: dict = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            body, top, bottom = _page_layout(page)

            # National TOTAL row (page 1 only): a line starting with "TOTAL".
            total = [w for w in body if w["text"] == "TOTAL"]
            if total:
                t = total[0]
                line = Block(page_no, t["top"] - 1, t["bottom"] + 1,
                             [w for w in body if abs(w["top"] - t["top"]) < 2])
                national = {f: line.col_text(f) for f in TURNOUT_FIELDS}
                national["total_scores"] = line.col_text("score")
                national["source_page"] = page_no
                top = t["bottom"] + 1
                body = [w for w in body if w["top"] >= top]

            cblocks = _blocks(page, page_no, body, _h_edges(page, 60, 250), top, bottom)
            kblocks = _blocks(page, page_no, body, _h_edges(page, 760, 1030), top, bottom)
            region_edges = _dedup([y for y in _h_edges(page, 22, 39) if top - 1 <= y <= bottom])
            region_edges = [top] + [y for y in region_edges if y > top + 1] + [bottom]

            page_circs = []
            for b in cblocks:
                ids = [w for w in b.words if ID_RE.match(w["text"]) and w["x0"] < 66]
                circ_id = ids[0]["text"] if ids else None
                y_ref = ids[0]["top"] if ids else (b.top + b.bottom) / 2
                region = ""
                for r0, r1 in zip(region_edges, region_edges[1:]):
                    if r0 <= y_ref < r1:
                        region = _region_label(page, r0, r1)
                        break
                name = b.col_text("circ")
                if circ_id:
                    name = re.sub(rf"^{circ_id}\s*", "", name).replace(f" {circ_id} ", " ")
                    name = " ".join(t for t in name.split() if t != circ_id)
                page_circs.append({
                    "circ_id": circ_id, "page": page_no, "top": b.top, "bottom": b.bottom,
                    "name": name, "region": region,
                    "turnout": {f: b.col_text(f) for f in TURNOUT_FIELDS},
                    "candidates": [],
                })

            for k in kblocks:
                if not k.col_text("score"):
                    continue  # not a candidate row
                mid = (k.top + k.bottom) / 2
                owner = next((c for c in page_circs if c["top"] <= mid < c["bottom"]), None)
                if owner is None:
                    issues.append(f"p{page_no}: candidate block at y={mid:.0f} has no circ block")
                    continue
                owner["candidates"].append({
                    "party": k.col_text("party"),
                    "candidate": k.col_text("candidate"),
                    "score": k.col_text("score"),
                    "score_pct": k.col_text("score_pct"),
                    "elu": k.col_text("elu"),
                    "source_page": page_no,
                    "y": round(mid, 1),
                })
            circ_blocks.extend(page_circs)

    merged = _stitch_orphans(circ_blocks, issues)

    # Regions: empty label => same region as previous circonscription.
    last_region = ""
    for c in merged:
        if c["region"]:
            last_region = c["region"]
        else:
            c["region"] = last_region

    circs, cands = [], []
    for c in merged:
        t = c["turnout"]
        pages = sorted({c["page"], *c.get("extra_pages", set())})
        circs.append({
            "circ_id": c["circ_id"],
            "circonscription": c["name"],
            "region_raw": c["region"],
            "nb_bv": parse_int(t["nb_bv"]),
            "inscrits": parse_int(t["inscrits"]),
            "votants": parse_int(t["votants"]),
            "taux_participation": parse_pct(t["taux_participation"]),
            "bulletins_nuls": parse_int(t["bulletins_nuls"]),
            "suffrages_exprimes": parse_int(t["suffrages_exprimes"]),
            "bulletins_blancs": parse_int(t["bulletins_blancs"]),
            "bulletins_blancs_pct": parse_pct(t["bulletins_blancs_pct"]),
            "source_page": c["page"],
            "source_pages": ",".join(map(str, pages)),
        })
        for rank, k in enumerate(c["candidates"], start=1):
            cands.append({
                "circ_id": c["circ_id"],
                "list_order": rank,
                "party_raw": k["party"],
                "candidate_raw": k["candidate"],
                "votes": parse_int(k["score"]),
                "vote_pct": parse_pct(k["score_pct"]),
                "is_elected": k["elu"].upper().startswith("ELU"),
                "source_page": k["source_page"],
                "source_y": k["y"],
            })

    nat = {
        **{f: (parse_pct(v) if f.endswith(("pct", "participation")) else parse_int(v))
           for f, v in national.items() if f in TURNOUT_FIELDS},
        "total_scores": parse_int(national.get("total_scores", "")),
        "source_page": national.get("source_page"),
    }
    return {"national": nat, "circonscriptions": circs, "candidates": cands, "issues": issues}
