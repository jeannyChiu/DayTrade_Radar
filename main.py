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

    # The TWSE/TPEx EOD snapshot is the authoritative source for today's bar.
    # yfinance has two failure modes on today's row: (1) it only reports 盤中一般
    # 交易 volume and misses 盤後定價, and (2) it intermittently serves a *partial/
    # stale* today-bar whose OHLC reflects an early-session quote rather than the
    # close. Previously only Trading_Volume was overridden, so a stale close
    # silently failed the breakout/≥4% checks and the stock vanished from the
    # report even though it was a valid candidate (e.g. 3019/2476/3715 on 6/09 —
    # all >5% gainers, absent). The missing-bar case (no today-row at all) is
    # handled by the injection below; this override fixes the partial-bar case by
    # making the snapshot authoritative for the full OHLCV of today's row.
    today_dt = price_df["date"].max()
    eod = today_df.drop_duplicates(subset="stock_id").set_index("stock_id")
    mask = price_df["date"] == today_dt
    today_sids = price_df.loc[mask, "stock_id"]
    for col in ["open", "max", "min", "close", "Trading_Volume"]:
        price_df.loc[mask, col] = (
            today_sids.map(eod[col]).fillna(price_df.loc[mask, col])
        )

    # yfinance intermittently lags today's daily bar for some symbols. The
    # batch-retry in get_stock_history only recovers tickers that returned NO
    # rows at all — a stock that returned history but is missing *today's* bar
    # slips through. Both the 漲幅前百 ranking (close_today_yf below) and the
    # screener key off rows dated today_dt, so a missing today-bar silently drops
    # the stock from selection even though the authoritative TWSE/TPEx EOD
    # snapshot already has it (e.g. 2492 華新科 6/02: +9.62%, 外資買超, ranked #9
    # market-wide, yet absent from the report). Inject today's EOD bar from the
    # snapshot for any candidate that has prior history but no today-bar.
    have_today = set(price_df.loc[mask, "stock_id"])
    have_hist  = set(price_df["stock_id"])
    inject_ids = [
        s for s in candidates
        if s not in have_today and s in have_hist and s in eod.index
    ]
    if inject_ids:
        inject = (
            eod.loc[inject_ids, ["open", "max", "min", "close", "Trading_Volume"]]
            .reset_index()  # index name is stock_id
        )
        inject["date"] = today_dt
        price_df = (
            pd.concat([price_df, inject], ignore_index=True)
            .sort_values(["stock_id", "date"])
            .reset_index(drop=True)
        )
        print(f"      injected {len(inject)} today-bars from EOD snapshot (yfinance lag)")

    # yfinance also intermittently drops the *prior* trading-day bar (40/318
    # candidates on 2026-06-17, incl. 6862 三集瑞 and 3715 定穎投控). The
    # screener's _breakout_tangled_ma reads prev_close as the raw yfinance bar at
    # iloc[-2]; when 06-16 is missing it silently uses 06-15 instead, so the
    # ≥4% gate, prev_spread and the prev_close→MA-band positioning all key off
    # the wrong day. This cuts both ways: 6862's real 188→206.5 (+9.84%) looked
    # like +2.23% off 06-15's 202 and was dropped, while 3715's real 171→176
    # (+2.92%) looked like +4.14% off 06-15's 169 and was wrongly included.
    # yfinance has no 06-16 bar even on a per-ticker refetch, so the only
    # authoritative source for that close is the EOD snapshot's 昨收 (close −
    # spread). Inject a synthetic prior-day bar so the close-based MA/breakout
    # pipeline sees a complete series and keys 漲跌幅/prev_spread/band-position
    # off the right day. Only the close is recoverable (open/high/low are set to
    # the close as placeholders), so the row is flagged `synthetic=True` and the
    # candlestick-shape conditions (一紅吃三黑/內困三日翻紅) drop it and evaluate on
    # the real bars only — they must not read a fabricated open/high/low, but they
    # also must still fire on stocks that genuinely qualify (e.g. 3715 2026-06-17
    # is a real 一紅吃三黑 and stays, only its wrong breakout tag is dropped).
    # Gated on a valid non-zero spread.
    price_df["synthetic"] = False
    market_dates = sorted(price_df["date"].unique())
    if len(market_dates) >= 2 and market_dates[-1] == today_dt:
        prev_trading_dt = market_dates[-2]
        have_prev = set(price_df.loc[price_df["date"] == prev_trading_dt, "stock_id"])
        eod_prev_close = eod["close"] - eod["spread"]
        gap_ids = [
            s for s in have_today
            if s not in have_prev and s in eod.index
            and pd.notna(eod.loc[s, "spread"]) and eod.loc[s, "spread"] != 0
            and eod_prev_close.get(s, 0) > 0
        ]
        if gap_ids:
            prev_bar = pd.DataFrame({
                "stock_id": gap_ids,
                "date": prev_trading_dt,
                "open": [eod_prev_close[s] for s in gap_ids],
                "max":  [eod_prev_close[s] for s in gap_ids],
                "min":  [eod_prev_close[s] for s in gap_ids],
                "close": [eod_prev_close[s] for s in gap_ids],
                "Trading_Volume": 0,
                "synthetic": True,
            })
            price_df = (
                pd.concat([price_df, prev_bar], ignore_index=True)
                .sort_values(["stock_id", "date"])
                .reset_index(drop=True)
            )
            price_df["synthetic"] = price_df["synthetic"].fillna(False)
            print(f"      injected {len(gap_ids)} prior-day bars from EOD 昨收 (yfinance gap)")

    # Authoritative prev_close for 漲跌幅. yfinance intermittently drops a single
    # *intermediate* daily bar for one ticker (6719 力智 missing 2026-06-15 on
    # 06-16), so "last yfinance close before today" silently falls back to an
    # even older session and inflates the computed change — 6719's real -0.42%
    # became +9.36%, wrongly entering 漲幅前百 and the report. The override/inject
    # guards above only cover a missing *today* bar, not a missing prior one.
    # The TWSE/TPEx EOD snapshot's 漲跌價差 (spread) is taken against the correct
    # reference price (incl. 除權息參考價), so prev_ref = close - spread is the
    # authoritative昨收 whenever spread is a valid non-zero number. Fall back to
    # yfinance prev_close only when spread is 0/NaN, which covers 除權息 X-flag
    # rows the snapshot reports as spread=0 (e.g. 2486 一詮 2026-05-25) — there
    # yfinance's auto_adjust already encodes the reference drop.
    prev_close_yf = (
        price_df[price_df["date"] < today_dt]
        .sort_values("date").groupby("stock_id")["close"].last()
    )
    eod_prev = eod["close"] - eod["spread"]
    valid_spread = eod["spread"].notna() & (eod["spread"] != 0) & (eod_prev > 0)
    prev_close_auth = prev_close_yf.copy()
    prev_close_auth.update(eod_prev[valid_spread])
    close_today_yf = (
        price_df[price_df["date"] == today_dt]
        .set_index("stock_id")["close"]
    )
    change_auth = ((close_today_yf - prev_close_auth) / prev_close_auth).dropna()
    top100_change = set(change_auth.nlargest(100).index)

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
        prev_close_map=prev_close_auth.to_dict(),
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

    # Report generation already succeeded above; a notification failure
    # (e.g. LINE 429 monthly-limit) must not fail the whole pipeline, or the
    # later "Deploy to GitHub Pages" step never runs and the site goes stale.
    notifier = LineNotifier(token=line_token)
    try:
        notifier.send_report(results, today_str)
        print("Report sent via LINE.")
    except Exception as e:
        print(f"WARNING: LINE notification failed, report still generated: {e}")


if __name__ == "__main__":
    main()
