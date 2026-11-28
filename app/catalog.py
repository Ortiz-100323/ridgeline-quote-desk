"""Load the reference CSVs in data/ into lookup tables for matching.

Data quirks handled here (see data/README.md):
- A discontinued product points to its replacement via `replaced_by`.
- A competitor part number can map to more than one Ridgeline part; all
  candidates are kept so the worker can flag the line for review.
- Cross-reference rows may carry notes ("close sub", "pkg qty differs") and may
  have no `last_verified` date; both are kept as-is.
"""
import csv
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from app.paths import CROSS_REFERENCE_CSV, CUSTOMERS_CSV, PRODUCTS_CSV, safe_path


def normalize_part_number(value: str) -> str:
    """Uppercase and drop spaces, dashes, dots and slashes: 'apx-bb 6204rs' -> 'APXBB6204RS'."""
    return re.sub(r"[\s\-./]", "", value or "").upper()


@dataclass(frozen=True)
class Product:
    part_number: str
    description: str
    category: str
    unit: str
    list_price: Decimal
    status: str
    replaced_by: str | None
    primary_warehouse: str

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    region: str
    pricing_tier: str


@dataclass(frozen=True)
class CrossReference:
    competitor: str
    competitor_part_number: str
    ridgeline_part_number: str
    notes: str | None
    last_verified: str | None


@dataclass(frozen=True)
class Catalog:
    products: dict[str, Product]                      # by Ridgeline part number
    products_normalized: dict[str, Product]           # by normalize_part_number()
    customers: dict[str, Customer]                    # by customer_id
    cross_references: dict[str, list[CrossReference]]  # by normalized competitor part number

    def resolve_replacement(self, part_number: str) -> Product | None:
        """Follow `replaced_by` from a discontinued part to the active one."""
        product = self.products.get(part_number)
        seen = set()
        while product and not product.is_active and product.replaced_by and product.part_number not in seen:
            seen.add(product.part_number)
            product = self.products.get(product.replaced_by)
        return product


def _read_rows(path: Path) -> list[dict[str, str | None]]:
    with open(safe_path(path), newline="", encoding="utf-8") as f:
        return [{k: (v.strip() or None) if v is not None else None for k, v in row.items()}
                for row in csv.DictReader(f)]


def load_products(path: Path = PRODUCTS_CSV) -> dict[str, Product]:
    products = {}
    for row in _read_rows(path):
        products[row["part_number"]] = Product(
            part_number=row["part_number"],
            description=row["description"],
            category=row["category"],
            unit=row["unit"],
            list_price=Decimal(row["list_price"]),
            status=row["status"],
            replaced_by=row["replaced_by"],
            primary_warehouse=row["primary_warehouse"],
        )
    return products


def load_customers(path: Path = CUSTOMERS_CSV) -> dict[str, Customer]:
    return {row["customer_id"]: Customer(**row) for row in _read_rows(path)}


def load_cross_references(path: Path = CROSS_REFERENCE_CSV) -> dict[str, list[CrossReference]]:
    xrefs: dict[str, list[CrossReference]] = {}
    for row in _read_rows(path):
        xrefs.setdefault(normalize_part_number(row["competitor_part_number"]), []).append(CrossReference(**row))
    return xrefs


def load_catalog_from_db(conn) -> Catalog:
    """Same Catalog, read from Postgres (includes cross-references added by the portal)."""
    products = {}
    for row in conn.execute(
        """SELECT part_number, description, category, unit, list_price, status, replaced_by,
                  primary_warehouse FROM products"""
    ).fetchall():
        products[row[0]] = Product(*row)
    customers = {r[0]: Customer(*r) for r in conn.execute(
        "SELECT customer_id, name, region, pricing_tier FROM customers").fetchall()}
    xrefs: dict[str, list[CrossReference]] = {}
    for comp, cpn, rpn, notes, verified in conn.execute(
        """SELECT competitor, competitor_part_number, ridgeline_part_number, notes, last_verified
           FROM cross_references ORDER BY cross_reference_id"""
    ).fetchall():
        xrefs.setdefault(normalize_part_number(cpn), []).append(
            CrossReference(comp, cpn, rpn, notes, verified.isoformat() if verified else None))
    return Catalog(
        products=products,
        products_normalized={normalize_part_number(pn): p for pn, p in products.items()},
        customers=customers,
        cross_references=xrefs,
    )


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    products = load_products()
    return Catalog(
        products=products,
        products_normalized={normalize_part_number(pn): p for pn, p in products.items()},
        customers=load_customers(),
        cross_references=load_cross_references(),
    )
