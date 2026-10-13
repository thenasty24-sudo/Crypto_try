"""
Robinhood crypto bot: buys and sells a fixed dollar amount of one coin,
at least TRADES_PER_DAY times per day, using the official Robinhood
Crypto Trading API (https://docs.robinhood.com/crypto/trading/).

Cycle:
  - Trades are spaced evenly: one "slot" every 24h / TRADES_PER_DAY.
  - When flat, it buys TRADE_USD of the coin at the next slot.
  - When holding, it sells everything it bought at the next slot, or
    earlier if TAKE_PROFIT_PCT or STOP_LOSS_PCT is hit.
  - Every slot produces a trade, so you get at least TRADES_PER_DAY
    trades per day (early take-profit/stop-loss sells add extra trades).
  - It stops trading for the day once realized losses reach MAX_DAILY_LOSS_USD.

DRY_RUN=true (the default) simulates orders at live quotes and never
places a real order.
"""

import base64
import datetime as dt
import json
import logging
import os
import sys
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE_URL = "https://trading.robinhood.com"

log = logging.getLogger("bot")


def env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self):
        self.api_key = os.getenv("RH_API_KEY", "")
        self.private_key_b64 = os.getenv("RH_PRIVATE_KEY", "")
        self.symbol = os.getenv("SYMBOL", "BTC-USD")
        self.trade_usd = Decimal(os.getenv("TRADE_USD", "10"))
        self.trades_per_day = int(os.getenv("TRADES_PER_DAY", "12"))
        self.take_profit_pct = Decimal(os.getenv("TAKE_PROFIT_PCT", "1.0"))
        self.stop_loss_pct = Decimal(os.getenv("STOP_LOSS_PCT", "1.5"))
        self.max_daily_loss_usd = Decimal(os.getenv("MAX_DAILY_LOSS_USD", "5"))
        self.poll_seconds = int(os.getenv("POLL_SECONDS", "30"))
        self.dry_run = env_bool("DRY_RUN", True)
        self.state_file = Path(os.getenv("STATE_FILE", "bot_state.json"))

        if self.trades_per_day < 10:
            raise SystemExit("TRADES_PER_DAY must be at least 10")
        if not self.api_key or not self.private_key_b64:
            raise SystemExit("Set RH_API_KEY and RH_PRIVATE_KEY in .env")

    @property
    def slot_seconds(self):
        return 86400 / self.trades_per_day


