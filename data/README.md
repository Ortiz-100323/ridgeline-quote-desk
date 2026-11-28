# Reference data

Loaded in memory by `app/catalog.py`, and into Postgres by `python -m app.seed` (tables in `db/schema.sql`, contract in `docs/CONTRACT.md`). All data is fictional.

| File | Rows | Key | Columns |
|---|---|---|---|
| `products.csv` | 200 | `part_number` | description, category, unit, list_price, status, replaced_by, primary_warehouse |
| `customers.csv` | 10 | `customer_id` | name, region, pricing_tier |
| `cross_reference.csv` | 52 | `competitor_part_number` | competitor, ridgeline_part_number, notes, last_verified |

## Profile

- **Products:** 18 categories (Power Transmission and Fasteners are the largest, 28 each). Units: EA 128, BX 37, PK 19, CS 7, RL 4, PR 4, FT 1. Warehouses: DAL 86, PHX 60, ATL 54. No duplicate part numbers or descriptions, and no missing prices.
- **Customers:** 7 on `contract` pricing, 3 on `standard`.
- **Cross-reference:** competitors are Northfield Supply (22), Apex Industrial (18) and MRO Direct (12). Every mapped Ridgeline part exists in `products.csv`. The file is incomplete on purpose, so some competitor parts will need description matching.

## Quirks the matcher must handle

| Quirk | Where | Handling |
|---|---|---|
| Discontinued product | `RL150432` is replaced by `RL120374` | `Catalog.resolve_replacement()` follows `replaced_by`. The worker should quote the replacement and flag the line |
| One competitor part, two Ridgeline parts | `NF-610338` maps to `RL177128` ("dup? Maria to confirm") and `RL107716` | Both kept. The line needs review |
| Uncertain mappings | 7 × "close sub", 8 × "pkg qty differs", 1 × "check thickness" | Notes kept. Lower confidence or flag |
| Unverified mappings | 9 rows have no `last_verified` | Kept as `None` |
| Cross-reference points to a discontinued part | 1 row | Resolve to the replacement |

Part numbers are compared after `normalize_part_number()` (uppercase, no spaces, dashes, dots or slashes), so `apx-bb 6204rs` matches `APX-BB-6204RS`.
