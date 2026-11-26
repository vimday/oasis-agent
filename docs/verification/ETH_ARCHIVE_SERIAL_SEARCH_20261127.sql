-- DuckDB, offline. Run inside a fresh directory holding the two hash-checked
-- files named YYYY-MM-DD_0.parquet (see the accompanying verification record).
SET threads = 6;
SET memory_limit = '4GB';
CREATE TEMP VIEW raw_logs AS
SELECT * FROM read_parquet(['2026-11-13_0.parquet', '2026-11-25_0.parquet'], filename = true);

CREATE TEMP TABLE hit AS
SELECT substring(filename, 1, 10) AS day, block_number, block_hash,
       transaction_hash, transaction_index, log_index, address, topics[1] AS sig,
       topics[2] AS topic1, data
FROM raw_logs
WHERE topics[1] IN (
    '0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822', -- V2
    '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67', -- V3
    '0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83', -- Pancake V3
    '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f', -- V4
    '0x2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b', -- Balancer V2
    '0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140', -- Curve
    '0x143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c', -- Curve crypto
    '0x276856b36cbc45526a0ba64f44611557a2a8b68662c5388e9fe6d72e86e1c8cb'  -- Bancor
);

CREATE TEMP TABLE sizes AS
SELECT day, transaction_hash, count(*) AS events FROM hit GROUP BY ALL;

COPY (
    SELECT substring(filename, 1, 10) AS day, min(block_number) AS min_height,
           max(block_number) AS max_height, count(DISTINCT block_hash) AS emitting_blocks,
           count(*) AS logs, count(DISTINCT transaction_hash) AS emitting_txs
    FROM raw_logs GROUP BY day ORDER BY day
) TO 'coverage.csv' (HEADER, DELIMITER ',');

COPY (
    SELECT day, events, count(*) AS txs FROM sizes GROUP BY ALL ORDER BY day, events
) TO 'hist.csv' (HEADER, DELIMITER ',');

COPY (
    SELECT * FROM hit SEMI JOIN (
        SELECT day, transaction_hash FROM sizes WHERE events >= 5
    ) AS candidates USING (day, transaction_hash) ORDER BY day, transaction_hash, log_index
) TO 'candidates.csv' (HEADER, DELIMITER ',');

COPY (
    WITH fingerprints AS (
        SELECT block_number, transaction_hash, count(*) AS transfers
        FROM raw_logs
        WHERE filename LIKE '2026-11-13%'
          AND topics[1] = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
          AND len(topics) = 3 AND length(data) = 66
        GROUP BY ALL HAVING count(*) = 272
    )
    SELECT f.*, coalesce(s.events, 0) AS matches
    FROM fingerprints f LEFT JOIN sizes s USING (transaction_hash)
    ORDER BY matches DESC, f.transaction_hash
) TO 'fingerprint.csv' (HEADER, DELIMITER ',');
