"""Create the schema and load data/*.csv into Postgres. Safe to re-run (upserts).

    python -m app.seed
"""
from app.catalog import load_cross_references, load_customers, load_products
from app.db import apply_schema, connect


def seed() -> dict[str, int]:
    products = load_products()
    customers = load_customers()
    xrefs = [x for group in load_cross_references().values() for x in group]

    with connect() as conn, conn.cursor() as cur:
        apply_schema(conn)
        cur.executemany(
            """INSERT INTO customers (customer_id, name, region, pricing_tier) VALUES (%s, %s, %s, %s)
               ON CONFLICT (customer_id) DO UPDATE SET name = EXCLUDED.name, region = EXCLUDED.region,
                   pricing_tier = EXCLUDED.pricing_tier""",
            [(c.customer_id, c.name, c.region, c.pricing_tier) for c in customers.values()],
        )
        # Insert without replaced_by first, so a replacement never points at a row not loaded yet.
        cur.executemany(
            """INSERT INTO products (part_number, description, category, unit, list_price, status,
                                     primary_warehouse)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (part_number) DO UPDATE SET description = EXCLUDED.description,
                   category = EXCLUDED.category, unit = EXCLUDED.unit, list_price = EXCLUDED.list_price,
                   status = EXCLUDED.status, primary_warehouse = EXCLUDED.primary_warehouse""",
            [(p.part_number, p.description, p.category, p.unit, p.list_price, p.status, p.primary_warehouse)
             for p in products.values()],
        )
        cur.executemany(
            "UPDATE products SET replaced_by = %s WHERE part_number = %s",
            [(p.replaced_by, p.part_number) for p in products.values()],
        )
        cur.executemany(
            """INSERT INTO cross_references (competitor, competitor_part_number, ridgeline_part_number,
                                             notes, last_verified, source)
               VALUES (%s, %s, %s, %s, %s, 'seed')
               ON CONFLICT (competitor_part_number_norm, ridgeline_part_number) DO UPDATE SET
                   competitor = EXCLUDED.competitor, notes = EXCLUDED.notes,
                   last_verified = EXCLUDED.last_verified""",
            [(x.competitor, x.competitor_part_number, x.ridgeline_part_number, x.notes, x.last_verified)
             for x in xrefs],
        )
        counts = {}
        for table in ("customers", "products", "cross_references"):
            counts[table] = cur.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counts


if __name__ == "__main__":
    for table, n in seed().items():
        print(f"{table}: {n}")
