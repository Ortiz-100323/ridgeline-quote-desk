"""What the LLM returns for one RFQ: a structured-output schema (Pydantic).

Extraction only. Matching to Ridgeline part numbers and to customers.customer_id is
done afterwards, in code, by the matcher (docs/CONTRACT.md §5).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Problems with the customer's request itself. They never trigger a retry or the
# fallback model: they are flagged for review (CONTRACT §3.3, "flag, don't guess").
LineIssue = Literal[
    "quantity_unclear",          # missing, vague ("a few boxes") or contradictory
    "unit_unclear",              # unit missing or not a standard unit
    "refers_to_previous_order",  # "same as last order"
    "refers_to_other_line",      # "same as above but 1/2 inch"
    "vague_description",         # not enough detail to identify a product
    "illegible",                 # handwriting or scan partly unreadable
    "substitution_allowed",      # customer accepts an equivalent
]

PartNumberKind = Literal["ridgeline", "competitor", "manufacturer", "unknown"]


class ExtractedLine(BaseModel):
    line: int = Field(description="1, 2, 3... in the order the items appear in the RFQ")
    as_written: str = Field(description="The customer's text for this item, copied exactly, without changes")
    quantity: float | None = Field(description="Number requested, or null when not stated")
    unit: str | None = Field(description="Unit as written by the customer (EA, PR, CS, box, pail...), or null")
    part_number: str | None = Field(description="Part number exactly as written, or null if none is given")
    part_number_kind: PartNumberKind | None = Field(
        description="ridgeline when it looks like RL+6 digits; competitor when it is a supplier number "
                    "(e.g. APX-, NF-, MRO-); manufacturer for generic sizes like 6204-2RS; null if no part number")
    competitor: str | None = Field(description="Supplier the part number comes from, when the RFQ says it")
    description: str | None = Field(
        description="Clean product description with every size and spec (thread, length, grade, material, "
                    "pressure class), taking 'same as above' references into account")
    customer_note: str | None = Field(description="Extra instructions for this line (thickness, pack size...)")
    issues: list[LineIssue] = Field(description="Problems with the request itself. Empty when the line is clear")
    extraction_confidence: float = Field(
        description="0 to 1: how sure you are that you read this line correctly (quantity, unit, part number, "
                    "description). Lower it for handwriting, scans or broken layout, not for vague requests")


class RfqExtraction(BaseModel):
    customer_name: str | None = Field(description="Company that sent the RFQ")
    contact_name: str | None = Field(description="Person who sent it")
    contact_email: str | None
    contact_phone: str | None
    customer_reference: str | None = Field(description="Their RFQ, bid or PO number")
    rfq_date: str | None = Field(description="Date of the request, ISO 8601 when possible")
    reply_by: str | None = Field(description="Requested response or delivery date, as written")
    ship_to: str | None
    current_supplier: str | None = Field(description="Supplier the customer buys from today, if named")
    lines: list[ExtractedLine] = Field(description="Every requested item. Never merge or split items")
    non_item_rows: list[str] = Field(
        description="Rows or lines that are not items: freight, totals, delivery notes, signatures")
    overall_confidence: float = Field(
        description="0 to 1: how sure you are that the whole document was read correctly and no item was "
                    "missed or invented")
    extraction_notes: str | None = Field(description="Short note for the reviewer about anything unusual")
