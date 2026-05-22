from dataclasses import dataclass, field
from typing import List, Set
import pandas as pd


@dataclass
class StockResult:
    stock_id: str
    name: str
    close: float
    change_pct: float
    volume: int
    priority: int
    conditions: List[str] = field(default_factory=list)


class Screener:
    def screen(
        self,
        price_df: pd.DataFrame,
        institutional_df: pd.DataFrame,
        today: str,
        name_map: dict = None,
        top100_change: Set[str] = None,
        top100_volume: Set[str] = None,
        inner_outer_df: pd.DataFrame = None,
    ) -> dict:
        if name_map is None:
            name_map = {}

        today_dt = pd.to_datetime(today)

        today_price = price_df[price_df["date"] == today_dt]
        if today_price.empty:
            return {"p1": [], "p2": [], "p3": []}

        # Previous day's close for accurate % change calculation
        prev_price = (
            price_df[price_df["date"] < today_dt]
            .sort_values("date")
            .groupby("stock_id")
            .last()
            .reset_index()[["stock_id", "close"]]
            .rename(columns={"close": "prev_close"})
        )

        today_ext = today_price.merge(prev_price, on="stock_id", how="left")
        today_ext["change_pct"] = (
            (today_ext["close"] - today_ext["prev_close"]) / today_ext["prev_close"]
        )

        # Exclude limit-down (跌停 ≤ -9.9%) and apply price filter 100–500
        today_ext = today_ext[
            (today_ext["change_pct"] > -0.099)
            & (today_ext["close"] >= 100)
            & (today_ext["close"] <= 500)
        ]

        # Use full-market rankings if provided; otherwise fall back to local ranking
        if top100_change is None:
            top100_change = set(today_ext.nlargest(100, "change_pct")["stock_id"])
        if top100_volume is None:
            top100_volume = set(today_ext.nlargest(100, "Trading_Volume")["stock_id"])

        # {stock_id: ["外資買超", "投信買超", "自營商買超"]} — only buying institutions
        inst_label_map: dict = {}
        if not institutional_df.empty:
            for _, row in institutional_df.iterrows():
                labels = []
                if row.get("foreign_net", 0) >= 1000:
                    labels.append("外資買超")
                if row.get("trust_net", 0) >= 1000:
                    labels.append("投信買超")
                if row.get("dealer_net", 0) >= 1000:
                    labels.append("自營商買超")
                if labels:
                    inst_label_map[row["stock_id"]] = labels

        # inner/outer ratio < 0.66 AND total > 300 張 → bullish buy-side pressure
        inner_outer_bullish: Set[str] = set()
        if inner_outer_df is not None and not inner_outer_df.empty:
            io = inner_outer_df[
                (inner_outer_df["ratio"] < 0.66)
                & ((inner_outer_df["in_market"] + inner_outer_df["out_market"]) > 300)
            ]
            inner_outer_bullish = set(io["stock_id"])

        groups = {sid: grp.reset_index(drop=True) for sid, grp in price_df.groupby("stock_id")}
        today_lookup = today_ext.set_index("stock_id")[["close", "change_pct", "Trading_Volume"]]

        results: dict = {"p1": [], "p2": [], "p3": []}

        for stock_id, group in groups.items():
            if stock_id not in today_lookup.index:
                continue
            row = today_lookup.loc[stock_id]
            close = float(row["close"])
            change_pct = float(row["change_pct"])
            volume = int(row["Trading_Volume"])
            if volume < 1000:
                continue
            name = name_map.get(stock_id, "")

            p1_hits = []
            p2_hits = []

            if self._red_eats_three_black(group):
                p1_hits.append("一紅吃三黑")
            if self._breakout_tangled_ma(group):
                p1_hits.append("突破糾結均線")

            if stock_id in top100_change:
                p2_hits.append("漲幅前百")
            if stock_id in top100_volume:
                p2_hits.append("量能前百")
            if self._inner_trapped_reversal(group):
                p2_hits.append("內困三日翻紅")
            if stock_id in inner_outer_bullish:
                p2_hits.append("買氣強大")

            inst_labels = inst_label_map.get(stock_id, [])
            is_inst_buy = len(inst_labels) > 0

            if p1_hits:
                results["p1"].append(
                    StockResult(stock_id, name, close, change_pct, volume, 1,
                                p1_hits + inst_labels)
                )
            elif self._qualifies_p2(p2_hits, is_inst_buy):
                results["p2"].append(
                    StockResult(stock_id, name, close, change_pct, volume, 2,
                                p2_hits + inst_labels)
                )

        for key in results:
            results[key].sort(key=lambda x: (round(x.change_pct, 4), len(x.conditions)), reverse=True)

        return results

    # ── Priority-1 conditions ─────────────────────────────────────────────────

    def _red_eats_three_black(self, g: pd.DataFrame) -> bool:
        """一紅吃三黑: today is red K, previous 3 days are all black K,
        and today's close > max high of those 3 black days."""
        if len(g) < 4:
            return False
        today = g.iloc[-1]
        prev3 = g.iloc[-4:-1]

        # close >= open allows 漲停一字/T字 (body=0 but visually red 漲停)
        if today["close"] < today["open"]:           # not a red K
            return False
        if not (prev3["close"] < prev3["open"]).all():  # not all black K
            return False
        if today["close"] <= prev3["max"].max():     # doesn't eat the highs
            return False
        return True

    def _breakout_tangled_ma(self, g: pd.DataFrame) -> bool:
        """突破糾結均線: today breaks above all three MAs with ≥ 4% gain, AND one of:
          (a) prev_spread ≤ 2.5% AND prev_close ∈ [dyn_low, min_MA*1.015], where
              dyn_low = min_MA*0.985 if today's MAs are already tight (≤ 2%) else
              min_MA → classic band-proximity breakout (the dynamic lower bound
              keeps cases like 5243 5/21 where prev_close is 0.85% below min_MA
              but today_spread collapsed to 1.56% — a real tangle-break — while
              still rejecting 1513 5/21 where prev_close is 1% below min_MA AND
              today_spread is still 2.03%, i.e. a bounce-from-below), or
          (a') prev_spread ≤ 1.5% (very tight tangle) AND prev_close ≤ min_MA*1.015 →
              MAs are genuinely clustered, allow prev_close further below the band, or
          (b) prev_spread ≤ 2.5% AND prev_close ≤ max_MA AND tanglement (≤ 2%)
              sustained for ≥ 2 days → near-band continuation breakout, or
          (c) today's K pulled MAs into a tight band (today_spread ≤ 2%) and that
              spread shrank to ≤ 70% of yesterday's, AND prev_close was already
              at or above min_MA → convergence-driven breakout (e.g. 6862/3042/3708
              5/21 where MAs were still in 空頭排列 spread 2.8~4.5% but today's
              red K collapsed the band to ~1~2%), or
          (d) prev_spread ≤ 2.5% AND prev_close ∈ [min_MA, max_MA*1.02] AND
              today_spread ≤ 2% → V-reversal continuation: yesterday's MAs were
              already tangled and yesterday's reversal K just stood on top of
              the band, today's continuation gain is still a fresh tangle-break
              (e.g. 8039/1773 5/22 where 5/18-20 plunged into the tangle, 5/21's
              red K closed just above max_MA, 5/22 followed with another ≥4% gain).
        The streak check on (b) filters out single-day borderline tanglement
        where MAs only just converged the day before today's gain.
        cond (c) requires prev_close ≥ min_MA so post-plunge bounces (where MAs
        haven't caught up yet) don't slip through via today's mechanical convergence."""
        if len(g) < 21:
            return False
        closes = g["close"]
        ma5 = closes.rolling(5).mean()
        ma10 = closes.rolling(10).mean()
        ma20 = closes.rolling(20).mean()

        if pd.isna(ma20.iloc[-2]):
            return False

        ma_prev = [ma5.iloc[-2], ma10.iloc[-2], ma20.iloc[-2]]
        ma_today = [ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]]
        if any(pd.isna(v) for v in ma_prev) or any(pd.isna(v) for v in ma_today):
            return False
        prev_spread = (max(ma_prev) - min(ma_prev)) / min(ma_prev)
        today_spread = (max(ma_today) - min(ma_today)) / min(ma_today)

        today_close = closes.iloc[-1]
        prev_close = closes.iloc[-2]

        # Today must break above all three MAs
        if not (today_close > ma_today[0] and
                today_close > ma_today[1] and
                today_close > ma_today[2]):
            return False

        # Change must be ≥ 4%
        if (today_close - prev_close) / prev_close < 0.04:
            return False

        prev_min_ma = min(ma_prev)
        prev_max_ma = max(ma_prev)

        # (c) convergence-driven breakout: today's K collapsed the MA spread.
        # Independent of yesterday's spread — allows cases where MAs were still
        # in 空頭排列 yesterday but today's gain pulled them into a tight band.
        if (today_spread <= 0.02
                and today_spread <= prev_spread * 0.70
                and prev_close >= prev_min_ma):
            return True

        # The remaining (a)/(a')/(b) paths require yesterday's MAs to already be tangled.
        if prev_spread > 0.025:
            return False

        # (a) classic band-proximity breakout: prev_close near min_MA.
        # Lower bound is dynamic — if today's MAs are already tight (≤ 2%),
        # we accept prev_close slightly below the band (real tangle-break in
        # progress); otherwise prev_close must sit at/above min_MA (bounce-
        # from-below rejected).
        dyn_low = prev_min_ma * 0.985 if today_spread <= 0.02 else prev_min_ma
        if dyn_low <= prev_close <= prev_min_ma * 1.015:
            return True

        # (a') very tight tangle: spread ≤ 1.5% means MAs are genuinely clustered,
        # so allow prev_close further below the band (e.g. 3169 with spread 1.42%
        # and prev_close 4.5% below min_MA — still a real tangle-break).
        if prev_spread <= 0.015 and prev_close <= prev_min_ma * 1.015:
            return True

        # (d) V-reversal continuation: yesterday's MAs already tangled and
        # yesterday's K just stood on top of the band (prev_close within
        # +2% of max_MA), and today's MAs are still tangled. Catches the
        # "plunge → reversal day → continuation gain" pattern where (a)/(b)
        # all fail because prev_close already cleared max_MA (e.g. 8039/1773
        # 5/22 — 5/21 reversal red K closed just above max_MA, 5/22 followed
        # with another ≥4% gain). today_spread ≤ 2% guards against cases
        # where MAs have already diverged.
        if (prev_min_ma <= prev_close <= prev_max_ma * 1.02
                and today_spread <= 0.02):
            return True

        # (b) near-band continuation: yesterday's close must still be inside
        # the band (≤ max MA). Above the band = already broken out — not a
        # fresh-breakout signal.
        if prev_close > prev_max_ma:
            return False

        # (b) prerequisite: sustained tangling (≤ 2%) for ≥ 2 consecutive days
        # ending yesterday — rules out single-day borderline tanglement.
        streak = 0
        for i in range(-2, -len(g), -1):
            m5_i, m10_i, m20_i = ma5.iloc[i], ma10.iloc[i], ma20.iloc[i]
            if pd.isna(m5_i) or pd.isna(m10_i) or pd.isna(m20_i):
                break
            s = (max(m5_i, m10_i, m20_i) - min(m5_i, m10_i, m20_i)) / min(m5_i, m10_i, m20_i)
            if s <= 0.02:
                streak += 1
            else:
                break
        return streak >= 2

    # ── Priority-2 conditions ─────────────────────────────────────────────────

    def _inner_trapped_reversal(self, g: pd.DataFrame) -> bool:
        """內困三日翻紅: 2 days ago is a long black K (the "trap"),
        yesterday's range is contained within day-2's range (inside bar),
        and today breaks above both previous highs.
        Yesterday's color is not constrained — a small red inside bar is
        actually a stronger reversal hint."""
        if len(g) < 3:
            return False
        d2 = g.iloc[-3]   # 2 days ago (long black)
        d1 = g.iloc[-2]   # yesterday (inside bar, any color)
        today = g.iloc[-1]

        # d2 must be a black K
        if d2["close"] >= d2["open"]:
            return False

        # d2 must be a long black (body ≥ 65% of candle range)
        body2 = d2["open"] - d2["close"]
        range2 = d2["max"] - d2["min"]
        if range2 == 0 or body2 / range2 < 0.65:
            return False

        # d1's high-low must be inside d2's high-low (inner bar)
        if not (d1["max"] <= d2["max"] and d1["min"] >= d2["min"]):
            return False

        # Today breaks above both previous highs
        if today["close"] <= max(d2["max"], d1["max"]):
            return False

        return True

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _qualifies_p2(self, hits: List[str], is_inst_buy: bool) -> bool:
        """A stock enters P2 if it satisfies at least two distinct P2 signals."""
        rank_hits = sum(1 for h in hits if h in ("漲幅前百", "量能前百"))
        pattern_hits = sum(1 for h in hits if h in ("內困三日翻紅", "買氣強大"))
        inst = 1 if is_inst_buy else 0
        return (rank_hits + pattern_hits + inst) >= 2
