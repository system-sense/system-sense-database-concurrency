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

INSERT INTO inventory (sku_id, name, stock) VALUES (1, 'Front row seat', 100);

CREATE TABLE orders (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    sku_id      INT NOT NULL,
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
