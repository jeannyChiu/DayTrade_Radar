import requests
import time
import pandas as pd

TWSE_DAY_ALL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
FINMIND_API = "https://api.finmindtrade.com/api/v4/data"


class DataFetcher:
    def __init__(self, token: str = ""):
        self.token = token
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

    # ── Step 1: today's market snapshot (TWSE, no date → always latest day) ───

    def get_today_all_stocks(self) -> pd.DataFrame:
        """All stocks for the most recent trading day from TWSE.
        Tries today and falls back up to 5 weekdays until data is found."""
        from datetime import datetime, timedelta
        for days_back in range(7):
            date = datetime.now() - timedelta(days=days_back)
            if date.weekday() >= 5:   # skip weekends
                continue
            date_str = date.strftime("%Y%m%d")
            resp = self.session.get(
                TWSE_DAY_ALL,
                params={"response": "json", "date": date_str},
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            body = resp.json()
            if body.get("stat") != "OK" or not body.get("data"):
                continue
            print(f"  TWSE snapshot date: {date.strftime('%Y-%m-%d')}")
            break
        else:
            return pd.DataFrame()

        fields = body.get("fields", [])
        df = pd.DataFrame(body.get("data", []), columns=fields)

        col_map = {
            "證券代號": "stock_id", "證券名稱": "name",
            "開盤價": "open",      "最高價": "max",
            "最低價": "min",        "收盤價": "close",
            "成交股數": "Trading_Volume", "漲跌價差": "spread",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "stock_id" not in df.columns:
            return pd.DataFrame()

        # Main-board stocks only; exclude ETFs (code starts with "0")
        df = df[df["stock_id"].str.match(r"^\d{4}$", na=False)]
        df = df[df["stock_id"].str[0] != "0"].copy()

        for col in ["open", "max", "min", "close", "Trading_Volume", "spread"]:
            if col in df.columns:
                df[col] = (
                    df[col].astype(str)
                    .str.replace(",", "", regex=False)
                    .replace({"--": None, "": None, "X": None})
                )
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # TWSE volume is in shares → convert to lots (張)
        if "Trading_Volume" in df.columns:
            df["Trading_Volume"] = (df["Trading_Volume"] / 1000).round(0)

        return df.dropna(subset=["open", "close", "max", "min"]).reset_index(drop=True)

    # ── Step 2: real historical OHLCV per stock (FinMind, requires data_id) ───

    # ── Step 1b: today's OTC snapshot (TPEx) ─────────────────────────────────

    def get_today_otc_stocks(self) -> pd.DataFrame:
        """All OTC (上櫃) stocks for the most recent trading day from TPEx.
        TPEx uses ROC calendar (year - 1911) in the date parameter.
        Uses daily_close_quotes (full EOD) instead of the 14:30 snapshot —
        the latter misses 盤後定價 trades and undercounts volume."""
        from datetime import datetime, timedelta

        for days_back in range(7):
            date = datetime.now() - timedelta(days=days_back)
            if date.weekday() >= 5:
                continue
            roc_date = f"{date.year - 1911}/{date.month:02d}/{date.day:02d}"
            try:
                resp = self.session.get(
                    "https://www.tpex.org.tw/web/stock/aftertrading/"
                    "daily_close_quotes/stk_quote_result.php",
                    params={"l": "zh-tw", "o": "json", "d": roc_date, "se": "AL"},
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
                tables = body.get("tables", [])
                if not tables or not tables[0].get("data"):
                    continue
                table = tables[0]
                fields = [f.strip() for f in table.get("fields", [])]
                rows = table["data"]

                df = pd.DataFrame(rows, columns=fields)

                # Strip whitespace from all column names (TPEx has trailing spaces)
                df.columns = df.columns.str.strip()

                col_map = {
                    "代號": "stock_id", "名稱": "name",
                    "收盤": "close",    "漲跌": "spread",
                    "開盤": "open",     "最高": "max",
                    "最低": "min",      "成交股數": "Trading_Volume",
                }
                df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

                # 4-digit OTC codes, no ETFs (not starting with "0")
                df = df[df["stock_id"].str.match(r"^\d{4}$", na=False)]
                df = df[df["stock_id"].str[0] != "0"].copy()

                for col in ["open", "max", "min", "close", "Trading_Volume", "spread"]:
                    df[col] = (
                        df[col].astype(str)
                        .str.replace(",", "", regex=False)
                        .replace({"---": None, "--": None, "": None})
                    )
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                df = df.dropna(subset=["open", "close", "max", "min"]).reset_index(drop=True)
                if df.empty:
                    continue

                # TPEx 成交股數單位為股 → 換算成張
                if "Trading_Volume" in df.columns:
                    df["Trading_Volume"] = (df["Trading_Volume"] / 1000).round(0)

                print(f"  TPEx snapshot date: {date.strftime('%Y-%m-%d')} ({len(df)} stocks)")
                return df

            except Exception:
                continue

        return pd.DataFrame()

    def get_stock_history(
        self, stock_ids: list, start_date: str, end_date: str,
        market_map: dict = None,
    ) -> pd.DataFrame:
        """Fetch daily OHLCV history via yfinance (Yahoo Finance).
        market_map: {stock_id: "OTC"} for 上櫃 stocks → uses .TWO suffix; others use .TW.
        Tickers silently dropped by the batch download are retried one-by-one."""
        import yfinance as yf
        from datetime import datetime, timedelta

        if market_map is None:
            market_map = {}

        ticker_to_sid = {}
        for sid in stock_ids:
            suffix = "TWO" if market_map.get(sid) == "OTC" else "TW"
            ticker_to_sid[f"{sid}.{suffix}"] = sid

        tickers = list(ticker_to_sid.keys())
        if not tickers:
            return pd.DataFrame()

        # yfinance end date is exclusive
        end_dt = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

        print(f"  yfinance downloading {len(tickers)} stocks ...")
        try:
            raw = yf.download(tickers, start=start_date, end=end_dt,
                              auto_adjust=True, progress=False)
        except Exception as e:
            print(f"  yfinance error: {e}")
            return pd.DataFrame()

        parsed = self._parse_yf_download(raw, tickers, ticker_to_sid)

        # Retry stocks the batch silently dropped (Yahoo intermittently returns
        # NaN-only columns for some tickers in multi-ticker downloads).
        got_sids = set(parsed["stock_id"]) if not parsed.empty else set()
        missing = [sid for sid in stock_ids if sid not in got_sids]
        if missing:
            print(f"  yfinance batch missed {len(missing)} stocks, retrying individually: {missing}")
            retry_frames = []
            for sid in missing:
                ticker = next(t for t, s in ticker_to_sid.items() if s == sid)
                try:
                    raw_one = yf.download(ticker, start=start_date, end=end_dt,
                                          auto_adjust=True, progress=False)
                except Exception as e:
                    print(f"    {sid} retry failed: {e}")
                    continue
                one = self._parse_yf_download(raw_one, [ticker], {ticker: sid})
                if not one.empty:
                    retry_frames.append(one)
            if retry_frames:
                recovered = sum(f["stock_id"].nunique() for f in retry_frames)
                parsed = pd.concat([parsed] + retry_frames, ignore_index=True)
                print(f"  yfinance recovered {recovered}/{len(missing)} stocks via retry")

        if parsed.empty:
            return pd.DataFrame()

        return (
            parsed[["stock_id", "date", "open", "max", "min", "close", "Trading_Volume"]]
            .sort_values(["stock_id", "date"])
            .reset_index(drop=True)
        )

    def _parse_yf_download(self, raw, tickers: list, ticker_to_sid: dict) -> pd.DataFrame:
        """Reshape a yfinance download result to long format and clean types."""
        if raw is None or raw.empty:
            return pd.DataFrame()

        if isinstance(raw.columns, pd.MultiIndex):
            try:
                stacked = raw.stack(level="Ticker").reset_index()
            except KeyError:
                stacked = raw.stack(level=-1).reset_index()
            cols = stacked.columns.tolist()
            stacked = stacked.rename(columns={cols[0]: "date", cols[1]: "ticker"})
        else:
            stacked = raw.reset_index()
            stacked.columns.name = None
            stacked["ticker"] = tickers[0]
            stacked = stacked.rename(columns={stacked.columns[0]: "date"})

        stacked = stacked.rename(columns={
            "Open": "open", "High": "max", "Low": "min",
            "Close": "close", "Volume": "Trading_Volume",
        })
        stacked["stock_id"] = stacked["ticker"].map(ticker_to_sid)
        stacked["date"] = pd.to_datetime(stacked["date"]).dt.tz_localize(None)

        for col in ["open", "max", "min", "close"]:
            stacked[col] = pd.to_numeric(stacked[col], errors="coerce")
        stacked["Trading_Volume"] = pd.to_numeric(
            stacked.get("Trading_Volume", pd.Series(dtype=float)), errors="coerce"
        )
        stacked = stacked.dropna(subset=["stock_id", "open", "close", "max", "min"])
        # Convert shares → lots (張)
        stacked["Trading_Volume"] = (stacked["Trading_Volume"] / 1000).round(0)
        return stacked

    # ── Step 3: institutional buy/sell (TWSE T86, no auth) ───────────────────

    def get_institutional_data(self, date: str) -> pd.DataFrame:
        """三大法人淨買超，合併 TWSE T86 (上市) + TPEx (上櫃).
        Returns DataFrame with stock_id and net (positive = any institution net bought)."""
        twse_df = self._get_twse_institutional(date)
        otc_df  = self._get_otc_institutional(date)

        if twse_df.empty and otc_df.empty:
            return pd.DataFrame()
        if twse_df.empty:
            return otc_df
        if otc_df.empty:
            return twse_df
        combined = pd.concat([twse_df, otc_df], ignore_index=True)
        return combined.drop_duplicates(subset=["stock_id"]).reset_index(drop=True)

    def _get_twse_institutional(self, date: str) -> pd.DataFrame:
        """TWSE T86 上市三大法人，回傳 stock_id / net / foreign_net / trust_net / dealer_net.
        T86 positional columns (0-indexed):
          [4] 外陸資買賣超, [7] 外資自營買賣超, [10] 投信買賣超,
          [11] 自營商買賣超(合計), [18] 三大法人買賣超."""
        date_str = date.replace("-", "")
        try:
            resp = self.session.get(
                TWSE_T86,
                params={"response": "json", "date": date_str, "selectType": "ALL"},
                timeout=20,
            )
            if resp.status_code != 200:
                return pd.DataFrame()
            body = resp.json()
            if body.get("stat") != "OK":
                return pd.DataFrame()

            raw = body.get("data", [])
            if not raw or len(raw[0]) < 19:
                return pd.DataFrame()

            df = pd.DataFrame(raw)
            # col[0] = 證券代號, col[1] = 名稱
            df = df.rename(columns={0: "stock_id"})
            df["stock_id"] = df["stock_id"].astype(str).str.strip()
            df = df[df["stock_id"].str.match(r"^\d{4}$", na=False)].copy()

            def pos_num(idx):
                return pd.to_numeric(
                    df.iloc[:, idx].astype(str).str.replace(",", "", regex=False)
                    .replace({"--": "0", "": "0"}),
                    errors="coerce",
                ).fillna(0)

            df["foreign_net"] = pos_num(4) + pos_num(7)  # 外陸資 + 外資自營
            df["trust_net"]   = pos_num(10)               # 投信
            df["dealer_net"]  = pos_num(11)               # 自營商合計
            df["net"]         = pos_num(18)               # 三大法人合計

            return df[["stock_id", "net", "foreign_net", "trust_net", "dealer_net"]]

        except Exception as e:
            print(f"  Warning: TWSE institutional data unavailable ({e})")
            return pd.DataFrame()

    def _get_otc_institutional(self, date: str) -> pd.DataFrame:
        """TPEx 上櫃三大法人，回傳 stock_id / net / foreign_net / trust_net / dealer_net.
        Positional columns (0-indexed from row start):
          [10] 外資合計買賣超, [13] 投信買賣超, [22] 自營商合計買賣超, [23] 三大法人合計."""
        from datetime import datetime
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return pd.DataFrame()
        roc_date = f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"
        try:
            resp = self.session.get(
                "https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
                "3itrade_hedge_result.php",
                params={"l": "zh-tw", "o": "json", "se": "EW", "t": "D", "d": roc_date},
                timeout=20,
            )
            if resp.status_code != 200:
                return pd.DataFrame()
            body = resp.json()
            tables = body.get("tables", [])
            if not tables:
                return pd.DataFrame()

            table = tables[0]
            fields = table.get("fields", [])
            rows   = table.get("data", [])
            if not rows or not fields:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=fields)
            stock_col = next((c for c in df.columns if c.strip() == "代號"), None)
            if stock_col is None:
                return pd.DataFrame()

            df = df.rename(columns={stock_col: "stock_id"})
            df["stock_id"] = df["stock_id"].astype(str).str.strip()
            df = df[df["stock_id"].str.match(r"^\d{4}$", na=False)].copy()
            df = df[df["stock_id"].str[0] != "0"].copy()

            def pos_num(idx):
                if idx >= df.shape[1]:
                    return pd.Series(0, index=df.index)
                return pd.to_numeric(
                    df.iloc[:, idx].astype(str).str.replace(",", "", regex=False)
                    .replace({"--": "0", "": "0"}),
                    errors="coerce",
                ).fillna(0)

            df["foreign_net"] = pos_num(10)   # 外資合計買賣超
            df["trust_net"]   = pos_num(13)   # 投信買賣超
            df["dealer_net"]  = pos_num(22)   # 自營商合計買賣超
            df["net"]         = pos_num(23)   # 三大法人合計

            return df[["stock_id", "net", "foreign_net", "trust_net", "dealer_net"]]

        except Exception as e:
            print(f"  Warning: OTC institutional data unavailable ({e})")
            return pd.DataFrame()

    def get_finmind_institutional_one(self, date: str, stock_id: str) -> dict:
        """Per-stock FinMind 三大法人 fallback. anonymous tier 允許單檔查詢
        (bulk 需要付費)，因此設計成「primary 抓不到該檔，再 1 檔 1 檔補」.
        回傳 {'foreign_net', 'trust_net', 'dealer_net', 'net'}, 找不到回 {}."""
        import os
        params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": stock_id,
            "start_date": date,
            "end_date": date,
        }
        token = os.environ.get("FINMIND_TOKEN", "")
        if token:
            params["token"] = token
        try:
            resp = self.session.get(FINMIND_API, params=params, timeout=20)
            if resp.status_code != 200:
                return {}
            body = resp.json()
            if body.get("status") != 200:
                return {}
            rows = body.get("data", [])
            if not rows:
                return {}

            nets = {"Foreign_Investor": 0, "Foreign_Dealer_Self": 0,
                    "Investment_Trust": 0, "Dealer_self": 0, "Dealer_Hedging": 0}
            for r in rows:
                name = r.get("name")
                if name in nets:
                    nets[name] += int(r.get("buy", 0) or 0) - int(r.get("sell", 0) or 0)

            foreign_net = nets["Foreign_Investor"] + nets["Foreign_Dealer_Self"]
            trust_net   = nets["Investment_Trust"]
            dealer_net  = nets["Dealer_self"] + nets["Dealer_Hedging"]
            return {
                "foreign_net": foreign_net,
                "trust_net":   trust_net,
                "dealer_net":  dealer_net,
                "net":         foreign_net + trust_net + dealer_net,
            }
        except Exception as e:
            print(f"  Warning: FinMind fallback failed for {stock_id} ({e})")
            return {}

    # ── Step 3b: industry/sector map (TWSE ISIN pages) ───────────────────────

    def get_sector_map(self) -> dict:
        """Fetch 產業別 for all listed (上市) and OTC (上櫃) stocks.
        Returns {stock_id: "產業別"}, e.g. {"4772": "化學工業"}."""
        import io
        result = {}
        for mode in ["2", "4"]:  # 2=上市, 4=上櫃
            try:
                resp = self.session.get(
                    f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}",
                    timeout=20,
                )
                resp.encoding = "big5"
                dfs = pd.read_html(io.StringIO(resp.text))
                if not dfs:
                    continue
                df = dfs[0]
                for _, row in df.iterrows():
                    cell = str(row.iloc[0]).strip()
                    parts = cell.split("　")  # full-width space
                    if not parts:
                        continue
                    code = parts[0].strip()
                    if len(code) == 4 and code.isdigit():
                        sector = str(row.iloc[4]).strip() if len(row) > 4 else ""
                        if sector and sector.lower() != "nan":
                            result[code] = sector
            except Exception as e:
                print(f"  Warning: sector data unavailable (mode={mode}): {e}")
        return result

    # ── Step 4: intraday 1-minute close prices (yfinance) ────────────────────

    def get_intraday_data(self, stock_ids: list, market_map: dict = None) -> dict:
        """Fetch today's 1-minute close prices for each stock via yfinance.
        Returns {stock_id: {"times": ["09:01", ...], "prices": [123.5, ...]}}."""
        import yfinance as yf

        if not stock_ids:
            return {}
        if market_map is None:
            market_map = {}

        ticker_to_sid = {}
        for sid in stock_ids:
            suffix = "TWO" if market_map.get(sid) == "OTC" else "TW"
            ticker_to_sid[f"{sid}.{suffix}"] = sid

        tickers = list(ticker_to_sid.keys())
        try:
            raw = yf.download(tickers, period="1d", interval="1m",
                              auto_adjust=True, progress=False)
        except Exception as e:
            print(f"  Warning: intraday fetch failed ({e})")
            return {}

        if raw.empty:
            return {}

        if isinstance(raw.columns, pd.MultiIndex):
            try:
                stacked = raw.stack(level="Ticker").reset_index()
            except KeyError:
                stacked = raw.stack(level=-1).reset_index()
            cols = stacked.columns.tolist()
            stacked = stacked.rename(columns={cols[0]: "dt", cols[1]: "ticker"})
        else:
            stacked = raw.reset_index()
            stacked.columns.name = None
            stacked["ticker"] = tickers[0]
            stacked = stacked.rename(columns={stacked.columns[0]: "dt"})

        stacked = stacked.rename(columns={"Close": "close"})
        stacked["stock_id"] = stacked["ticker"].map(ticker_to_sid)
        stacked["close"] = pd.to_numeric(stacked.get("close", pd.Series(dtype=float)), errors="coerce")
        stacked = stacked.dropna(subset=["stock_id", "close"])

        result = {}
        for sid, grp in stacked.groupby("stock_id"):
            grp = grp.sort_values("dt")
            dts = pd.to_datetime(grp["dt"])
            if dts.dt.tz is not None:
                dts = dts.dt.tz_convert("Asia/Taipei")
            else:
                dts = dts.dt.tz_localize("UTC").dt.tz_convert("Asia/Taipei")
            result[str(sid)] = {
                "times":  dts.dt.strftime("%H:%M").tolist(),
                "prices": [round(float(p), 2) for p in grp["close"]],
            }

        return result

    # ── Step 5: inner/outer market ratio (Yahoo Finance Taiwan) ──────────────

    def get_inner_outer_data(self, stock_ids: list, market_map: dict = None) -> pd.DataFrame:
        """Fetch 內外盤 data from Yahoo Finance Taiwan StockServices API.
        Returns DataFrame with columns: stock_id, in_market, out_market, ratio."""
        if not stock_ids:
            return pd.DataFrame()
        if market_map is None:
            market_map = {}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://tw.stock.yahoo.com/",
        }

        def sid_to_ticker(sid):
            suffix = "TWO" if market_map.get(sid) == "OTC" else "TW"
            return f"{sid}.{suffix}"

        ticker_to_sid = {sid_to_ticker(sid): sid for sid in stock_ids}

        # Query in batches of 50 to stay within URL length limits
        batch_size = 50
        tickers = list(ticker_to_sid.keys())
        rows = []

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]
            ts = int(time.time() * 1000)
            syms = "%2C".join(batch)
            url = (
                "https://tw.stock.yahoo.com/_td-stock/api/resource/"
                f"StockServices.stockList"
                f";autoRefresh={ts}"
                f";fields=avgPrice%2Corderbook"
                f";symbols={syms}"
                f"?device=desktop&intl=tw&lang=zh-Hant-TW&region=TW"
                f"&site=finance&tz=Asia%2FTaipei&returnMeta=true"
            )
            try:
                resp = self.session.get(url, headers=headers, timeout=15)
                if resp.status_code != 200:
                    continue
                for item in resp.json().get("data", []):
                    ticker = item.get("symbol", "")
                    sid = ticker_to_sid.get(ticker)
                    if not sid:
                        continue
                    in_m = item.get("inMarket")
                    out_m = item.get("outMarket")
                    if isinstance(in_m, (int, float)) and isinstance(out_m, (int, float)) and out_m > 0:
                        rows.append({
                            "stock_id": sid,
                            "in_market": int(in_m),
                            "out_market": int(out_m),
                            "ratio": round(in_m / out_m, 4),
                        })
            except Exception as e:
                print(f"  Warning: inner/outer data batch {i//batch_size+1} failed ({e})")
                continue

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
