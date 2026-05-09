import html
import requests
from typing import List
from src.screener import StockResult

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MSG_LEN = 4000
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
LINE_MAX_MSG_LEN = 4500


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send_report(self, results: dict, date: str):
        p1: List[StockResult] = results.get("p1", [])
        p2: List[StockResult] = results.get("p2", [])

        lines = [f"<b>📊 台股當沖選股報告 {date}</b>\n"]

        lines.append("🥇 <b>第一優先</b>（一紅吃三黑 / 突破糾結均線）")
        if p1:
            lines += [self._fmt(r) for r in p1]
        else:
            lines.append("  • 今日無符合個股")

        lines.append("\n🥈 <b>第二優先</b>（量價+型態）")
        if p2:
            lines += [self._fmt(r) for r in p2[:15]]
        else:
            lines.append("  • 今日無符合個股")

        shown = len(p1) + min(len(p2), 15)
        total = len(p1) + len(p2)
        lines.append(f"\n顯示 <b>{shown}</b> 檔 / 共 <b>{total}</b> 檔候選股")
        lines.append("⚠️ 僅供參考，請自行評估風險")

        message = "\n".join(lines)
        self._send_chunked(message)

    def _fmt(self, r: StockResult) -> str:
        sign = "+" if r.change_pct >= 0 else ""
        tags = " | ".join(html.escape(c) for c in r.conditions)
        return (
            f"  • <b>{r.stock_id} {r.name}</b>  "
            f"收:{r.close:.0f}元  "
            f"{sign}{r.change_pct:.2%}  "
            f"量:{r.volume:,.0f}張  "
            f"[{tags}]"
        )

    def _send(self, text: str):
        url = TELEGRAM_URL.format(token=self.token)
        resp = requests.post(
            url,
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")

    def _send_chunked(self, message: str):
        if len(message) <= MAX_MSG_LEN:
            self._send(message)
            return
        # Split by newline, accumulate until near the limit
        buffer, current = [], 0
        for line in message.split("\n"):
            if current + len(line) + 1 > MAX_MSG_LEN:
                self._send("\n".join(buffer))
                buffer, current = [], 0
            buffer.append(line)
            current += len(line) + 1
        if buffer:
            self._send("\n".join(buffer))


class LineNotifier:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def send_report(self, results: dict, date: str):
        p1: List[StockResult] = results.get("p1", [])
        p2: List[StockResult] = results.get("p2", [])

        lines = [f"📊 台股當沖選股報告 {date}\n"]

        lines.append("🥇 第一優先（一紅吃三黑 / 突破糾結均線）")
        if p1:
            lines += [self._fmt(r) for r in p1]
        else:
            lines.append("  • 今日無符合個股")

        lines.append("\n🥈 第二優先（量價+型態）")
        if p2:
            lines += [self._fmt(r) for r in p2[:15]]
        else:
            lines.append("  • 今日無符合個股")

        shown = len(p1) + min(len(p2), 15)
        total = len(p1) + len(p2)
        lines.append(f"\n顯示 {shown} 檔 / 共 {total} 檔候選股")
        lines.append("⚠️ 僅供參考，請自行評估風險")

        message = "\n".join(lines)
        self._send_chunked(message)

    def _fmt(self, r: StockResult) -> str:
        sign = "+" if r.change_pct >= 0 else ""
        tags = " | ".join(r.conditions)
        return (
            f"  • {r.stock_id} {r.name}  "
            f"收:{r.close:.0f}元  "
            f"{sign}{r.change_pct:.2%}  "
            f"量:{r.volume:,.0f}張  "
            f"[{tags}]"
        )

    def _send(self, text: str):
        resp = requests.post(
            LINE_BROADCAST_URL,
            headers=self.headers,
            json={"messages": [{"type": "text", "text": text}]},
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"LINE error {resp.status_code}: {resp.text}")

    def _send_chunked(self, message: str):
        if len(message) <= LINE_MAX_MSG_LEN:
            self._send(message)
            return
        buffer, current = [], 0
        for line in message.split("\n"):
            if current + len(line) + 1 > LINE_MAX_MSG_LEN:
                self._send("\n".join(buffer))
                buffer, current = [], 0
            buffer.append(line)
            current += len(line) + 1
        if buffer:
            self._send("\n".join(buffer))
