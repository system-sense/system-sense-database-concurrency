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
    CONSTRAINT stock_never_negative CHECK (stock >= 0)
);

INSERT INTO inventory (sku_id, name, stock) VALUES
    (1, 'Front row seat', 100);

-- What we believe we sold. Note there is nothing here to be unique about:
-- three hundred different customers placing three hundred different orders is
-- not a duplicate-detection problem, and the UNIQUE constraint that fixed the
-- last series is no help at all. Every one of these rows is a real order that
-- a real person is entitled to.
CREATE TABLE orders (
    id          BIGSERIAL   PRIMARY KEY,
    sku_id      INT         NOT NULL REFERENCES inventory(sku_id),
    customer_id INT         NOT NULL,
    qty         INT         NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX orders_sku_idx ON orders (sku_id, created_at);
