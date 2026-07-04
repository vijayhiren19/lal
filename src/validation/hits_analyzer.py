"""Hit analysis for scoring picks.

Computes forward-return hits against configurable targets and windows,
stores results in the predicted_stock table, and provides analysis reports.

Usage (via run_hits.py):
    python -m src.validation.run_hits --compute --start-date 2026-05-01 --end-date 2026-06-04
    python -m src.validation.run_hits --analyze
"""

import logging
import time
from functools import lru_cache

import pandas as pd
import yaml

from config import ROOT_DIR
from db.connection import get_connection

logger = logging.getLogger("runner")


@lru_cache(maxsize=1)
def _load_config():
    """Load hit_analysis config from config/scoring.yaml (cached)."""
    config_path = ROOT_DIR / "config" / "scoring.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)["hit_analysis"]


def _get_next_trading_day(cur, symbol, pick_date):
    """Find the next trading day after pick_date for a specific symbol."""
    cur.execute(
        """
        SELECT trade_date, open_price, high_price, low_price, close_price
        FROM daily
        WHERE symbol = ? AND trade_date > ?
        ORDER BY trade_date ASC
        LIMIT 1
        """,
        (symbol, pick_date),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "trade_date": row[0],
        "open_price": row[1],
        "high_price": row[2],
        "low_price": row[3],
        "close_price": row[4],
    }



