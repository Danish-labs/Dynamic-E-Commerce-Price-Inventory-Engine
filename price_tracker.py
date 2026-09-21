import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


class PriceTracker:
    def __init__(
        self,
        target_url: str,
        selectors: dict[str, str],
        price_threshold: float,
        history_file: str = "price_history.csv",
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        required = {"product_card", "title", "price", "stock"}
        missing = required.difference(selectors)
        if missing:
            raise ValueError(f"Missing selectors: {', '.join(sorted(missing))}")
        if not target_url.startswith(("http://", "https://")):
            raise ValueError("target_url must use http:// or https://")

        self.target_url = target_url
        self.selectors = selectors
        self.price_threshold = float(price_threshold)
        self.history_file = Path(history_file)
        self.telegram_bot_token = telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/131.0.0.0 Safari/537.36"
        )

    @staticmethod
    def _parse_price(value: str) -> float | None:
        value = re.sub(r"[^\d,.-]", "", value.replace("\xa0", "").strip())
        if not value:
            return None
        if "," in value and "." in value:
            value = value.replace(",", "") if value.rfind(".") > value.rfind(",") else value.replace(".", "").replace(",", ".")
        elif "," in value:
            parts = value.split(",")
            value = "".join(parts) if len(parts[-1]) == 3 else ".".join(parts)
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    async def _text(locator: Any) -> str:
        try:
            return (await locator.inner_text(timeout=30000)).strip()
        except (PlaywrightTimeoutError, Exception):
            return ""

    async def _extract(self, page: Page) -> list[dict[str, Any]]:
        cards = page.locator(self.selectors["product_card"])
        results: list[dict[str, Any]] = []
        try:
            count = await cards.count()
        except Exception:
            return results

        for index in range(count):
            card = cards.nth(index)
            title = await self._text(card.locator(self.selectors["title"]))
            price_text = await self._text(card.locator(self.selectors["price"]))
            stock = await self._text(card.locator(self.selectors["stock"]))
            results.append(
                {
                    "title": title or "Unknown product",
                    "price": self._parse_price(price_text),
                    "stock": stock or "Unknown",
                    "url": self.target_url,
                }
            )
        return results

    def _history(self) -> pd.DataFrame:
        if not self.history_file.exists() or self.history_file.stat().st_size == 0:
            return pd.DataFrame(columns=["timestamp", "url", "title", "price", "stock"])
        try:
            return pd.read_csv(self.history_file)
        except (OSError, pd.errors.ParserError):
            return pd.DataFrame(columns=["timestamp", "url", "title", "price", "stock"])

    @staticmethod
    def _same_product(history: pd.DataFrame, item: dict[str, Any]) -> pd.DataFrame:
        if history.empty:
            return history
        matches = history[history["url"].eq(item["url"]) & history["title"].eq(item["title"])]
        return matches.sort_values("timestamp")

    async def _send_alert(self, item: dict[str, Any], old_price: Any, stock_changed: bool) -> None:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return
        old = "N/A" if pd.isna(old_price) else f"${float(old_price):.2f}"
        new = "N/A" if item["price"] is None else f"${item['price']:.2f}"
        message = (
            "🚨 *PRICE DROP ALERT* 🚨\n"
            f"*Product:* {item['title']}\n"
            f"*Old Price:* {old} -> *New Price:* {new}\n"
            f"*Link:* [Buy Now]({item['url']})"
        )
        if stock_changed:
            message += f"\n*Stock:* {item['stock']}"
        endpoint = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        await asyncio.to_thread(requests.post, endpoint, data=payload, timeout=30)

    async def run(self) -> list[dict[str, Any]]:
        load_dotenv()
        history = self._history()
        timestamp = datetime.now(timezone.utc).isoformat()
        async with async_playwright() as playwright:
            browser: Browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                page = await context.new_page()
                await page.goto(self.target_url, wait_until="domcontentloaded", timeout=30000)
                items = await self._extract(page)
                await context.close()
            finally:
                await browser.close()

        rows = [
            {
                "timestamp": timestamp,
                "url": item["url"],
                "title": item["title"],
                "price": item["price"],
                "stock": item["stock"],
            }
            for item in items
        ]
        if rows:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_csv(
                self.history_file,
                mode="a",
                header=not self.history_file.exists() or self.history_file.stat().st_size == 0,
                index=False,
            )

        for item in items:
            previous = self._same_product(history, item)
            old_price = previous.iloc[-1]["price"] if not previous.empty else float("nan")
            old_stock = previous.iloc[-1]["stock"] if not previous.empty else None
            stock_changed = old_stock is not None and old_stock != item["stock"]
            price_triggered = item["price"] is not None and item["price"] < self.price_threshold
            if price_triggered or stock_changed:
                await self._send_alert(item, old_price, stock_changed)
        return items


async def main() -> None:
    load_dotenv()
    selectors = {
        "product_card": os.environ["PRODUCT_CARD_SELECTOR"],
        "title": os.environ["TITLE_SELECTOR"],
        "price": os.environ["PRICE_SELECTOR"],
        "stock": os.environ["STOCK_SELECTOR"],
    }
    tracker = PriceTracker(
        target_url=os.environ["TARGET_URL"],
        selectors=selectors,
        price_threshold=float(os.environ["PRICE_THRESHOLD"]),
    )
    await tracker.run()


if __name__ == "__main__":
    asyncio.run(main())