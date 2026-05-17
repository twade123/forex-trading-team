-- Kronos Shadow Analysis Queries
-- Run against Database/v2/trading_forex.db
-- Usage: sqlite3 -header -column .../trading_forex.db < kronos_shadow_analysis.sql

-- =============================================================
-- Q1: Shadow verdict vs eventual outcome
-- "Of trades where scorer wanted to close (BLACK seen at any point),
--  what actually happened?"
-- =============================================================
SELECT
    CASE WHEN MAX(CASE WHEN zone='BLACK' THEN 1 ELSE 0 END) = 1
         THEN 'BLACK_seen' ELSE 'never_BLACK' END AS shadow_verdict,
    trade_outcome,
    COUNT(DISTINCT trade_id) AS n,
    ROUND(AVG(final_pnl_pips), 1) AS avg_final_pips
FROM kronos_shadow_scores
WHERE trade_outcome IS NOT NULL
GROUP BY shadow_verdict, trade_outcome;

-- =============================================================
-- Q2: PnL at first BLACK event vs final PnL
-- Per-trade: when did scorer first fire BLACK, and what was the
-- eventual trade outcome?
-- =============================================================
SELECT
    s1.trade_id, s1.pair,
    MIN(CASE WHEN s1.zone='BLACK' THEN s1.tick_time END) AS first_black_time,
    (SELECT pnl_pips FROM kronos_shadow_scores s2
     WHERE s2.trade_id = s1.trade_id AND s2.zone = 'BLACK'
     ORDER BY tick_time LIMIT 1) AS pnl_at_first_black,
    s1.final_pnl_pips,
    s1.trade_outcome
FROM kronos_shadow_scores s1
WHERE s1.zone = 'BLACK' AND s1.trade_outcome IS NOT NULL
GROUP BY s1.trade_id, s1.pair, s1.final_pnl_pips, s1.trade_outcome
ORDER BY first_black_time DESC;

-- =============================================================
-- Q3: Which BLACK reasons are most often WRONG (trade recovers to win)?
-- PHASE 5 INPUT: shows which scoring components fire on eventual winners.
-- "wrong_pct" = % of trades where this reason was cited in BLACK zone
-- but the trade eventually won.
-- =============================================================
SELECT
    json_each.value AS reason,
    COUNT(*) AS times_cited,
    SUM(CASE WHEN trade_outcome='win' THEN 1 ELSE 0 END) AS eventual_wins,
    SUM(CASE WHEN trade_outcome='loss' THEN 1 ELSE 0 END) AS eventual_losses,
    ROUND(100.0 * SUM(CASE WHEN trade_outcome='win' THEN 1 ELSE 0 END)
          / COUNT(*), 1) AS wrong_pct
FROM kronos_shadow_scores, json_each(reasons)
WHERE zone = 'BLACK' AND trade_outcome IS NOT NULL
GROUP BY reason
ORDER BY times_cited DESC;

-- =============================================================
-- Q4: Scorer hit-rate by candles_in (is the scorer more reliable
-- on older trades, or does it fire randomly throughout?)
-- =============================================================
SELECT
    candles_in / 5 * 5 AS candles_bucket,
    COUNT(*) AS tick_count,
    SUM(CASE WHEN zone='BLACK' THEN 1 ELSE 0 END) AS black_ticks,
    ROUND(100.0 * SUM(CASE WHEN zone='BLACK' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_black
FROM kronos_shadow_scores
GROUP BY candles_bucket
ORDER BY candles_bucket;
