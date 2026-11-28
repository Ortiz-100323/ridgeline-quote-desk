"""Worker step 3: match one extracted RFQ line to a Ridgeline product.

Lookup order (docs/CONTRACT.md §5), first hit wins:

1. ``ridgeline_part_number``  The customer wrote one of our part numbers.        0.99
2. ``cross_reference``        A competitor number in cross_references.           0.95, lowered
                              by notes / missing verification; several targets -> ambiguous.
3. ``description``            Most similar catalog description.                  at most 0.90

Description similarity is domain-aware: sizes and specs (6204, A42, 3/8, 1-1/2, grade 5,
150 lb, 5 HP, 20x20x2) must match exactly, words may match with small typos, and rare
tokens weigh more than common ones (IDF). The result always carries the top candidates, so
a later LLM step can choose among them (``match_method = "llm"``).

The matcher never guesses silently: low confidence, ambiguity, size mismatch, unit mismatch
and lines read from an image all set ``needs_review``.

Try it:  python -m app.worker.matcher "20 ea 6204-2RS bearings" "NF-220420" "bolts 3/8 x 1 1/2 gr5"
"""
from __future__ import annotations

import math
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

from app.catalog import Catalog, CrossReference, Product, load_catalog, normalize_part_number

if TYPE_CHECKING:
    from app.pipeline_log import PipelineLogger

REVIEW_THRESHOLD = float(os.getenv("MATCH_REVIEW_THRESHOLD", "0.8"))

CONF_EXACT = 0.99
CONF_XREF = 0.95
CONF_DESCRIPTION_MAX = 0.90
CONF_XREF_NOTE = 0.75          # any note ("close sub", "pkg qty differs", ...) -> a person checks
CONF_AMBIGUOUS = 0.60
PENALTY_UNVERIFIED = 0.05      # cross-reference without last_verified
PENALTY_REPLACED = 0.05        # discontinued part replaced by its successor
AMBIGUITY_MARGIN = 0.05        # top two description scores closer than this -> ambiguous
UNIT_BONUS = 0.06              # candidate unit equals the requested unit (ranking and ambiguity)
TOP_K = 5

# Flags that always require a person (docs/CONTRACT.md §4 flags vocabulary)
BLOCKING_FLAGS = {
    "no_match", "ambiguous_match", "ambiguous_cross_reference", "cross_reference_note",
    "cross_reference_conflict", "size_mismatch", "unit_mismatch", "discontinued", "from_image",
    "low_extraction_confidence",
    # extraction issues (app/worker/schemas.py LineIssue); substitution_allowed is informational
    "quantity_unclear", "unit_unclear", "refers_to_previous_order", "refers_to_other_line",
    "vague_description", "illegible",
}

# --------------------------------------------------------------------------- text normalization

_UNITS = {
    "ea": "EA", "each": "EA", "pc": "EA", "pcs": "EA", "piece": "EA", "pieces": "EA", "pail": "EA",
    "bx": "BX", "box": "BX", "boxes": "BX",
    "cs": "CS", "case": "CS", "cases": "CS",
    "pk": "PK", "pack": "PK", "packs": "PK", "pkg": "PK",
    "pr": "PR", "pair": "PR", "pairs": "PR",
    "rl": "RL", "roll": "RL", "rolls": "RL",
    "ft": "FT", "foot": "FT", "feet": "FT",
}

_SYNONYMS = {
    "bolt": "screw", "bolts": "screw", "screws": "screw",
    "hyd": "hydraulic", "ss": "stainless", "sst": "stainless",
    "gallon": "gal", "gallons": "gal", "gals": "gal",
    "elbows": "elbow", "deg": "", "degree": "",
    "large": "size_l", "medium": "size_m", "small": "size_s",
    "thk": "thick", "fp": "full port",
}
_SIZES = {"xs", "s", "m", "l", "xl", "xxl"}

_STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "with", "to", "in", "x", "qty", "quantity",
    "per", "please", "need", "needs", "same", "as", "above", "but", "we", "our", "usually", "get",
    "few", "some", "whatever", "you", "have", "thats", "that", "equivalent", "asap", "is", "it",
    "box", "boxes", "case", "cases", "pack", "ea", "each", "pcs", "pc", "pk", "bx", "cs", "pr",
}

# "AW 46" -> aw46, "NLGI #2" -> nlgi2, "JIC -08" -> jic08, "#40" chain -> 40
_PREFIX_NUM = re.compile(r"\b(aw|iso|nlgi|merv|nrr|ansi|jic|type|grit)\s*[#-]?\s*-?\s*(\d+)\b")
_GRADE = re.compile(r"\b(?:gr|grade)\.?\s*(\d+)\b")
_NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(hp|rpm|lb|lbs|micron|mil|gal|oz|psi|ml|phase|ton|ft|vac)\b")
_DIM3 = re.compile(r"\b(\d+)\s*x\s*(\d+)\s*x\s*(\d+)\b")
_MIXED = re.compile(r"\b(\d+)\s+(\d+/\d+)\b")              # 1 1/2 -> 1-1/2
_SIZE_WORD = re.compile(r"\bsize\s+(xs|s|m|l|xl|xxl)\b")
_LETTER_DASH_NUM = re.compile(r"\b([a-z])-(\d)")            # L-100 -> l100
_SPEC_TOKEN = re.compile(r"\d+-\d+/\d+|\d+/\d+|\d+\.\d+[a-z]*|[a-z0-9_]+")   # 1-1/2, 3/8, 1.5hp


def _normalize_text(text: str) -> str:
    t = text.lower()
    t = t.replace("”", '"').replace("″", '"').replace("''", '"')
    t = re.sub(r'(\d)\s*(?:"|inches|inch|in\b\.?)', r"\1 ", t)     # inch is the default unit: drop it
    t = re.sub(r"(\d)\s*#", r"\1 lb", t)                           # 150# -> 150 lb
    t = re.sub(r"#\s*(\d)", r"\1", t)                              # #40 -> 40
    t = t.replace("gallon", "gal")
    t = _GRADE.sub(r"grade\1", t)
    t = _PREFIX_NUM.sub(r"\1\2", t)
    t = _DIM3.sub(r"\1x\2x\3", t)
    t = _MIXED.sub(r"\1-\2", t)
    t = _SIZE_WORD.sub(r"size_\1", t)
    t = _NUM_UNIT.sub(lambda m: m.group(1) + {"lbs": "lb"}.get(m.group(2), m.group(2)), t)
    t = _LETTER_DASH_NUM.sub(r"\1\2", t)
    return t


def tokenize(text: str) -> list[str]:
    """Normalized tokens. Sizes and specs survive intact: '3/8-16 x 1-1/2' -> 3/8, 16, 1-1/2."""
    tokens: list[str] = []
    for raw in _SPEC_TOKEN.findall(_normalize_text(text)):
        for tok in _SYNONYMS.get(raw, raw).split():
            if tok in _SIZES:
                tok = f"size_{tok}"
            elif not any(c.isdigit() for c in tok) and len(tok) > 3 and tok.endswith("s") \
                    and not tok.endswith("ss"):
                tok = tok[:-1]                                     # plural: bearings -> bearing
                tok = _SYNONYMS.get(tok, tok)
            if tok and tok not in _STOPWORDS and (len(tok) > 1 or tok.isdigit()):
                tokens.append(tok)
    return tokens


def is_spec(token: str) -> bool:
    return any(c.isdigit() for c in token) or token.startswith("size_")


def normalize_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.strip().lower().rstrip(".")
    return _UNITS.get(u, unit.strip().upper() if u.upper() in set(_UNITS.values()) else None)


# --------------------------------------------------------------------------- results

