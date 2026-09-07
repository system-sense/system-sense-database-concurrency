-- System Sense — DB Concurrency Ep.1: The Phantom Update
--
-- One table, one row, and everything the series is about happens to it.
--
-- Read the CHECK constraint below and then remember it, because it is the
-- point: it is exactly the guard a reviewer would ask for, it is correct, and
-- it will never fire. A lost update does not write a negative number. It
-- writes a perfectly plausible one that happens to be wrong.

CREATE TABLE inventory (
    sku_id  INT  PRIMARY KEY,
    name    TEXT NOT NULL,
    stock   INT  NOT NULL,
    -- Kept from the first commit because Episode 1's third fix needs it, and
    -- because half the codebases that have this column never actually check it.
    version BIGINT NOT NULL DEFAULT 0,
    -- ── Episode 4 ──────────────────────────────────────────────────────────
    -- The fencing token, and this column is the entire punchline of the
    -- series. Every lock acquisition mints a strictly higher token; every
    -- guarded write carries the token it was granted; and the WHERE clause
    -- below refuses any write that arrives with a token lower than the one
    -- already recorded here:
    --
    --   claim:  UPDATE inventory SET fence_token = $1
    --            WHERE sku_id = $2 AND fence_token < $1
    --   write:  UPDATE inventory SET stock = $1
    --            WHERE sku_id = $2 AND fence_token = $3
    --
    -- Zero rows updated is a stale writer being refused BY THE STORAGE LAYER,
    -- which is the only place in this whole series that can refuse it. The
    -- lock cannot: a worker whose lease expired still believes it holds one,
    -- and nothing is in a position to tell it otherwise.
    --
    -- The row is claimed on the way IN, and that ordering is the mechanism
    -- rather than a detail. Stamping the token only at write time protects
    -- nothing when the stale worker happens to finish FIRST -- its token is
    -- still the highest the row has seen, so it is accepted and the newer
    -- holder's write lands on top of it. That was measured here, not reasoned:
    -- the first cut of this refused 1 stale write out of 30 expired leases.
    -- Claiming on entry, then writing under `fence_token = my_token`, asks the
    -- only question worth asking at that point -- does this row still think I
    -- am the holder? -- and refused 26 of 33.
    fence_token BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT stock_never_negative CHECK (stock >= 0)
);

-- Episode 3 needs more than one row, because a deadlock needs two rows taken
-- in two orders. The set is deliberately SMALL: eight SKUs is what makes two
-- random baskets overlap often enough for the cycle to be a real event rather
-- than a curiosity, and a shop's traffic really does concentrate like this.
INSERT INTO inventory (sku_id, name, stock) VALUES
    (1, 'Front row seat',      100),
    (2, 'Second row seat',     100),
    (3, 'Third row seat',      100),
    (4, 'Balcony seat',        100),
    (5, 'Standing ticket',     100),
    (6, 'Programme',           100),
    (7, 'Cloakroom ticket',    100),
    (8, 'Interval drink',      100);

-- What we believe we sold. Note there is nothing here to be unique about:
-- three hundred different customers placing three hundred different orders is
-- not a duplicate-detection problem, and the UNIQUE constraint that fixed the
-- last series is no help at all. Every one of these rows is a real order that
-- a real person is entitled to.
CREATE TABLE orders (
    id          BIGSERIAL   PRIMARY KEY,
    -- Episode 3 makes this nullable. Episodes 1 and 2 write one SKU per order
    -- and still do; a basket order writes its lines to order_items and leaves
    -- this NULL, rather than pretending one of the lines is the order.
    sku_id      INT         REFERENCES inventory(sku_id),
    customer_id INT         NOT NULL,
    qty         INT         NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX orders_sku_idx ON orders (sku_id, created_at);

-- ── Episode 2 ──────────────────────────────────────────────────────────────
-- A row per reserved seat, for the count-then-insert anomaly.
--
-- `sku_id` is on a NON-UNIQUE secondary index on purpose, and the choice is
-- load-bearing rather than incidental. InnoDB's gap locking under REPEATABLE
-- READ behaves differently for a unique index matching an existing row (record
-- lock only) than for a non-unique one (next-key lock, gap included), and the
-- whole point of this episode is what the two engines do with the same schema.
-- Pinning the index shape here means the capture measures one thing rather than
-- two.
CREATE TABLE reservations (
    id          BIGSERIAL   PRIMARY KEY,
    sku_id      INT         NOT NULL,
    customer_id INT         NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX reservations_sku_idx ON reservations (sku_id);

-- ── Episode 3 ──────────────────────────────────────────────────────────────
-- The basket. Episode 2 shipped without it on purpose: with one SKU per order
-- there is only ever one row to lock, and a transaction holding exactly one
-- lock cannot be half of a cycle.
--
-- This is the whole first commit of the episode. Nothing else about the
-- checkout changes: the statement it runs per line is still Episode 1's atomic
-- decrement, which is the FIX that episode landed on.
CREATE TABLE order_items (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT    NOT NULL REFERENCES orders(id),
    sku_id      INT       NOT NULL REFERENCES inventory(sku_id),
    qty         INT       NOT NULL
);

CREATE INDEX order_items_order_idx ON order_items (order_id);
