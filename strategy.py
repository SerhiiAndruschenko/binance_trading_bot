"""
strategy.py — Торгова стратегія та технічні індикатори.

Стратегія:
  LONG  — EMA21 > EMA50, RSI між 40–65, MACD перетинає вгору
  SHORT — EMA21 < EMA50, RSI між 35–60, MACD перетинає вниз

Бібліотека: ta (pip install ta)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd
import ta
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator

import config
from binance_client import binance
from logger import log


# ─── Типи сигналів ────────────────────────────────────────────────────────────

class Signal(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    NONE  = "NONE"


@dataclass
class SignalResult:
    signal: Signal
    symbol: str
    price: float
    reason: str          # для логування — чому сигнал видано або відхилено
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    rsi: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0


# ─── Отримання та підготовка даних ────────────────────────────────────────────

def fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    """
    Завантажує OHLCV-свічки з Binance та повертає DataFrame.
    Колонки: open, high, low, close, volume
    """
    raw = binance.get_klines(symbol, config.TIMEFRAME, config.CANDLES_LIMIT)
    if not raw:
        log.error("%s: не вдалося отримати свічки", symbol)
        return None

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])

    numeric_cols = ["open", "high", "low", "close", "volume"]
    df[numeric_cols] = df[numeric_cols].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Додає EMA, RSI, MACD до DataFrame через бібліотеку ta."""
    close = df["close"]

    # EMA
    df[f"ema_{config.EMA_FAST}"] = EMAIndicator(
        close=close, window=config.EMA_FAST, fillna=False
    ).ema_indicator()

    df[f"ema_{config.EMA_SLOW}"] = EMAIndicator(
        close=close, window=config.EMA_SLOW, fillna=False
    ).ema_indicator()

    # RSI
    df["rsi"] = RSIIndicator(
        close=close, window=config.RSI_PERIOD, fillna=False
    ).rsi()

    # MACD
    macd_indicator = MACD(
        close=close,
        window_fast=config.MACD_FAST,
        window_slow=config.MACD_SLOW,
        window_sign=config.MACD_SIGNAL,
        fillna=False,
    )
    df["macd"]        = macd_indicator.macd()
    df["macd_signal"] = macd_indicator.macd_signal()

    return df.dropna()


# ─── Логіка сигналу ───────────────────────────────────────────────────────────

def _macd_cross_up(df: pd.DataFrame) -> bool:
    """
    MACD лінія перетнула сигнальну знизу вгору
    між передостанньою (-2) та останньою (-1) свічкою.
    """
    prev_macd   = df["macd"].iloc[-2]
    prev_signal = df["macd_signal"].iloc[-2]
    curr_macd   = df["macd"].iloc[-1]
    curr_signal = df["macd_signal"].iloc[-1]
    return (prev_macd <= prev_signal) and (curr_macd > curr_signal)


def _macd_cross_down(df: pd.DataFrame) -> bool:
    """MACD лінія перетнула сигнальну зверху вниз."""
    prev_macd   = df["macd"].iloc[-2]
    prev_signal = df["macd_signal"].iloc[-2]
    curr_macd   = df["macd"].iloc[-1]
    curr_signal = df["macd_signal"].iloc[-1]
    return (prev_macd >= prev_signal) and (curr_macd < curr_signal)


def analyze(symbol: str) -> SignalResult:
    """
    Аналізує символ і повертає SignalResult з сигналом або NONE.
    """
    df = fetch_ohlcv(symbol)
    if df is None or len(df) < config.EMA_SLOW + 10:
        return SignalResult(Signal.NONE, symbol, 0.0,
                            "Недостатньо даних для аналізу")

    df = add_indicators(df)
    if df.empty or len(df) < 2:
        return SignalResult(Signal.NONE, symbol, 0.0,
                            "DataFrame порожній після додавання індикаторів")

    price      = df["close"].iloc[-1]
    ema_fast   = df[f"ema_{config.EMA_FAST}"].iloc[-1]
    ema_slow   = df[f"ema_{config.EMA_SLOW}"].iloc[-1]
    rsi        = df["rsi"].iloc[-1]
    macd_val   = df["macd"].iloc[-1]
    macd_sig   = df["macd_signal"].iloc[-1]

    base = SignalResult(
        signal=Signal.NONE,
        symbol=symbol,
        price=price,
        reason="",
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=rsi,
        macd=macd_val,
        macd_signal=macd_sig,
    )

    # ── LONG ──────────────────────────────────────────────────────────────────
    if ema_fast > ema_slow:
        if not (config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX):
            base.reason = (
                f"LONG відхилено: EMA OK, але RSI={rsi:.1f} "
                f"поза межами [{config.RSI_LONG_MIN}–{config.RSI_LONG_MAX}]"
            )
            log.debug("[%s] %s", symbol, base.reason)
            return base

        if not _macd_cross_up(df):
            base.reason = (
                f"LONG відхилено: EMA OK, RSI={rsi:.1f} OK, "
                f"але MACD не перетинає вгору"
            )
            log.debug("[%s] %s", symbol, base.reason)
            return base

        base.signal = Signal.LONG
        base.reason = (
            f"LONG: EMA{config.EMA_FAST}={ema_fast:.2f} > EMA{config.EMA_SLOW}={ema_slow:.2f}, "
            f"RSI={rsi:.1f}, MACD перетнув вгору"
        )
        log.info("✅ [%s] %s", symbol, base.reason)
        return base

    # ── SHORT ─────────────────────────────────────────────────────────────────
    if ema_fast < ema_slow:
        if not (config.RSI_SHORT_MIN <= rsi <= config.RSI_SHORT_MAX):
            base.reason = (
                f"SHORT відхилено: EMA OK, але RSI={rsi:.1f} "
                f"поза межами [{config.RSI_SHORT_MIN}–{config.RSI_SHORT_MAX}]"
            )
            log.debug("[%s] %s", symbol, base.reason)
            return base

        if not _macd_cross_down(df):
            base.reason = (
                f"SHORT відхилено: EMA OK, RSI={rsi:.1f} OK, "
                f"але MACD не перетинає вниз"
            )
            log.debug("[%s] %s", symbol, base.reason)
            return base

        base.signal = Signal.SHORT
        base.reason = (
            f"SHORT: EMA{config.EMA_FAST}={ema_fast:.2f} < EMA{config.EMA_SLOW}={ema_slow:.2f}, "
            f"RSI={rsi:.1f}, MACD перетнув вниз"
        )
        log.info("✅ [%s] %s", symbol, base.reason)
        return base

    base.reason = f"Без сигналу: EMA{config.EMA_FAST} ≈ EMA{config.EMA_SLOW}, тренду немає"
    log.debug("[%s] %s", symbol, base.reason)
    return base


# ─── Вибір найактивнішої пари ─────────────────────────────────────────────────

def pick_most_active_symbol(symbols: list[str]) -> str:
    """
    Повертає символ з найбільшим об'ємом торгів за 24г.
    """
    best_symbol = symbols[0]
    best_volume = 0.0

    for sym in symbols:
        try:
            ticker = binance.get_ticker_24h(sym)
            vol = float(ticker.get("quoteVolume", 0))
            log.debug("%s: 24h обсяг = %.0f USDT", sym, vol)
            if vol > best_volume:
                best_volume = vol
                best_symbol = sym
        except Exception as e:
            log.warning("Не вдалося отримати ticker для %s: %s", sym, e)

    log.info("📊 Найактивніша пара: %s (%.0f USDT за 24г)", best_symbol, best_volume)
    return best_symbol