@dataclass(frozen=True)
class Candidate:
    part_number: str
    description: str
    unit: str
    score: float                         # 0..1, description similarity (or 1.0 for exact / xref)
    source: str                          # ridgeline_part_number | cross_reference | description
    missing: tuple[str, ...] = ()        # query tokens the product lacks
    unit_match: bool = False             # product unit equals the requested unit

    @property
    def rank(self) -> float:
        return self.score + (UNIT_BONUS if self.unit_match else 0.0)


@dataclass
class MatchResult:
    as_written: str
    ridgeline_part_number: str | None
    match_method: str | None
    confidence: float | None
    needs_review: bool
    flags: list[str] = field(default_factory=list)
    note: str | None = None
    candidates: list[Candidate] = field(default_factory=list)

    def to_line_fields(self) -> dict[str, Any]:
        """The match fields of an output line (docs/CONTRACT.md §4)."""
        return {
            "ridgeline_part_number": self.ridgeline_part_number,
            "match_method": self.match_method,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "flags": list(self.flags),
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["candidates"] = [asdict(c) for c in self.candidates]
        return d


# --------------------------------------------------------------------------- matcher

_COMPETITOR_PN = re.compile(r"^[A-Za-z]{2,4}-[A-Za-z0-9-]*\d")   # APX-BB-6206ZZ, NF-778812
_PN_CHUNK = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-./]*[A-Za-z0-9]")
# A leading number is a quantity unless a size unit follows it ("2 in", "5 HP", 2", 1/2).
_NOT_SIZE = r"(?!(?:hp|rpm|in|inch|lb|micron|gal|ft|oz|psi|phase|ton|mil|mm)\b)"
_QTY_PATTERNS = [
    re.compile(r"\([^()\d]*\)"),                                             # "(we usually get boxes)"
    re.compile(r"^\s*[-*•]?\s*\d+\s+(?:ea|each|pcs?)\b\.?", re.I),        # "20 ea ..."
    # 1-3 digits only: "6205 bearings" is a bearing number, not a quantity
    re.compile(r"^\s*[-*•]?\s*\d{1,3}\s+" + _NOT_SIZE, re.I),             # "10 A42 v-belts"
    re.compile(r"\bx\s*\d+\b(?!\s*(?:[\"/x]|\d+/|in\b))", re.I),               # "... x 4", not 3/8 x 1 1/2
    re.compile(r"\bqty\.?\s*:?\s*\d+\b", re.I),                              # "qty 24"
    re.compile(r"\s-\s*\d+\b.*$"),                                           # "... - 300 (...)"
    re.compile(r",\s*\d+\s+(?:boxes|cases|packs|pairs|rolls|ea)\b.*$", re.I),  # ", 2 boxes"
]


