"""
binance_client.py — Singleton-клієнт для Binance Futures API.
Підтримує Testnet та Mainnet, retry-логіку на всі запити.
"""

import time
import functools
from typing import Optional, Any

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

import config
from logger import log


# ─── Retry-декоратор ──────────────────────────────────────────────────────────

def retry(max_attempts: int = config.API_RETRY_COUNT,
          delay: float = config.API_RETRY_DELAY):
    """Декоратор: повторює виклик при помилці з'єднання / API."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (BinanceAPIException, BinanceRequestException,
                        ConnectionError, TimeoutError) as exc:
                    last_exc = exc
                    log.warning(
                        "Спроба %d/%d — %s: %s",
                        attempt, max_attempts, func.__name__, exc
                    )
                    if attempt < max_attempts:
                        time.sleep(delay)
            log.error("Усі %d спроби вичерпано для %s", max_attempts, func.__name__)
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


# ─── BinanceClient ────────────────────────────────────────────────────────────

class BinanceClient:
    """
    Тонка обгортка навколо python-binance Client.
    Ініціалізується один раз, далі використовується як синглтон.
    """

    _instance: Optional["BinanceClient"] = None

    def __new__(cls) -> "BinanceClient":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:  # type: ignore[has-type]
            return

        testnet_url = "https://testnet.binancefuture.com"

        if config.TESTNET:
            self.client = Client(
                api_key=config.API_KEY,
                api_secret=config.API_SECRET,
                testnet=True,
            )
            # python-binance потребує явного URL для ф'ючерсного testnet
            self.client.FUTURES_URL = testnet_url
            log.info("🔧 Режим: TESTNET (%s)", testnet_url)
        else:
            self.client = Client(
                api_key=config.API_KEY,
                api_secret=config.API_SECRET,
            )
            log.info("🚀 Режим: MAINNET")

        self._initialized = True

    # ─── Акаунт ───────────────────────────────────────────────────────────────

    @retry()
    def get_futures_balance(self) -> float:
        """Повертає доступний USDT-баланс на ф'ючерсному акаунті."""
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    @retry()
    def get_balance_details(self) -> dict:
        """
        Повертає детальну інформацію про USDT-баланс:
          available  — доступний баланс (для відкриття позицій)
          wallet     — баланс гаманця (без нереалізованого P&L)
        """
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return {
                    "available": float(b["availableBalance"]),
                    "wallet":    float(b["balance"]),
                }
        return {"available": 0.0, "wallet": 0.0}

    @retry()
    def get_open_positions(self) -> list[dict]:
        """Повертає список відкритих позицій (де positionAmt != 0)."""
        positions = self.client.futures_position_information()
        return [p for p in positions if float(p["positionAmt"]) != 0]

    # ─── Ринкові дані ─────────────────────────────────────────────────────────

    @retry()
    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[list]:
        """Повертає список OHLCV-свічок для символу."""
        return self.client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

    @retry()
    def get_ticker_24h(self, symbol: str) -> dict:
        """Повертає 24-годинну статистику для символу."""
        return self.client.futures_ticker(symbol=symbol)  # type: ignore[return-value]

    @retry()
    def get_symbol_price(self, symbol: str) -> float:
        """Повертає поточну ціну символу."""
        ticker = self.client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    @retry()
    def get_exchange_info(self) -> dict:
        """Повертає інформацію про всі ф'ючерсні пари."""
        return self.client.futures_exchange_info()  # type: ignore[return-value]

    # ─── Ордери ───────────────────────────────────────────────────────────────

    @retry()
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Встановлює плече для символу."""
        return self.client.futures_change_leverage(  # type: ignore[return-value]
            symbol=symbol, leverage=leverage
        )

    @retry()
    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Any:
        """Встановлює тип маржі (ISOLATED / CROSSED)."""
        try:
            return self.client.futures_change_margin_type(
                symbol=symbol, marginType=margin_type
            )
        except BinanceAPIException as e:
            # Код -4046: тип маржі вже встановлено — не помилка
            if e.code == -4046:
                log.debug("%s: тип маржі вже %s", symbol, margin_type)
                return None
            raise

    @retry()
    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """
        Відкриває ринковий ордер.
        side: 'BUY' або 'SELL'
        """
        return self.client.futures_create_order(  # type: ignore[return-value]
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
        )

    def place_stop_order(self, symbol: str, side: str,
                         quantity: float, stop_price: float) -> dict:
        """
        Розміщує STOP_MARKET ордер (для SL).
        Без @retry — помилка -4120 є постійним обмеженням API (не мережева),
        тому повторні спроби лише додають затримку (3×5 сек = 15 сек).
        Fallback — soft monitoring в головному циклі.
        """
        return self.client.futures_create_order(  # type: ignore[return-value]
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            quantity=quantity,
            stopPrice=round(stop_price, 2),
            reduceOnly=True,
        )

    def place_take_profit_order(self, symbol: str, side: str,
                                quantity: float, stop_price: float) -> dict:
        """
        Розміщує TAKE_PROFIT_MARKET ордер.
        Без @retry з тієї ж причини що й place_stop_order.
        """
        return self.client.futures_create_order(  # type: ignore[return-value]
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            quantity=quantity,
            stopPrice=round(stop_price, 2),
            reduceOnly=True,
        )

    @retry()
    def cancel_all_open_orders(self, symbol: str) -> Any:
        """Скасовує всі відкриті ордери для символу."""
        return self.client.futures_cancel_all_open_orders(symbol=symbol)

    @retry()
    def close_position(self, symbol: str, position_amt: float) -> dict:
        """
        Закриває позицію ринковим ордером.
        position_amt — значення з positionAmt (може бути від'ємним для SHORT).
        """
        side = "SELL" if position_amt > 0 else "BUY"
        quantity = abs(position_amt)
        return self.place_market_order(symbol, side, quantity)

    # ─── Фільтри символів (кеш) ───────────────────────────────────────────────

    # Кеш: symbol → {"step_size", "min_qty", "qty_precision", "min_notional"}
    _symbol_filters: dict = {}

    def get_symbol_filters(self, symbol: str) -> dict:
        """
        Повертає LOT_SIZE + MIN_NOTIONAL фільтри для символу.
        Результат кешується — exchange_info завантажується лише один раз.

        Поля результату:
          step_size     — мінімальний крок кількості (LOT_SIZE.stepSize)
          min_qty       — мінімальна кількість (LOT_SIZE.minQty)
          qty_precision — знаків після коми
          min_notional  — мінімальна вартість ордера в USDT (MIN_NOTIONAL.notional)
        """
        if symbol in self._symbol_filters:
            return self._symbol_filters[symbol]

        result = {"step_size": 0.001, "min_qty": 0.001, "qty_precision": 3, "min_notional": 5.0}

        try:
            info = self.get_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] != symbol:
                    continue
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        result["step_size"]     = float(f["stepSize"])
                        result["min_qty"]       = float(f["minQty"])
                        step_str = f["stepSize"]
                        result["qty_precision"] = (
                            len(step_str.rstrip("0").split(".")[-1])
                            if "." in step_str else 0
                        )
                    elif f["filterType"] == "MIN_NOTIONAL":
                        # Binance Futures використовує ключ "notional"
                        result["min_notional"] = float(
                            f.get("notional") or f.get("minNotional") or 5.0
                        )
                self._symbol_filters[symbol] = result
                log.debug(
                    "%s фільтри: step=%s minQty=%s minNotional=%s",
                    symbol, result["step_size"], result["min_qty"], result["min_notional"],
                )
                return result
        except Exception as e:
            log.warning("Не вдалося отримати фільтри для %s: %s", symbol, e)

        # Fallback-значення якщо API недоступний
        fallback = {"step_size": 0.001, "min_qty": 0.001, "qty_precision": 3, "min_notional": 5.0}
        self._symbol_filters[symbol] = fallback
        return fallback

    # ─── Допоміжні ────────────────────────────────────────────────────────────

    @retry()
    def get_open_orders(self, symbol: str) -> list[dict]:
        """Повертає всі відкриті ордери для символу."""
        return self.client.futures_get_open_orders(symbol=symbol)  # type: ignore[return-value]

    def ping(self) -> bool:
        """Перевіряє з'єднання з API. Повертає True якщо успішно."""
        try:
            self.client.futures_ping()
            return True
        except Exception as e:
            log.error("Ping не вдався: %s", e)
            return False


# Глобальний екземпляр — імпортуй у всіх модулях
binance = BinanceClient()
