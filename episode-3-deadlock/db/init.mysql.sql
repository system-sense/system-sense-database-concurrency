-- System Sense — DB Concurrency Ep.2: the same schema, in MySQL.
--
-- Deliberately the same shape as db/init.sql, because the episode's claim is
-- that identical schemas and identical statements behave differently. Any
-- difference here that is not forced by the dialect would undermine that.
--
-- Forced differences, and only these:
--   BIGSERIAL          -> BIGINT AUTO_INCREMENT
--   TIMESTAMPTZ        -> TIMESTAMP
--   CHECK is enforced by InnoDB from 8.0.16, so it stays and still never fires.

CREATE TABLE inventory (
    sku_id  INT  PRIMARY KEY,
    name    VARCHAR(128) NOT NULL,
    stock   INT  NOT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT stock_never_negative CHECK (stock >= 0)
) ENGINE=InnoDB;

INSERT INTO inventory (sku_id, name, stock) VALUES
    (1, 'Front row seat',      100),
    (2, 'Second row seat',     100),
    (3, 'Third row seat',      100),
    (4, 'Balcony seat',        100),
    (5, 'Standing ticket',     100),
    (6, 'Programme',           100),
    (7, 'Cloakroom ticket',    100),
    (8, 'Interval drink',      100);

CREATE TABLE orders (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    -- Nullable from Episode 3: a basket order's lines live in order_items.
    sku_id      INT NULL,
    customer_id INT NOT NULL,
    qty         INT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY orders_sku_idx (sku_id, created_at)
) ENGINE=InnoDB;

-- Non-unique secondary index on sku_id, matching Postgres. See the note in
-- db/init.sql: this is what InnoDB's next-key locking keys off.
CREATE TABLE reservations (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    sku_id      INT NOT NULL,
    customer_id INT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY reservations_sku_idx (sku_id)
) ENGINE=InnoDB;

-- ── Episode 3 ──────────────────────────────────────────────────────────────
-- The basket, same shape as db/init.sql. See the note there.
CREATE TABLE order_items (
    id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id BIGINT NOT NULL,
    sku_id   INT    NOT NULL,
    qty      INT    NOT NULL,
    KEY order_items_order_idx (order_id)
) ENGINE=InnoDB;

-- Reading performance_schema.data_locks needs the PROCESS privilege, and the
-- application user is not granted it by default. It is granted here so that the
-- lock evidence this episode rests on can be checked by anyone who clones the
-- repo, with the same user and the same query the capture uses:
--
--   SELECT OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE, LOCK_STATUS, count(*)
--     FROM performance_schema.data_locks GROUP BY 1,2,3,4,5;
--
-- Run it while a load is on. Once the last transaction commits the view is
-- empty, which is the whole difficulty of showing a lock to anybody.
-- Both are needed: PROCESS to see other sessions' locks at all, and SELECT on
-- performance_schema to read the table that reports them. PROCESS alone still
-- fails with "SELECT command denied ... for table 'data_locks'", which is a
-- confusing enough error to be worth the second line.
GRANT PROCESS ON *.* TO 'sysense'@'%';
GRANT SELECT ON performance_schema.* TO 'sysense'@'%';
FLUSH PRIVILEGES;