class Matcher:
    def __init__(self, catalog: Catalog | None = None, review_threshold: float = REVIEW_THRESHOLD):
        self.catalog = catalog or load_catalog()
        self.review_threshold = review_threshold
        self._tokens: dict[str, list[str]] = {}
        df: Counter[str] = Counter()
        for pn, product in self.catalog.products.items():
            toks = tokenize(product.description)
            self._tokens[pn] = toks
            df.update(set(toks))
        n = len(self._tokens)
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self._max_idf = math.log(n + 1) + 1
        self._vocab_words = [t for t in df if not is_spec(t)]

    # ---------------------------------------------------------------- public API

    def match(self, as_written: str, *, part_number: str | None = None, description: str | None = None,
              unit: str | None = None, from_image: bool = False,
              log: PipelineLogger | None = None, job_id: str | None = None,
              line: int | None = None) -> MatchResult:
        """Match one line. ``part_number``/``description``/``unit`` come from extraction when
        available; otherwise they are derived from ``as_written``."""
        started = time.monotonic()
        result = self._match(as_written, part_number, description, unit)
        if from_image and "from_image" not in result.flags:
            result.flags.append("from_image")
        result.needs_review = self._needs_review(result)
        if log is not None:
            self._log(log, result, job_id, line, int((time.monotonic() - started) * 1000))
        return result

    @staticmethod
    def _log(log: PipelineLogger, result: MatchResult, job_id: str | None, line: int | None,
             duration_ms: int) -> None:
        # no RFQ text in logs (CONTRACT §3.4): part numbers, scores and flags only
        log.log("WARNING" if result.needs_review else "INFO", "matched",
                f"line {line or '?'}: {result.ridgeline_part_number or 'no match'} "
                f"via {result.match_method or '-'} ({result.confidence})",
                job_id=job_id, duration_ms=duration_ms, line=line,
                ridgeline_part_number=result.ridgeline_part_number, match_method=result.match_method,
                confidence=result.confidence, needs_review=result.needs_review, flags=result.flags,
                candidates=[c.part_number for c in result.candidates])

    def match_line(self, line: Any, *, from_image: bool = False, log: PipelineLogger | None = None,
                   job_id: str | None = None) -> MatchResult:
        """Match an extractor ``ExtractedLine`` (app/worker/schemas.py).

        Extraction ``issues`` become flags (all but ``substitution_allowed`` need review), and the
        line confidence is capped by ``extraction_confidence``: a misread line is not a sure match.
        """
        kind = getattr(line, "part_number_kind", None)
        part_number = line.part_number if kind != "manufacturer" else None  # 6204-2RS is a size, not a PN
        result = self.match(line.as_written, part_number=part_number, description=line.description,
                            unit=line.unit, from_image=from_image)
        for issue in getattr(line, "issues", None) or []:
            if issue not in result.flags:
                result.flags.append(issue)
        extraction_conf = getattr(line, "extraction_confidence", None)
        if extraction_conf is not None:   # LLM output: clamp, quote_lines CHECKs 0..1
            extraction_conf = min(max(float(extraction_conf), 0.0), 1.0)
        if extraction_conf is not None and result.confidence is not None and extraction_conf < result.confidence:
            result.confidence = round(extraction_conf, 2)
            if extraction_conf < self.review_threshold:
                result.flags.append("low_extraction_confidence")
        if getattr(line, "customer_note", None):
            result.note = _join(result.note, f"customer: {line.customer_note}")
        result.needs_review = self._needs_review(result)
        if log is not None:
            self._log(log, result, job_id, line.line, 0)
        return result

    def search(self, text: str, k: int = TOP_K, unit: str | None = None) -> list[Candidate]:
        """Top-k catalog products by description similarity."""
        query = tokenize(text)
        if not query:
            return []
        want_unit = normalize_unit(unit)
        scored = []
        for pn, toks in self._tokens.items():
            score, missing = self._similarity(query, toks)
            product = self.catalog.products[pn]
            rank = score + (UNIT_BONUS if want_unit and product.unit == want_unit else 0)
            scored.append((rank, score, missing, product))
        scored.sort(key=lambda s: (-s[0], len(self._tokens[s[3].part_number])))
        out: list[Candidate] = []
        seen: set[str] = set()
        for _, score, missing, product in scored:
            # a discontinued product and its successor are the same choice: keep the successor
            final = self.catalog.resolve_replacement(product.part_number) or product
            if final.part_number in seen:
                continue
            seen.add(final.part_number)
            out.append(Candidate(product.part_number, product.description, product.unit,
                                 round(score, 3), "description", tuple(missing),
                                 bool(want_unit) and product.unit == want_unit))
            if len(out) == k:
                break
        return out

    # ---------------------------------------------------------------- steps

    def _match(self, as_written: str, part_number: str | None, description: str | None,
               unit: str | None) -> MatchResult:
        chunks = ([part_number] if part_number else []) + _PN_CHUNK.findall(as_written)
        pn_chunks = [c for c in chunks if any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c)
                     and len(normalize_part_number(c)) >= 5]

        # 1. our own part number
        for chunk in pn_chunks:
            product = self.catalog.products_normalized.get(normalize_part_number(chunk))
            if product:
                return self._finish(as_written, product, "ridgeline_part_number", CONF_EXACT, [], None,
                                    [Candidate(product.part_number, product.description, product.unit,
                                               1.0, "ridgeline_part_number")], unit)

        # strip only competitor-looking numbers from the text, never specs like 6204-2RS
        competitor_chunks = [c for c in pn_chunks if _COMPETITOR_PN.match(c)]
        strip = ([part_number] if part_number else []) + competitor_chunks
        desc = description if description is not None else self._strip_line(as_written, strip)

        # 2. competitor part number
        for chunk in pn_chunks:
            rows = self.catalog.cross_references.get(normalize_part_number(chunk))
            if rows:
                return self._from_cross_reference(as_written, chunk, rows, desc, unit)
        missing_xref = competitor_chunks[0] if competitor_chunks else None

        # 3. description
        result = self._from_description(as_written, desc, unit)
        if missing_xref:
            result.flags.insert(0, "cross_reference_missing")
            result.note = _join(f"{missing_xref} is not in the cross-reference sheet", result.note)
        return result

    def _from_cross_reference(self, as_written: str, chunk: str, rows: list[CrossReference],
                              desc: str, unit: str | None) -> MatchResult:
        targets: dict[str, CrossReference] = {}
        for row in rows:
            targets.setdefault(row.ridgeline_part_number, row)
        query = tokenize(desc)

        if len(targets) > 1:
            ranked = sorted(targets.values(),
                            key=lambda r: -self._similarity(query, self._tokens[r.ridgeline_part_number])[0])
            best = self.catalog.products[ranked[0].ridgeline_part_number]
            cands = [self._xref_candidate(r, query) for r in ranked]
            note = f"{chunk} maps to {len(targets)} parts: {', '.join(targets)}"
            if any(r.notes for r in rows):
                note = _join(note, "; ".join(f"{r.ridgeline_part_number}: {r.notes}" for r in rows if r.notes))
            return self._finish(as_written, best, "cross_reference", CONF_AMBIGUOUS,
                                ["ambiguous_cross_reference"], note, cands, unit)

        row = next(iter(targets.values()))
        product = self.catalog.products[row.ridgeline_part_number]
        conf, flags, notes = CONF_XREF, [], []
        if row.notes:
            conf = min(conf, CONF_XREF_NOTE)
            flags.append("cross_reference_note")
            notes.append(f"cross-reference note: {row.notes}")
        if not row.last_verified:
            conf -= PENALTY_UNVERIFIED
            flags.append("unverified_cross_reference")
        if query:
            score, missing = self._similarity(query, self._tokens[product.part_number])
            spec_missing = [t for t in missing if is_spec(t)]
            if score < 0.5 or spec_missing:
                conf = min(conf, CONF_AMBIGUOUS)
                flags.append("cross_reference_conflict")
                notes.append(f"description does not fit {product.part_number}"
                             + (f" (missing {', '.join(spec_missing)})" if spec_missing else ""))
        return self._finish(as_written, product, "cross_reference", conf, flags, "; ".join(notes) or None,
                            [self._xref_candidate(row, query)], unit)

    def _from_description(self, as_written: str, desc: str, unit: str | None) -> MatchResult:
        cands = self.search(desc, unit=unit)
        # at least one real word must agree: a bare number ("x 4") is not a match
        words_hit = bool(cands) and any(not is_spec(t) and t not in cands[0].missing for t in tokenize(desc))
        if not cands or cands[0].score < 0.35 or not words_hit:
            return MatchResult(as_written, None, None, None, True, ["no_match"],
                               "no similar product in the catalog", cands)
        best, flags, notes = cands[0], [], []
        conf = CONF_DESCRIPTION_MAX * best.score
        spec_missing = [t for t in best.missing if is_spec(t)]
        if spec_missing:
            conf = min(conf, 0.7)
            flags.append("size_mismatch")
            notes.append(f"catalog item lacks {', '.join(spec_missing)}")
        if len(cands) > 1 and best.rank - cands[1].rank < AMBIGUITY_MARGIN:
            conf = min(conf, CONF_AMBIGUOUS)
            flags.append("ambiguous_match")
            close = [c.part_number for c in cands[1:] if best.rank - c.rank < AMBIGUITY_MARGIN]
            notes.append(f"close alternatives: {', '.join(close)}")
        return self._finish(as_written, self.catalog.products[best.part_number], "description", conf,
                            flags, "; ".join(notes) or None, cands, unit)

    def _finish(self, as_written: str, product: Product, method: str, conf: float, flags: list[str],
                note: str | None, cands: list[Candidate], unit: str | None) -> MatchResult:
        final = self.catalog.resolve_replacement(product.part_number) or product
        if final.part_number != product.part_number:
            conf -= PENALTY_REPLACED
            flags.append("discontinued_replaced")
            note = _join(f"{product.part_number} is discontinued, replaced by {final.part_number}", note)
        elif not final.is_active:
            flags.append("discontinued")
            conf = min(conf, CONF_AMBIGUOUS)
        want = normalize_unit(unit)
        if want and want != final.unit:
            flags.append("unit_mismatch")
            note = _join(f"requested unit {unit} but catalog sells {final.unit}", note)
        return MatchResult(as_written, final.part_number, method, round(max(conf, 0.0), 2), False,
                           flags, note, cands)

    # ---------------------------------------------------------------- helpers

    def _needs_review(self, r: MatchResult) -> bool:
        return (r.confidence is None or r.confidence < self.review_threshold
                or any(f in BLOCKING_FLAGS for f in r.flags))

    def _similarity(self, query: list[str], product_tokens: list[str]) -> tuple[float, list[str]]:
        """IDF-weighted share of the query found in the product. Specs match exactly, words fuzzily."""
        ptoks = set(product_tokens)
        total = got = 0.0
        missing: list[str] = []
        for tok in dict.fromkeys(query):
            weight = self._idf.get(tok)
            if weight is None:  # not in the catalog: specs are strong evidence, unknown words weak
                weight = self._max_idf if is_spec(tok) else 1.0
            total += weight
            if tok in ptoks or (not is_spec(tok) and len(tok) >= 5 and any(
                    fuzz.ratio(tok, p) >= 85 for p in ptoks if not is_spec(p))):
                got += weight
            else:
                missing.append(tok)
        return (got / total if total else 0.0), missing

    def _xref_candidate(self, row: CrossReference, query: list[str]) -> Candidate:
        p = self.catalog.products[row.ridgeline_part_number]
        score = self._similarity(query, self._tokens[p.part_number])[0] if query else 1.0
        return Candidate(p.part_number, p.description, p.unit, round(score, 3), "cross_reference")

    @staticmethod
    def _strip_line(text: str, pn_chunks: list[str]) -> str:
        """Drop part numbers and quantity phrases, keeping the descriptive words."""
        for chunk in pn_chunks:
            text = text.replace(chunk, " ")
        for pattern in _QTY_PATTERNS:
            text = pattern.sub(" ", text)
        return " ".join(text.split())


def _join(*parts: str | None) -> str | None:
    kept = [p for p in parts if p]
    return "; ".join(kept) if kept else None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Match RFQ lines to the catalog (CSV data, no database).")
    parser.add_argument("lines", nargs="+", help="as_written text of each line")
    parser.add_argument("--unit")
    parser.add_argument("--json", action="store_true", help="print the full result")
    args = parser.parse_args(argv)
    matcher = Matcher()
    for text in args.lines:
        r = matcher.match(text, unit=args.unit)
        if args.json:
            print(json.dumps(r.to_dict(), indent=2, default=str))
            continue
        review = "REVIEW" if r.needs_review else "ok"
        print(f"{text[:42]:42} -> {r.ridgeline_part_number or '-':9} {r.match_method or '-':21} "
              f"{r.confidence!s:5} {review:6} {','.join(r.flags)}")
        if r.note:
            print(f"{'':45}{r.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