class RobinhoodCrypto:
    """Minimal signed client for the Robinhood Crypto Trading API."""

    def __init__(self, api_key, private_key_b64):
        self.api_key = api_key
        self.signing_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
        self.session = requests.Session()

    def _headers(self, method, path, body):
        timestamp = str(int(time.time()))
        message = f"{self.api_key}{timestamp}{path}{method}{body}"
        signature = self.signing_key.sign(message.encode())
        return {
            "x-api-key": self.api_key,
            "x-timestamp": timestamp,
            "x-signature": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    def request(self, method, path, payload=None):
        body = json.dumps(payload) if payload is not None else ""
        resp = self.session.request(
            method,
            BASE_URL + path,
            headers=self._headers(method, path, body),
            data=body or None,
            timeout=15,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.text else {}

    def account(self):
        return self.request("GET", "/api/v1/crypto/trading/accounts/")

    def trading_pair(self, symbol):
        return self.request("GET", f"/api/v1/crypto/trading/trading_pairs/?symbol={symbol}")["results"][0]

    def best_bid_ask(self, symbol):
        return self.request("GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={symbol}")["results"][0]

    def place_market_order(self, symbol, side, asset_quantity):
        return self.request("POST", "/api/v1/crypto/trading/orders/", {
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "symbol": symbol,
            "type": "market",
            "market_order_config": {"asset_quantity": str(asset_quantity)},
        })

    def get_order(self, order_id):
        return self.request("GET", f"/api/v1/crypto/trading/orders/{order_id}/")


class Bot:
    def __init__(self, cfg, client):
        self.cfg = cfg
        self.client = client
        self.state = self._load_state()
        pair = client.trading_pair(cfg.symbol)
        self.asset_increment = Decimal(pair["asset_increment"])
        self.min_order_size = Decimal(pair.get("min_order_size") or "0")

    # ---- state ---------------------------------------------------------

    def _load_state(self):
        if self.cfg.state_file.exists():
            return json.loads(self.cfg.state_file.read_text())
        return {"day": "", "trades_today": 0, "pnl_today": "0",
                "position_qty": "0", "entry_price": "0", "last_trade_ts": 0}

    def _save_state(self):
        self.cfg.state_file.write_text(json.dumps(self.state, indent=2))

    def _roll_day(self):
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        if self.state["day"] != today:
            if self.state["day"]:
                log.info("Day %s closed: %d trades, P&L $%s", self.state["day"],
                         self.state["trades_today"], self.state["pnl_today"])
            self.state.update(day=today, trades_today=0, pnl_today="0")
            self._save_state()

    # ---- orders --------------------------------------------------------

    def _execute(self, side, qty, quote_price):
        """Place a market order and return the average fill price."""
        if self.cfg.dry_run:
            log.info("[DRY RUN] %s %s %s @ ~%s", side.upper(), qty, self.cfg.symbol, quote_price)
            return quote_price

        order = self.client.place_market_order(self.cfg.symbol, side, qty)
        order_id = order["id"]
        for _ in range(30):
            order = self.client.get_order(order_id)
            if order["state"] == "filled":
                price = Decimal(str(order.get("average_price") or quote_price))
                log.info("%s %s %s filled @ %s", side.upper(), qty, self.cfg.symbol, price)
                return price
            if order["state"] in ("canceled", "failed"):
                raise RuntimeError(f"Order {order_id} {order['state']}")
            time.sleep(2)
        raise RuntimeError(f"Order {order_id} not filled after 60s (state={order['state']})")

    def _record_trade(self):
        self.state["trades_today"] += 1
        self.state["last_trade_ts"] = time.time()
        self._save_state()

    def buy(self, ask):
        qty = (self.cfg.trade_usd / ask).quantize(self.asset_increment, rounding=ROUND_DOWN)
        if qty <= 0 or qty < self.min_order_size:
            log.warning("TRADE_USD $%s is below the minimum order size for %s", self.cfg.trade_usd, self.cfg.symbol)
            return
        price = self._execute("buy", qty, ask)
        self.state.update(position_qty=str(qty), entry_price=str(price))
        self._record_trade()

    def sell(self, bid, reason):
        qty = Decimal(self.state["position_qty"])
        entry = Decimal(self.state["entry_price"])
        log.info("Selling (%s)", reason)
        price = self._execute("sell", qty, bid)
        pnl = (price - entry) * qty
        self.state["pnl_today"] = str(Decimal(self.state["pnl_today"]) + pnl)
        self.state.update(position_qty="0", entry_price="0")
        self._record_trade()
        log.info("Round trip P&L $%.4f | today: %d trades, P&L $%.4f",
                 pnl, self.state["trades_today"], Decimal(self.state["pnl_today"]))

    # ---- main loop -----------------------------------------------------

    def tick(self):
        self._roll_day()
        quote = self.client.best_bid_ask(self.cfg.symbol)
        bid = Decimal(quote["bid_inclusive_of_sell_spread"])
        ask = Decimal(quote["ask_inclusive_of_buy_spread"])

        holding = Decimal(self.state["position_qty"]) > 0
        slot_due = time.time() - self.state["last_trade_ts"] >= self.cfg.slot_seconds
        loss_limit_hit = -Decimal(self.state["pnl_today"]) >= self.cfg.max_daily_loss_usd

        if holding:
            entry = Decimal(self.state["entry_price"])
            change_pct = (bid - entry) / entry * 100
            if change_pct >= self.cfg.take_profit_pct:
                self.sell(bid, f"take profit {change_pct:.2f}%")
            elif change_pct <= -self.cfg.stop_loss_pct:
                self.sell(bid, f"stop loss {change_pct:.2f}%")
            elif slot_due:
                self.sell(bid, f"scheduled slot, {change_pct:+.2f}%")
        elif slot_due:
            if loss_limit_hit:
                log.warning("Daily loss limit $%s reached; no new buys until tomorrow (UTC)",
                            self.cfg.max_daily_loss_usd)
                return
            self.buy(ask)

    def run(self):
        mode = "DRY RUN" if self.cfg.dry_run else "LIVE"
        log.info("Starting %s: %s, $%s per trade, >= %d trades/day (one every %.0f min)",
                 mode, self.cfg.symbol, self.cfg.trade_usd, self.cfg.trades_per_day,
                 self.cfg.slot_seconds / 60)
        while True:
            try:
                self.tick()
            except Exception as e:  # keep running through transient API errors
                log.error("Tick failed: %s", e)
            time.sleep(self.cfg.poll_seconds)


def main():
    load_dotenv()                # secrets on a phone/server install
    load_dotenv("settings.env")  # trading settings in the GitHub repo
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")])
    cfg = Config()
    client = RobinhoodCrypto(cfg.api_key, cfg.private_key_b64)
    client.account()  # fails fast if the API keys are wrong
    bot = Bot(cfg, client)
    if "--once" in sys.argv:  # scheduled runs (GitHub Actions): one check, then exit
        bot.tick()
    else:
        bot.run()


if __name__ == "__main__":
    main()