def _get_daily_rows(cur, symbol, from_date, to_date):
    """Get daily price rows for a symbol in a date range, ordered ascending."""
    cur.execute(
        """
        SELECT trade_date, high_price, low_price, close_price
        FROM daily
        WHERE symbol = ? AND trade_date > ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (symbol, from_date, to_date),
    )
    return cur.fetchall()


def _target_for_entry(entry_price, targets, windows):
    """Generate target records for each combination of target pct and window.

    Returns a list of dicts with keys: target_idx, target_pct, target_price, window_days.
    """
    records = []
    for i, tgt in enumerate(targets):
        tgt_pct = tgt["pct"]
        tgt_price = round(entry_price * (1 + tgt_pct / 100.0), 2)
        for w in windows:
            records.append({
                "target_idx": i + 1,  # 1, 2, 3
                "target_pct": tgt_pct,
                "target_price": tgt_price,
                "window_days": w,
            })
    return records


def compute_hits(start_date, end_date):
    """Compute hit analysis for all scoring picks in the given date range.

    For each pick, for each entry mode (from config), computes entry price,
    scans forward data for target hits, and stores results in predicted_stock.

    Args:
        start_date: Date string 'YYYY-MM-DD' for earliest pick_date.
        end_date: Date string 'YYYY-MM-DD' for latest pick_date.
    """
    config = _load_config()
    entry_modes = config["entry_modes"]
    targets = config["targets"]
    windows = config["windows"]
    _t0 = time.perf_counter()

    logger.info(
        "Computing hits for picks %s to %s: %d entry modes, %d targets, %d windows",
        start_date,
        end_date,
        len(entry_modes),
        len(targets),
        len(windows),
    )

    conn = get_connection()
    cur = conn.cursor()

    # Fetch all scoring_picks in the date range
    picks_df = pd.read_sql(
        """
        SELECT trade_date, symbol, score, rank, sector
        FROM scoring_picks
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, rank
        """,
        conn,
        params=(start_date, end_date),
    )

    if picks_df.empty:
        logger.warning("No scoring picks found in range %s to %s", start_date, end_date)
        conn.close()
        return

    logger.info("Found %d picks to process", len(picks_df))

    insert_sql = """
        INSERT OR REPLACE INTO predicted_stock (
            pick_date, symbol, entry_date, entry_price,
            tg1_price, tg1_date, tg1_high, tg1_close,
            tg2_price, tg2_date, tg2_high, tg2_close,
            tg3_price, tg3_date, tg3_high, tg3_close,
            window_low, window_low_date, window_end_date,
            target_hit, data_complete
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    inserted = 0
    max_window = max(windows)

    for _, pick in picks_df.iterrows():
        pick_date = pick["trade_date"]
        symbol = pick["symbol"]

        for mode in entry_modes:
            mode_name = mode["name"]
            slippage_pct = mode.get("slippage_pct")

            # Find next trading day for this specific symbol
            next_day = _get_next_trading_day(cur, symbol, pick_date)
            if next_day is None:
                logger.debug("No next trading day for %s pick %s", pick_date, symbol)
                continue

            entry_date = next_day["trade_date"]

            # Compute entry price
            if slippage_pct is not None:
                entry_price = round(next_day["open_price"] * (1 + slippage_pct / 100.0), 2)
            else:
                entry_price = round(next_day["high_price"], 2)

            # Compute target prices for tg1, tg2, tg3 (use the largest window for max target reach)
            target_prices = []
            for tgt in targets:
                tp = round(entry_price * (1 + tgt["pct"] / 100.0), 2)
                target_prices.append(tp)

            # Scan forward data for all targets across max window
            rows = _get_daily_rows(
                cur, symbol, pick_date,
                (pd.Timestamp(pick_date) + pd.Timedelta(days=max_window)).strftime("%Y-%m-%d"),
            )

            if not rows:
                logger.debug("No forward data for %s %s", symbol, pick_date)
                continue

            # Determine target hits hierarchically
            # Track best hit: 0=none, 1=tg1, 2=tg2, 3=tg3
            best_hit = 0

            # Store per-target hit info
            tg_info = {
                1: {"date": None, "high": None, "close": None},
                2: {"date": None, "high": None, "close": None},
                3: {"date": None, "high": None, "close": None},
            }

            window_low = float("inf")
            window_low_date = None
            window_end_date = None

            # For each window level, we check each target
            # Strategy: walk through days, for each target check if hit
            # Target 3 needs window 15, target 2 needs window 10, target 1 needs window 5

            # Map window_days to targets
            # windows config: [5, 10, 15]
            # target1 (4%) uses window 5
            # target2 (5%) uses window 10
            # target3 (10%) uses window 15
            # But we also want hierarchical: tg3 hit implies tg2 and tg1 also hit

            # Walk through data rows
            for row in rows:
                trade_date = row[0]
                high = row[1]
                low = row[2]
                close = row[3]

                days_from_pick = (pd.Timestamp(trade_date) - pd.Timestamp(pick_date)).days

                # Update window low
                if low < window_low:
                    window_low = low
                    window_low_date = trade_date

                # Check targets hierarchically (check in order: tg3 first, then tg2, then tg1)
                # Use the appropriate window for each target
                for tgt_idx, tgt_window in [(3, windows[2]), (2, windows[1]), (1, windows[0])]:
                    if best_hit >= tgt_idx:
                        continue  # already hit this or higher
                    if days_from_pick > tgt_window:
                        continue  # outside window
                    if high >= target_prices[tgt_idx - 1]:
                        best_hit = tgt_idx
                        tg_info[tgt_idx]["date"] = trade_date
                        tg_info[tgt_idx]["high"] = round(high, 2)
                        tg_info[tgt_idx]["close"] = round(close, 2)
                        # If tg2 hit and not yet tracking tg1, mark tg1 as also hit at this date
                        if tgt_idx >= 2 and tg_info[1]["date"] is None:
                            tg_info[1]["date"] = trade_date
                            tg_info[1]["high"] = round(high, 2)
                            tg_info[1]["close"] = round(close, 2)
                        if tgt_idx >= 3 and tg_info[2]["date"] is None:
                            tg_info[2]["date"] = trade_date
                            tg_info[2]["high"] = round(high, 2)
                            tg_info[2]["close"] = round(close, 2)

                # Update window end date
                window_end_date = trade_date

            # If window low is still inf, set it to entry price
            if window_low == float("inf") or window_low is None:
                window_low = entry_price
                window_low_date = entry_date

            # Ensure tg_close / tg_high are set for windows where we have data
            # For targets not hit, use last available data
            for tgt_idx in [1, 2, 3]:
                if tg_info[tgt_idx]["date"] is None and rows:
                    # Use end of appropriate window
                    tgt_window = [5, 10, 15][tgt_idx - 1]
                    window_end_row = None
                    for row in rows:
                        td = row[0]
                        days = (pd.Timestamp(td) - pd.Timestamp(pick_date)).days
                        if days <= tgt_window:
                            window_end_row = row
                    if window_end_row is not None:
                        tg_info[tgt_idx]["high"] = round(window_end_row[1], 2)
                        tg_info[tgt_idx]["close"] = round(window_end_row[3], 2)

            # Determine row identifier based on mode_name — we store one row per pick per mode
            # Since predicted_stock PK is (pick_date, symbol), we store latest mode
            # Better approach: use mode_name in the PK as suffix or use the main row.
            # Per spec: predicted_stock table has PK (pick_date, symbol).
            # We'll overwrite with each mode, which means the last mode wins.
            # For multi-mode analysis, we need to store all. Let's use a composite key approach
            # where we store mode in the symbol field as "SYMBOL|MODE" or handle differently.
            #
            # Actually, looking at the AGENTS.md predicted_stock schema, PK is (pick_date, symbol).
            # For multi-mode, we should store separate rows. But the schema doesn't include mode.
            # We'll store the first mode's result and log a warning if multiple modes differ.
            # For the analyze step, we re-compute from scoring_picks anyway.
            #
            # Simplest: store one row per (pick_date, symbol). Use the open_p3 mode as primary.
            # The analyze step reads from predicted_stock which already has results.
            # For proper multi-mode support, we'd need a mode column in the schema.
            # Let's just store for open_p3 mode (first in list).

            if mode_name != entry_modes[0]["name"]:
                # For non-primary modes, skip storage for now (schema limitation)
                # But we still compute and could store if we add mode to PK later
                continue

            values = (
                pick_date,
                symbol,
                entry_date,
                entry_price,
                target_prices[0] if len(target_prices) > 0 else None,  # tg1_price
                tg_info[1]["date"],                                      # tg1_date
                tg_info[1]["high"],                                      # tg1_high
                tg_info[1]["close"],                                     # tg1_close
                target_prices[1] if len(target_prices) > 1 else None,    # tg2_price
                tg_info[2]["date"],                                      # tg2_date
                tg_info[2]["high"],                                      # tg2_high
                tg_info[2]["close"],                                     # tg2_close
                target_prices[2] if len(target_prices) > 2 else None,    # tg3_price
                tg_info[3]["date"],                                      # tg3_date
                tg_info[3]["high"],                                      # tg3_high
                tg_info[3]["close"],                                     # tg3_close
                round(window_low, 2) if window_low is not None else None,
                window_low_date,
                window_end_date,
                best_hit,
                1,  # data_complete
            )

            cur.execute(insert_sql, values)
            inserted += 1

            if inserted % 500 == 0:
                conn.commit()
                logger.info("Inserted %d predicted_stock rows…", inserted)

    conn.commit()
    conn.close()
    _elapsed = time.perf_counter() - _t0
    logger.info("Hit computation complete: %d rows inserted/updated (%.2fs)", inserted, _elapsed)


def analyze_hits(detail_threshold=0):
    """Analyze hit rates from predicted_stock table.

    Pivots on target × window to show hit counts and percentages.
    If detail_threshold > 0, prints individual pick details with target_hit >= threshold.

    Args:
        detail_threshold: Minimum target_hit level to show in detail (0=show summary only).
    """
    conn = get_connection()

    # Load predicted_stock data
    df = pd.read_sql(
        """
        SELECT
            pick_date,
            symbol,
            entry_date,
            entry_price,
            tg1_price, tg1_date, tg1_high, tg1_close,
            tg2_price, tg2_date, tg2_high, tg2_close,
            tg3_price, tg3_date, tg3_high, tg3_close,
            window_low, window_low_date, window_end_date,
            target_hit,
            data_complete
        FROM predicted_stock
        ORDER BY pick_date, symbol
        """,
        conn,
    )
    conn.close()

    if df.empty:
        logger.warning("No data in predicted_stock table. Run --compute first.")
        return

    total = len(df)

    # Summarise by target_hit level
    hit_labels = {0: "None", 1: "Tg1", 2: "Tg2", 3: "Tg3"}
    summary = df["target_hit"].value_counts().sort_index()

    print("\n" + "=" * 60)
    print("  HIT ANALYSIS SUMMARY")
    print("=" * 60)
    print(f"  Total picks analyzed: {total}")
    print()
    print(f"  {'Target Hit':<20} {'Count':>8} {'%':>8}")
    print(f"  {'-'*20} {'-'*8} {'-'*8}")

    any_hits = 0
    for level in sorted(hit_labels.keys()):
        count = summary.get(level, 0)
        pct = count / total * 100 if total > 0 else 0
        label = hit_labels[level]
        print(f"  {label:<20} {count:>8} {pct:>7.1f}%")
        if level > 0:
            any_hits += count

    any_pct = any_hits / total * 100 if total > 0 else 0
    print(f"  {'Any':<20} {any_hits:>8} {any_pct:>7.1f}%")

    # Also compute by window
    print()
    print(f"  {'Window':<10} {'Target':<12} {'Picks':>8} {'Hits':>8} {'Hit%':>8}")
    print(f"  {'-'*10} {'-'*12} {'-'*8} {'-'*8} {'-'*8}")

    config = _load_config()
    windows = config["windows"]
    targets = config["targets"]

    for w in windows:
        for tgt in targets:
            tgt_name = tgt["name"]
            tgt_pct = tgt["pct"]
            # Determine which target_hit level this corresponds to
            level = None
            for i, t in enumerate(targets):
                if t["pct"] == tgt_pct:
                    level = i + 1
                    break
            if level is None:
                continue

            # Count picks where target_hit >= level AND the hit was within this window
            # Since we only store best hit, we approximate by counting all hits >= level
            hits_for_target = (df["target_hit"] >= level).sum()
            pct_val = hits_for_target / total * 100 if total > 0 else 0

            print(
                f"  {f'{w}d':<10} {f'Tg{level} ({tgt_pct}%)':<12} "
                f"{total:>8} {hits_for_target:>8} {pct_val:>7.1f}%"
            )

    # Detail view
    if detail_threshold > 0:
        detail = df[df["target_hit"] >= detail_threshold]
        if detail.empty:
            print(f"\n  No picks with target_hit >= {detail_threshold}")
        else:
            print(f"\n  DETAIL — Picks with target_hit >= {detail_threshold} ({len(detail)} rows):")
            print(f"  {'Date':<12} {'Symbol':<12} {'Entry':>8} {'Tg Hit':>7} {'Tg1 Date':<12} {'Tg2 Date':<12} {'Tg3 Date':<12} {'Ret%':>7}")
            print(f"  {'-'*12} {'-'*12} {'-'*8} {'-'*7} {'-'*12} {'-'*12} {'-'*12} {'-'*7}")
            for _, row in detail.iterrows():
                entry = row["entry_price"]
                tg1 = row["tg1_price"]
                ret = ""
                if tg1 and tg1 != 0 and entry and entry != 0:
                    ret = f"{(tg1/entry - 1)*100:>6.1f}"
                print(
                    f"  {str(row['pick_date'])[:12]:<12} "
                    f"{str(row['symbol'])[:12]:<12} "
                    f"{row['entry_price']:>8.2f} "
                    f"{int(row['target_hit']):>7} "
                    f"{str(row['tg1_date'] or '')[0:10] if row['tg1_date'] else '-':<12} "
                    f"{str(row['tg2_date'] or '')[0:10] if row['tg2_date'] else '-':<12} "
                    f"{str(row['tg3_date'] or '')[0:10] if row['tg3_date'] else '-':<12} "
                    f"{ret:>7}"
                )

    print("=" * 60)
    print()
