"""
Reference Gap Strategy — Dual-Direction Paper Trading Bot
------------------------------------------------------------
Each run (every ~15 min via scheduler):

1. OPEN: For BTC and ETH separately, checks BOTH combo directions:
     Combo A: Kalshi-Down + Poly-Up
     Combo B: Kalshi-Up + Poly-Down
   Takes whichever is cheaper. If under threshold, logs a SIMULATED
   position (no real money, no real orders).

2. SETTLE: Checks any open positions whose window has closed, records
   the real outcome and simulated profit/loss.

3. SUMMARY: Writes a per-asset (BTC and ETH kept fully separate) report
   to GitHub's run summary page.
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


# ---------------- OPEN LOGIC ----------------

def get_current_kalshi_market(series_ticker):
    """Live Kalshi market: ticker, both Up ask and Down ask prices."""
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
    up_ask = m.get("yes_ask_dollars")
    down_ask = m.get("no_ask_dollars")
    close_time = m.get("close_time")
    if ticker is None or up_ask is None or down_ask is None:
        return None
    return {
        "ticker": ticker,
        "up_ask": float(up_ask),
        "down_ask": float(down_ask),
        "close_time": close_time,
    }


def get_current_polymarket_prices(poly_prefix, window_start_dt):
    """Live Polymarket event: slug, both Up ask and Down ask prices via CLOB."""
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

    prices = {}
    for name, tid in zip(outcomes, token_ids):
        price_data = get_json(f"{CLOB_BASE}/price", params={"token_id": tid, "side": "BUY"})
        if price_data and "price" in price_data:
            prices[name.lower()] = float(price_data["price"])

    if "up" not in prices or "down" not in prices:
        return None

    return {"slug": slug, "up_ask": prices["up"], "down_ask": prices["down"]}


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

        poly = get_current_polymarket_prices(cfg["poly_prefix"], window_start)
        if not poly:
            print(f"[{asset}] no matching Polymarket prices found, skipping")
            continue

        cost_a = round(kalshi["down_ask"] + poly["up_ask"], 4)   # Kalshi-Down + Poly-Up
        cost_b = round(kalshi["up_ask"] + poly["down_ask"], 4)   # Kalshi-Up + Poly-Down

        if cost_a <= cost_b:
            direction, chosen_cost = "A", cost_a
        else:
            direction, chosen_cost = "B", cost_b

        print(f"[{asset}] {kalshi['ticker']}: cost_A(Down+Up)=${cost_a}, "
              f"cost_B(Up+Down)=${cost_b} -> chose {direction} (${chosen_cost})")

        if chosen_cost < ENTRY_THRESHOLD:
            new_rows.append({
                "asset": asset,
                "kalshi_ticker": kalshi["ticker"],
                "poly_slug": poly["slug"],
                "direction": direction,
                "window_start": window_start.isoformat(),
                "close_time": kalshi["close_time"],
                "combined_cost": chosen_cost,
                "cost_a": cost_a,
                "cost_b": cost_b,
                "logged_at": now.isoformat(),
            })
            print(f"  -> OPENED (direction {direction}, cost ${chosen_cost} < ${ENTRY_THRESHOLD})")
        else:
            print(f"  -> skipped (best cost ${chosen_cost} >= ${ENTRY_THRESHOLD})")

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


# ---------------- SUMMARY (per-asset, never mixed) ----------------

def write_github_summary():
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    open_df = load_csv(OPEN_FILE)
    closed_df = load_csv(CLOSED_FILE)

    lines = []
    lines.append("# Reference Gap Bot — Dual-Direction — Run Summary\n")
    lines.append(f"**Run time:** {datetime.now(timezone.utc).isoformat()}\n")

    for asset in ASSETS.keys():
        lines.append(f"## {asset}\n")

        asset_open = open_df[open_df["asset"] == asset] if not open_df.empty else pd.DataFrame()
        lines.append(f"### Open Positions (awaiting settlement) — {asset}\n")
        if asset_open.empty:
            lines.append("_None currently open._\n")
        else:
            lines.append(f"**Count: {len(asset_open)}**\n")
            lines.append("| Ticker | Direction | Combined Cost | Window Start |")
            lines.append("|---|---|---|---|")
            for _, r in asset_open.iterrows():
                lines.append(f"| {r['kalshi_ticker']} | {r['direction']} | "
                              f"${r['combined_cost']} | {r['window_start']} |")
            lines.append("")

        asset_closed = closed_df[closed_df["asset"] == asset] if not closed_df.empty else pd.DataFrame()
        lines.append(f"### Closed Positions — {asset}\n")
        if asset_closed.empty:
            lines.append("_No trades settled yet._\n")
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
            lines.append("| Ticker | Dir | Kalshi | Poly | Cost | Payout | Profit |")
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
    print("--- Checking for new positions to open ---")
    check_and_open_positions()
    print("\n--- Checking for positions to settle ---")
    check_and_settle_positions()
    write_github_summary()
    print("\n=== Run complete ===")
