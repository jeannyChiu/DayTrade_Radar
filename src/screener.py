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

        if today["close"] <= today["open"]:          # not a red K
            return False
        if not (prev3["close"] < prev3["open"]).all():  # not all black K
            return False
        if today["close"] <= prev3["max"].max():     # doesn't eat the highs
            return False
        return True

    def _breakout_tangled_ma(self, g: pd.DataFrame) -> bool:
        """突破糾結均線: MAs tangled (spread ≤ 2%) yesterday, today breaks above all
        three MAs with ≥ 4% gain, AND either:
          (a) yesterday's close was within/below the MA band (≤ 1% above max MA), or
          (b) tanglement has been sustained for ≥ 5 consecutive days ending yesterday.
        Condition (a) filters stocks whose price had already escaped the MA band before
        the breakout day — those are false positives where MAs happened to converge
        briefly rather than a genuine consolidation."""
        if len(g) < 21:
            return False
        closes = g["close"]
        ma5 = closes.rolling(5).mean()
        ma10 = closes.rolling(10).mean()
        ma20 = closes.rolling(20).mean()

        if pd.isna(ma20.iloc[-2]):
            return False

        # Yesterday's MAs must be tangled (spread ≤ 2%)
        ma_prev = [ma5.iloc[-2], ma10.iloc[-2], ma20.iloc[-2]]
        if any(pd.isna(v) for v in ma_prev):
            return False
        spread = (max(ma_prev) - min(ma_prev)) / min(ma_prev)
        if spread > 0.02:
            return False

        today_close = closes.iloc[-1]
        prev_close = closes.iloc[-2]

        # Today must break above all three MAs
        if not (today_close > ma5.iloc[-1] and
                today_close > ma10.iloc[-1] and
                today_close > ma20.iloc[-1]):
            return False

        # Change must be ≥ 4%
        if (today_close - prev_close) / prev_close < 0.04:
            return False

        # Condition (a): price was within the MA band yesterday (not already above all MAs)
        prev_max_ma = max(ma_prev)
        if prev_close <= prev_max_ma * 1.01:
            return True

        # Condition (b): ≥ 5 consecutive days of tanglement ending yesterday
        streak = 0
        for i in range(-2, -len(g), -1):
            m5, m10, m20 = ma5.iloc[i], ma10.iloc[i], ma20.iloc[i]
            if pd.isna(m5) or pd.isna(m10) or pd.isna(m20):
                break
            s = (max(m5, m10, m20) - min(m5, m10, m20)) / min(m5, m10, m20)
            if s <= 0.02:
                streak += 1
            else:
                break
        return streak >= 5

    # ── Priority-2 conditions ─────────────────────────────────────────────────

    def _inner_trapped_reversal(self, g: pd.DataFrame) -> bool:
        """內困三日翻紅: the two days before today are long black K candles,
        yesterday's range is contained within day-2's range,
        and today breaks above both previous highs."""
        if len(g) < 3:
            return False
        d2 = g.iloc[-3]   # 2 days ago (long black)
        d1 = g.iloc[-2]   # yesterday (inner bar, also black)
        today = g.iloc[-1]

        # Both must be black K
        if d2["close"] >= d2["open"] or d1["close"] >= d1["open"]:
            return False

        # d2 must be a long black (body ≥ 50% of candle range)
        body2 = d2["open"] - d2["close"]
        range2 = d2["max"] - d2["min"]
        if range2 == 0 or body2 / range2 < 0.5:
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
