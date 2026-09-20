"""
Reference Gap Strategy — Dual-Direction Paper Trading Bot
-------------------------------------------------------------------------------------------
Version: v2_kalshi_ask_fix
Each run (every ~15 min via scheduler):

1. MIGRATE: Runs one-time migration if legacy (v1) history exists without 'gate_version',
   archiving legacy closed trades into 'closed_positions_v1_buggy.csv'.

2. OPEN: For each asset, checks BOTH combo directions:
     Combo A: Kalshi-Down + Poly-Up
     Combo B: Kalshi-Up + Poly-Down
   Evaluates depth for 100 contracts on BOTH legs via order books.
   If both legs fill 100 contracts under combined $0.80 VWAP, logs position tagged with gate_version.

3. SETTLE: Checks any open positions whose window has closed, records outcome/profit.

4. SUMMARY: Writes per-asset report to GitHub Step Summary (filtered exclusively for v2_kalshi_ask_fix).
"""

import json
import time
import os
from datetime import datetime, timezone

import requests
import pandas as pd

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

ENTRY_THRESHOLD = 0.80
TARGET_SHARES = 100  # Minimum depth size to fill

ASSETS = {
    "BTC": {"kalshi_series": "KXBTC15M", "poly_prefix": "btc-updown-15m"},
    "ETH": {"kalshi_series": "KXETH15M", "poly_prefix": "eth-updown-15m"},
    "SOL": {"kalshi_series": "KXSOL15M", "poly_prefix": "sol-updown-15m"},
    "XRP": {"kalshi_series": "KXXRP15M", "poly_prefix": "xrp-updown-15m"},
    "DOGE": {"kalshi_series": "KXDOGE15M", "poly_prefix": "doge-updown-15m"},
    "BNB": {"kalshi_series": "KXBNB15M", "poly_prefix": "bnb-updown-15m"},
    "HYPE": {"kalshi_series": "KXHYPE15M", "poly_prefix": "hype-updown-15m"},
    "ZEC": {"kalshi_series": "KXZEC15M", "poly_prefix": "zec-updown-15m"},
}

OPEN_FILE = "open_positions.csv"
CLOSED_FILE = "closed_positions.csv"
GATE_VERSION = "v2_kalshi_ask_fix"
LEGACY_CLOSED_FILE = "closed_positions_v1_buggy.csv"

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


