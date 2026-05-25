import glob
import os
import shutil
import sys
from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from src.data_fetcher import DataFetcher
from src.screener import Screener
from src.notifier import TelegramNotifier, LineNotifier
from src.report_builder import build_report


def main():
    line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

    if not line_token:
        print("Missing environment variable: LINE_CHANNEL_ACCESS_TOKEN")
        sys.exit(1)

    fetcher = DataFetcher()

    # ── 1. Today's full-market snapshot (TWSE 上市 + TPEx 上櫃) ───────────────
    print("[1/7] Fetching today's market snapshot ...")
    twse_df = fetcher.get_today_all_stocks()
    otc_df  = fetcher.get_today_otc_stocks()

    if twse_df.empty and otc_df.empty:
        print("No market data — likely a holiday. Exiting.")
        sys.exit(0)

    today_df = pd.concat([twse_df, otc_df], ignore_index=True) if not otc_df.empty else twse_df
    print(f"      TWSE {len(twse_df)} + OTC {len(otc_df)} = {len(today_df)} stocks")

    # ── 2. Identify candidates ─────────────────────────────────────────────────
    print("[2/7] Identifying candidates ...")

    # Approx change% from TWSE spread (spread = close - prev_close)
    today_df["prev_close"] = today_df["close"] - today_df["spread"].fillna(0)
    today_df.loc[today_df["prev_close"] <= 0, "prev_close"] = today_df["close"]
    today_df["change_pct"] = today_df["spread"].fillna(0) / today_df["prev_close"]

    # Volume ranking is reliable from the TWSE/TPEx snapshot.
    # Change-pct ranking is recomputed after yfinance fetch (see below) because
    # TWSE marks 除權息 days with an 'X' flag that the parser nulls out, which
    # would otherwise push affected stocks out of the top-100 漲幅 ranking.
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
    print(f"[3/7] Fetching 40-day history for {len(candidates)} stocks from yfinance ...")
    price_df = fetcher.get_stock_history(list(candidates), start_date, end_date, market_map=market_map)
    if price_df.empty:
        print("No historical data. Exiting.")
        sys.exit(0)

    today_str = price_df["date"].max().strftime("%Y-%m-%d")
    print(f"      Latest trading date: {today_str}")

    # yfinance only reports 盤中一般交易 volume — miss 盤後定價. Override today's
    # row with the TWSE/TPEx EOD figure (matches what 三竹 / brokers display).
    today_dt = price_df["date"].max()
    vol_override = today_df.set_index("stock_id")["Trading_Volume"]
    mask = price_df["date"] == today_dt
    price_df.loc[mask, "Trading_Volume"] = (
        price_df.loc[mask, "stock_id"].map(vol_override)
        .fillna(price_df.loc[mask, "Trading_Volume"])
    )

    # Top-100 漲幅 ranking — use yfinance prev_close (handles 除權息 X-flag rows
    # that the TWSE snapshot reports as spread=0; e.g. 2486 一詮 on 2026-05-25).
    prev_close_yf = (
        price_df[price_df["date"] < today_dt]
        .sort_values("date").groupby("stock_id")["close"].last()
    )
    close_today_yf = (
        price_df[price_df["date"] == today_dt]
        .set_index("stock_id")["close"]
    )
    yf_change_pct = ((close_today_yf - prev_close_yf) / prev_close_yf).dropna()
    top100_change = set(yf_change_pct.nlargest(100).index)

    # ── 4. Institutional investor data ─────────────────────────────────────────
    print("[4/7] Fetching institutional investor data ...")
    institutional_df = fetcher.get_institutional_data(today_str)

    # ── 5. Inner/outer market ratio ─────────────────────────────────────────────
    print(f"[5/7] Fetching inner/outer market data for {len(candidates)} stocks ...")
    inner_outer_df = fetcher.get_inner_outer_data(list(candidates), market_map=market_map)
    print(f"      Got inner/outer data for {len(inner_outer_df)} stocks")

    # ── 6. Screen ──────────────────────────────────────────────────────────────
    print("[6/7] Running screener ...")
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

    # ── 6b. FinMind cross-check: 補位 primary T86/TPEx 漏列或全 0 的結果股 ──
    # 只針對 P1/P2 結果中「沒帶任何法人標籤」的個股逐檔補查 (一天 < 20 次呼叫)
    INST_LABELS = ("外資買超", "投信買超", "自營商買超")
    missing = [
        r for r in results["p1"] + results["p2"]
        if not any(lbl in r.conditions for lbl in INST_LABELS)
    ]
    if missing:
        print(f"      Cross-checking {len(missing)} result(s) without inst labels via FinMind ...")
        for r in missing:
            fm = fetcher.get_finmind_institutional_one(today_str, r.stock_id)
            if not fm:
                continue
            added = []
            if fm.get("foreign_net", 0) >= 1000:
                added.append("外資買超")
            if fm.get("trust_net", 0) >= 1000:
                added.append("投信買超")
            if fm.get("dealer_net", 0) >= 1000:
                added.append("自營商買超")
            if added:
                r.conditions.extend(added)
                print(f"        {r.stock_id} {r.name}: +{','.join(added)} (from FinMind)")

    # ── 7. Intraday charts + sector + HTML report + LINE notification ──────────
    print("[7/7] Building HTML report ...")
    selected = [r.stock_id for r in results["p1"]] + [r.stock_id for r in results["p2"]]
    intraday = fetcher.get_intraday_data(selected, market_map=market_map) if selected else {}
    print(f"      Intraday data for {len(intraday)} stocks")

    print("      Fetching sector data ...")
    sector_raw = fetcher.get_sector_map()
    sector_map = {}
    for sid, sec in sector_raw.items():
        market = "上櫃" if sid in otc_ids else "上市"
        sector_map[sid] = f"{sec}（{market}）"

    os.makedirs("docs", exist_ok=True)
    existing = sorted(
        os.path.basename(f).replace(".html", "")
        for f in glob.glob("docs/????-??-??.html")
    )
    available_dates = sorted(set(existing) | {today_str})

    html = build_report(results, today_str, intraday,
                        sector_map=sector_map, available_dates=available_dates)
    dated_path = f"docs/{today_str}.html"
    with open(dated_path, "w", encoding="utf-8") as f:
        f.write(html)
    shutil.copy(dated_path, "docs/index.html")
    print(f"      HTML saved ({today_str}.html + index.html)")

    notifier = LineNotifier(token=line_token)
    notifier.send_report(results, today_str)
    print("Report sent via LINE.")


if __name__ == "__main__":
    main()
