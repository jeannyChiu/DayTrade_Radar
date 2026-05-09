import os
import sys
from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from src.data_fetcher import DataFetcher
from src.screener import Screener
from src.notifier import TelegramNotifier, LineNotifier


def main():
    line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

    if not line_token:
        print("Missing environment variable: LINE_CHANNEL_ACCESS_TOKEN")
        sys.exit(1)

    fetcher = DataFetcher()

    # ── 1. Today's full-market snapshot (TWSE 上市 + TPEx 上櫃) ───────────────
    print("[1/5] Fetching today's market snapshot ...")
    twse_df = fetcher.get_today_all_stocks()
    otc_df  = fetcher.get_today_otc_stocks()

    if twse_df.empty and otc_df.empty:
        print("No market data — likely a holiday. Exiting.")
        sys.exit(0)

    today_df = pd.concat([twse_df, otc_df], ignore_index=True) if not otc_df.empty else twse_df
    print(f"      TWSE {len(twse_df)} + OTC {len(otc_df)} = {len(today_df)} stocks")

    # ── 2. Identify candidates ─────────────────────────────────────────────────
    print("[2/5] Identifying candidates ...")

    # Approx change% from TWSE spread (spread = close - prev_close)
    today_df["prev_close"] = today_df["close"] - today_df["spread"].fillna(0)
    today_df.loc[today_df["prev_close"] <= 0, "prev_close"] = today_df["close"]
    today_df["change_pct"] = today_df["spread"].fillna(0) / today_df["prev_close"]

    # Full-market rankings (before price filter) — used inside screener
    top100_change = set(today_df.nlargest(100, "change_pct")["stock_id"])
    top100_volume = set(today_df.nlargest(100, "Trading_Volume")["stock_id"])

    # Filter to 100–500 NTD, no limit-down
    valid = today_df[
        (today_df["close"] >= 100) & (today_df["close"] <= 500)
        & (today_df["change_pct"] > -0.099)
    ]

    # Candidate pools
    top200_vol = set(valid.nlargest(200, "Trading_Volume")["stock_id"])
    top200_chg = set(valid.nlargest(200, "change_pct")["stock_id"])
    red_k      = set(valid[valid["close"] > valid["open"]]["stock_id"])   # for 一紅吃三黑
    big_gain   = set(valid[valid["change_pct"] >= 0.03]["stock_id"])      # for 突破糾結均線

    candidates = top200_vol | top200_chg | red_k | big_gain
    print(f"      {len(candidates)} candidate stocks")

    # Build name map and market map (上市 vs 上櫃，yfinance 需要不同後綴)
    name_map = today_df.set_index("stock_id")["name"].to_dict() if "name" in today_df.columns else {}
    otc_ids  = set(otc_df["stock_id"]) if not otc_df.empty else set()
    market_map = {sid: "OTC" for sid in candidates if sid in otc_ids}

    # ── 3. Historical data for candidates (yfinance) ───────────────────────────
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    print(f"[3/5] Fetching 40-day history for {len(candidates)} stocks from yfinance ...")
    price_df = fetcher.get_stock_history(list(candidates), start_date, end_date, market_map=market_map)
    if price_df.empty:
        print("No historical data. Exiting.")
        sys.exit(0)

    today_str = price_df["date"].max().strftime("%Y-%m-%d")
    print(f"      Latest trading date: {today_str}")

    # ── 4. Institutional investor data ─────────────────────────────────────────
    print("[4/6] Fetching institutional investor data ...")
    institutional_df = fetcher.get_institutional_data(today_str)

    # ── 5. Inner/outer market ratio ─────────────────────────────────────────────
    print(f"[5/6] Fetching inner/outer market data for {len(candidates)} stocks ...")
    inner_outer_df = fetcher.get_inner_outer_data(list(candidates), market_map=market_map)
    print(f"      Got inner/outer data for {len(inner_outer_df)} stocks")

    # ── 6. Screen & notify ─────────────────────────────────────────────────────
    print("[6/6] Running screener ...")
    screener = Screener()
    results = screener.screen(
        price_df, institutional_df, today_str,
        name_map=name_map,
        top100_change=top100_change,
        top100_volume=top100_volume,
        inner_outer_df=inner_outer_df,
    )

    p1, p2 = len(results["p1"]), len(results["p2"])
    print(f"      Found: P1={p1}, P2={p2}")

    notifier = LineNotifier(token=line_token)
    notifier.send_report(results, today_str)
    print("Report sent via LINE.")


if __name__ == "__main__":
    main()