def get_json(url, params=None, retries=5, backoff=1.5):
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=20)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            time.sleep(backoff ** (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(backoff ** (attempt + 1))
            continue
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            return None
        return resp.json()
    return None


def round_down_15m(dt):
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def load_csv(path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return pd.read_csv(path)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
    return pd.DataFrame()


def save_csv(df, path):
    df.to_csv(path, index=False)


def migrate_legacy_history():
    """One-time: move pre-fix history aside so it never mixes with v2 results."""
    closed_df = load_csv(CLOSED_FILE)
    if not closed_df.empty and "gate_version" not in closed_df.columns:
        if not os.path.exists(LEGACY_CLOSED_FILE):
            save_csv(closed_df, LEGACY_CLOSED_FILE)
        save_csv(pd.DataFrame(columns=list(closed_df.columns) + ["gate_version"]), CLOSED_FILE)
        print(f"Archived {len(closed_df)} pre-fix closed trades")
    open_df = load_csv(OPEN_FILE)
    if not open_df.empty and "gate_version" not in open_df.columns:
        open_df["gate_version"] = "v1_buggy"
        save_csv(open_df, OPEN_FILE)


# ---------------- ORDER BOOK & VWAP CALCULATION ----------------

def calculate_vwap(asks, target_shares=100):
    """
    Calculates VWAP for buying target_shares from an order book ask array [[price, size], ...].
    Returns (filled_bool, vwap_price).
    """
    if not asks:
        return False, 0.0

    accumulated_shares = 0
    total_cost = 0.0

    for level in asks:
        try:
            price = float(level[0])
            size = float(level[1])
        except (ValueError, IndexError, TypeError):
            continue

        needed = target_shares - accumulated_shares
        fill_amount = min(needed, size)

        total_cost += fill_amount * price
        accumulated_shares += fill_amount

        if accumulated_shares >= target_shares:
            vwap = total_cost / target_shares
            return True, round(vwap, 4)

    return False, 0.0  # Not enough depth to fill 100 shares


def _norm(levels):
    out = []
    for p, q in levels or []:
        p, q = float(p), float(q)
        if p > 1: p /= 100.0
        out.append((p, q))
    return out


def get_kalshi_ask_vwap(ticker, side, target_shares=100):
    data = get_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook")
    if not data: return False, 0.0
    ob = data.get("orderbook_fp") or data.get("orderbook") or {}
    yes_bids = _norm(ob.get("yes_dollars") or ob.get("yes"))
    no_bids = _norm(ob.get("no_dollars") or ob.get("no"))
    opposite = no_bids if side == "yes" else yes_bids   # buying YES lifts NO bids
    asks = sorted((round(1.0 - p, 4), q) for p, q in opposite)
    return calculate_vwap(asks, target_shares)


def get_polymarket_ask_vwap(token_id, target_shares=100):
    """
    Fetches Polymarket CLOB orderbook for a token ID and calculates ask VWAP.
    """
    data = get_json(f"{CLOB_BASE}/book", params={"token_id": token_id})
    if not data or "asks" not in data:
        return False, 0.0

    asks_raw = data.get("asks", [])
    asks = [[item.get("price"), item.get("size")] for item in asks_raw]
    asks.sort(key=lambda x: float(x[0]))
    
    return calculate_vwap(asks, target_shares)


# ---------------- OPEN LOGIC ----------------

def get_current_kalshi_market(series_ticker):
    """Live Kalshi market ticker & close_time."""
    data = get_json(f"{KALSHI_BASE}/markets", params={
        "series_ticker": series_ticker, "status": "open", "limit": 5
    })
    if not data:
        return None
    markets = data.get("markets", [])
    if not markets:
        return None
    m = markets[0]
    ticker = m.get("ticker")
    close_time = m.get("close_time")
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "close_time": close_time,
    }


def get_current_polymarket_info(poly_prefix, window_start_dt):
    """Live Polymarket event: slug and token IDs for Up/Down."""
    ts = int(window_start_dt.timestamp())
    slug = f"{poly_prefix}-{ts}"
    data = get_json(f"{GAMMA_BASE}/events", params={"slug": slug})
    if not data:
        return None
    event = data[0] if isinstance(data, list) and data else None
    if not event:
        return None
    markets = event.get("markets") or []
    if not markets:
        return None
    m = markets[0]

    outcomes = m.get("outcomes")
    token_ids = m.get("clobTokenIds")
    if not outcomes or not token_ids:
        return None
    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
    except (ValueError, TypeError):
        return None

    tokens = {}
    for name, tid in zip(outcomes, token_ids):
        tokens[name.lower()] = tid

    if "up" not in tokens or "down" not in tokens:
        return None

    return {"slug": slug, "up_token": tokens["up"], "down_token": tokens["down"]}


def check_and_open_positions():
    open_df = load_csv(OPEN_FILE)
    closed_df = load_csv(CLOSED_FILE)
    already_logged = set()
    if not open_df.empty:
        already_logged |= set(open_df["kalshi_ticker"])
    if not closed_df.empty:
        already_logged |= set(closed_df["kalshi_ticker"])

    now = datetime.now(timezone.utc)
    window_start = round_down_15m(now)
    new_rows = []

    for asset, cfg in ASSETS.items():
        kalshi = get_current_kalshi_market(cfg["kalshi_series"])
        if not kalshi:
            print(f"[{asset}] no open Kalshi market found, skipping")
            continue
        if kalshi["ticker"] in already_logged:
            print(f"[{asset}] {kalshi['ticker']} already logged, skipping")
            continue

        poly = get_current_polymarket_info(cfg["poly_prefix"], window_start)
        if not poly:
            print(f"[{asset}] no matching Polymarket tokens found, skipping")
            continue

        # Evaluate Combo A: Kalshi-Down + Poly-Up for 100 contracts
        k_down_ok, k_down_vwap = get_kalshi_ask_vwap(kalshi["ticker"], side="no", target_shares=TARGET_SHARES)
        p_up_ok, p_up_vwap = get_polymarket_ask_vwap(poly["up_token"], target_shares=TARGET_SHARES)
        
        cost_a = round(k_down_vwap + p_up_vwap, 4) if (k_down_ok and p_up_ok) else 999.0

        # Evaluate Combo B: Kalshi-Up + Poly-Down for 100 contracts
        k_up_ok, k_up_vwap = get_kalshi_ask_vwap(kalshi["ticker"], side="yes", target_shares=TARGET_SHARES)
        p_down_ok, p_down_vwap = get_polymarket_ask_vwap(poly["down_token"], target_shares=TARGET_SHARES)

        cost_b = round(k_up_vwap + p_down_vwap, 4) if (k_up_ok and p_down_ok) else 999.0

        if cost_a == 999.0 and cost_b == 999.0:
            print(f"[{asset}] {kalshi['ticker']}: Insufficient order book depth (<{TARGET_SHARES} contracts) on both combos, skipping")
            continue

        if cost_a <= cost_b:
            direction, chosen_cost = "A", cost_a
        else:
            direction, chosen_cost = "B", cost_b

        print(f"[{asset}] {kalshi['ticker']}: Combo A VWAP=${cost_a}, Combo B VWAP=${cost_b} -> chose {direction} (${chosen_cost})")

        if chosen_cost < ENTRY_THRESHOLD:
            new_rows.append({
                "gate_version": GATE_VERSION,
                "asset": asset,
                "kalshi_ticker": kalshi["ticker"],
                "poly_slug": poly["slug"],
                "direction": direction,
                "window_start": window_start.isoformat(),
                "close_time": kalshi["close_time"],
                "combined_cost": chosen_cost,
                "cost_a": cost_a if cost_a != 999.0 else None,
                "cost_b": cost_b if cost_b != 999.0 else None,
                "logged_at": now.isoformat(),
            })
            print(f"  -> OPENED (direction {direction}, 100-contract VWAP ${chosen_cost} < ${ENTRY_THRESHOLD})")
        else:
            print(f"  -> skipped (best 100-contract VWAP ${chosen_cost} >= ${ENTRY_THRESHOLD})")

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        open_df = pd.concat([open_df, new_df], ignore_index=True) if not open_df.empty else new_df
        save_csv(open_df, OPEN_FILE)
        print(f"Saved {len(new_rows)} new position(s)")


# ---------------- SETTLE LOGIC ----------------

def get_kalshi_outcome(ticker):
    data = get_json(f"{KALSHI_BASE}/markets/{ticker}")
    if not data or "market" not in data:
        return None
    m = data["market"]
    if m.get("status") != "finalized":
        return None
    floor_strike = m.get("floor_strike")
    expiration_value = m.get("expiration_value")
    if floor_strike is None or expiration_value is None:
        return None
    try:
        return "up" if float(expiration_value) >= float(floor_strike) else "down"
    except (ValueError, TypeError):
        return None


def get_polymarket_outcome(slug):
    data = get_json(f"{GAMMA_BASE}/events", params={"slug": slug})
    if not data:
        return None
    event = data[0] if isinstance(data, list) and data else None
    if not event:
        return None
    markets = event.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    if not m.get("closed"):
        return None
    outcomes = m.get("outcomes")
    prices = m.get("outcomePrices")
    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        prices = [float(p) for p in prices]
    except (ValueError, TypeError):
        return None
    for name, price in zip(outcomes, prices):
        if price >= 0.9:
            return "up" if "up" in name.lower() else "down"
    return None


def payout_for_combo(direction, k_outcome, p_outcome):
    if k_outcome == p_outcome:
        return 1.0
    if direction == "A" and k_outcome == "down" and p_outcome == "up":
        return 2.0
    if direction == "B" and k_outcome == "up" and p_outcome == "down":
        return 2.0
    return 0.0


def check_and_settle_positions():
    open_df = load_csv(OPEN_FILE)
    if open_df.empty:
        print("No open positions to settle.")
        return

    closed_df = load_csv(CLOSED_FILE)
    still_open = []
    newly_closed = []
    now = datetime.now(timezone.utc)

    for _, row in open_df.iterrows():
        close_time = pd.to_datetime(row["close_time"])
        if close_time.tzinfo and close_time.tz_convert("UTC") > now:
            still_open.append(row)
            continue

        k_outcome = get_kalshi_outcome(row["kalshi_ticker"])
        p_outcome = get_polymarket_outcome(row["poly_slug"])

        if k_outcome is None or p_outcome is None:
            still_open.append(row)
            continue

        payout = payout_for_combo(row["direction"], k_outcome, p_outcome)
        profit = round(payout - row["combined_cost"], 4)

        closed_row = row.to_dict()
        closed_row.update({
            "kalshi_outcome": k_outcome,
            "polymarket_outcome": p_outcome,
            "payout": payout,
            "profit": profit,
            "settled_at": now.isoformat(),
        })
        newly_closed.append(closed_row)
        print(f"[{row['asset']}] {row['kalshi_ticker']}: SETTLED (dir {row['direction']}) — "
              f"Kalshi={k_outcome}, Poly={p_outcome}, profit=${profit}")

    if newly_closed:
        new_closed_df = pd.DataFrame(newly_closed)
        closed_df = pd.concat([closed_df, new_closed_df], ignore_index=True) if not closed_df.empty else new_closed_df
        save_csv(closed_df, CLOSED_FILE)
        print(f"Settled {len(newly_closed)} position(s)")

    remaining_df = pd.DataFrame(still_open) if still_open else pd.DataFrame(columns=open_df.columns)
    save_csv(remaining_df, OPEN_FILE)
    print(f"{len(remaining_df)} position(s) still open.")


# ---------------- SUMMARY (per-asset, filtered for v2_kalshi_ask_fix) ----------------

def write_github_summary():
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    open_df = load_csv(OPEN_FILE)
    closed_df = load_csv(CLOSED_FILE)

    if not closed_df.empty and "gate_version" in closed_df.columns:
        closed_df = closed_df[closed_df["gate_version"] == GATE_VERSION]
    if not open_df.empty and "gate_version" in open_df.columns:
        open_df = open_df[open_df["gate_version"] == GATE_VERSION]

    lines = []
    lines.append(f"# Reference Gap Bot — Dual-Direction ({GATE_VERSION}) — Run Summary\n")
    lines.append(f"**Run time:** {datetime.now(timezone.utc).isoformat()}\n")

    for asset in ASSETS.keys():
        lines.append(f"## {asset}\n")

        asset_open = open_df[open_df["asset"] == asset] if not open_df.empty else pd.DataFrame()
        lines.append(f"### Open Positions (awaiting settlement) — {asset}\n")
        if asset_open.empty:
            lines.append("_None currently open._\n")
        else:
            lines.append(f"**Count: {len(asset_open)}**\n")
            lines.append("| Ticker | Direction | 100-Contract VWAP Cost | Window Start |")
            lines.append("|---|---|---|---|")
            for _, r in asset_open.iterrows():
                lines.append(f"| {r['kalshi_ticker']} | {r['direction']} | "
                              f"${r['combined_cost']} | {r['window_start']} |")
            lines.append("")

        asset_closed = closed_df[closed_df["asset"] == asset] if not closed_df.empty else pd.DataFrame()
        lines.append(f"### Closed Positions — {asset}\n")
        if asset_closed.empty:
            lines.append("_No closed trades yet under fixed pricing._\n")
        else:
            total = len(asset_closed)
            wins = (asset_closed["profit"] > 0).sum()
            win_rate = wins / total * 100
            total_profit = asset_closed["profit"].sum()
            avg_roi = asset_closed["profit"].mean()
            avg_entry_cost = asset_closed["combined_cost"].mean()

            lines.append(f"**Total settled: {total}**")
            lines.append(f"**Win rate: {win_rate:.1f}%**")
            lines.append(f"**ROI (avg profit per $1 staked): ${avg_roi:.4f}**")
            lines.append(f"**Total simulated profit: ${total_profit:.2f}**")
            lines.append(f"**Average entry cost: ${avg_entry_cost:.4f}**\n")

            lines.append(f"#### Last 10 settled trades — {asset}\n")
            lines.append("| Ticker | Dir | Kalshi | Poly | VWAP Cost | Payout | Profit |")
            lines.append("|---|---|---|---|---|---|---|")
            for _, r in asset_closed.tail(10).iloc[::-1].iterrows():
                lines.append(f"| {r['kalshi_ticker']} | {r['direction']} | "
                              f"{r['kalshi_outcome']} | {r['polymarket_outcome']} | "
                              f"${r['combined_cost']} | ${r['payout']} | ${r['profit']} |")
        lines.append("")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    print(f"=== Run started at {datetime.now(timezone.utc).isoformat()} ===\n")
    migrate_legacy_history()
    print("--- Checking for new positions to open ---")
    check_and_open_positions()
    print("\n--- Checking for positions to settle ---")
    check_and_settle_positions()
    write_github_summary()
    print("\n=== Run complete ===")
