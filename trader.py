"""
trader.py — Відкриття та закриття позицій на Binance Futures.
Координує binance_client, risk_manager та notifications.
"""

import time
from datetime import datetime, timezone
from typing import Optional

import config
from binance_client import binance
from risk_manager import risk_manager, TradeParams
from strategy import Signal, SignalResult
from logger import log


# ─── Внутрішній стан відкритих угод ──────────────────────────────────────────

class OpenTrade:
    """Зберігає метадані про поточну відкриту угоду."""
    def __init__(self, symbol: str, signal: str, params: TradeParams,
                 order_id: int, opened_at: datetime) -> None:
        self.symbol    = symbol
        self.signal    = signal      # 'LONG' або 'SHORT'
        self.params    = params
        self.order_id  = order_id
        self.opened_at = opened_at

    def duration_str(self) -> str:
        delta = datetime.now(timezone.utc) - self.opened_at
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        minutes = rem // 60
        if hours:
            return f"{hours}г {minutes}хв"
        return f"{minutes}хв"


# Словник: symbol → OpenTrade
_open_trades: dict[str, OpenTrade] = {}


# ─── Підготовка пари ──────────────────────────────────────────────────────────

def _prepare_symbol(symbol: str) -> bool:
    """Встановлює плече та isolated margin для символу."""
    try:
        binance.set_margin_type(symbol, "ISOLATED")
        binance.set_leverage(symbol, config.LEVERAGE)
        log.debug("%s: плече x%d ISOLATED встановлено", symbol, config.LEVERAGE)
        return True
    except Exception as e:
        log.error("Не вдалося налаштувати %s: %s", symbol, e)
        return False


# ─── Відкриття позиції ────────────────────────────────────────────────────────

def open_position(result: SignalResult) -> Optional[OpenTrade]:
    """
    Відкриває позицію на основі SignalResult.
    Повертає OpenTrade або None у разі помилки.
    """
    symbol = result.symbol
    signal = result.signal

    # 1. Перевірка на дублювання
    if risk_manager.has_open_position(symbol):
        log.info("[%s] Позиція вже відкрита — пропускаємо", symbol)
        return None

    # 2. Отримуємо баланс
    balance = binance.get_futures_balance()
    log.info("💼 Баланс: %.4f USDT", balance)

    # 3. Денний ліміт
    if not risk_manager.check_daily_loss_limit(balance):
        log.warning("⛔️ Денний ліміт — торгівля заборонена")
        return None

    # 4. Параметри угоди
    params = risk_manager.calculate_trade_params(
        symbol=symbol,
        signal=signal.value,
        entry_price=result.price,
        balance=balance,
    )
    if params is None:
        return None

    # 5. Підготовка символу (плече, тип маржі)
    if not _prepare_symbol(symbol):
        return None

    # 6. Ринковий ордер
    try:
        order = binance.place_market_order(symbol, params.side, params.quantity)
        order_id = order.get("orderId", 0)
        filled_price = float(order.get("avgPrice", result.price) or result.price)
        if filled_price == 0:
            filled_price = result.price

        log.info(
            "📥 [%s] %s відкрито: qty=%.4f @ %.2f USDT | orderId=%s",
            symbol, signal.value, params.quantity, filled_price, order_id,
        )
    except Exception as e:
        log.error("Помилка відкриття ордеру [%s]: %s", symbol, e)
        return None

    # 7. Встановлюємо SL і TP
    close_side = "SELL" if signal == Signal.LONG else "BUY"
    try:
        binance.place_stop_order(symbol, close_side, params.quantity, params.stop_loss)
        log.info("[%s] SL ордер: %.2f USDT", symbol, params.stop_loss)
    except Exception as e:
        log.error("Не вдалося встановити SL для %s: %s", symbol, e)

    try:
        binance.place_take_profit_order(symbol, close_side, params.quantity, params.take_profit)
        log.info("[%s] TP ордер: %.2f USDT", symbol, params.take_profit)
    except Exception as e:
        log.error("Не вдалося встановити TP для %s: %s", symbol, e)

    # 8. Зберігаємо в пам'яті
    trade = OpenTrade(
        symbol=symbol,
        signal=signal.value,
        params=params,
        order_id=order_id,
        opened_at=datetime.now(timezone.utc),
    )
    _open_trades[symbol] = trade

    log.info(
        "✅ [%s] %s | Вхід: %.2f | TP: %.2f | SL: %.2f | qty: %.4f",
        symbol, signal.value, filled_price,
        params.take_profit, params.stop_loss, params.quantity,
    )

    # 9. Telegram сповіщення (імпортуємо тут щоб уникнути циклічних залежностей)
    try:
        from notifications import notify_trade_opened
        notify_trade_opened(trade, filled_price)
    except Exception as e:
        log.warning("Telegram сповіщення не відправлено: %s", e)

    return trade


# ─── Закриття позиції ─────────────────────────────────────────────────────────

def close_position(symbol: str, reason: str = "сигнал") -> bool:
    """
    Закриває позицію для символу ринковим ордером.
    Повертає True якщо успішно.
    """
    try:
        positions = binance.get_open_positions()
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if not pos or float(pos["positionAmt"]) == 0:
            log.info("[%s] Немає відкритої позиції для закриття", symbol)
            _open_trades.pop(symbol, None)
            return False

        pos_amt = float(pos["positionAmt"])
        entry_price = float(pos.get("entryPrice", 0))

        # Скасовуємо всі відкриті ордери (SL/TP)
        binance.cancel_all_open_orders(symbol)

        # Закриваємо позицію
        order = binance.close_position(symbol, pos_amt)
        exit_price = float(order.get("avgPrice", 0) or 0)

        # Розраховуємо P&L
        pnl_usdt = 0.0
        pnl_pct  = 0.0
        if entry_price > 0 and exit_price > 0:
            if pos_amt > 0:  # LONG
                pnl_pct  = (exit_price - entry_price) / entry_price * 100 * config.LEVERAGE
                pnl_usdt = (exit_price - entry_price) * abs(pos_amt)
            else:  # SHORT
                pnl_pct  = (entry_price - exit_price) / entry_price * 100 * config.LEVERAGE
                pnl_usdt = (entry_price - exit_price) * abs(pos_amt)

        risk_manager.record_trade_pnl(pnl_usdt)

        log.info(
            "🔴 [%s] Позиція закрита (%s) | Вхід: %.2f → Вихід: %.2f | "
            "P&L: %.4f USDT (%.2f%%)",
            symbol, reason, entry_price, exit_price, pnl_usdt, pnl_pct,
        )

        # Telegram сповіщення
        trade = _open_trades.get(symbol)
        if trade:
            try:
                from notifications import notify_trade_closed
                notify_trade_closed(trade, exit_price, pnl_usdt, pnl_pct)
            except Exception as e:
                log.warning("Telegram сповіщення (закриття) не відправлено: %s", e)

        _open_trades.pop(symbol, None)
        return True

    except Exception as e:
        log.error("Помилка закриття позиції [%s]: %s", symbol, e)
        return False


def close_all_positions(reason: str = "команда /stop") -> None:
    """Закриває всі відкриті позиції."""
    positions = binance.get_open_positions()
    if not positions:
        log.info("Немає відкритих позицій")
        return
    for pos in positions:
        sym = pos["symbol"]
        log.info("Закриваємо %s (%s)…", sym, reason)
        close_position(sym, reason)


# ─── Перевірка виходу за протилежним сигналом ────────────────────────────────

def check_exit_by_signal(symbol: str, new_signal: Signal) -> bool:
    """
    Якщо для символу відкрита позиція і прийшов протилежний сигнал — закриває.
    Повертає True якщо позицію закрито.
    """
    trade = _open_trades.get(symbol)
    if not trade:
        return False

    should_close = (
        (trade.signal == "LONG"  and new_signal == Signal.SHORT) or
        (trade.signal == "SHORT" and new_signal == Signal.LONG)
    )
    if should_close:
        log.info("[%s] Закриття за протилежним сигналом (%s)", symbol, new_signal.value)
        close_position(symbol, f"протилежний сигнал {new_signal.value}")
        return True
    return False


def get_open_trades() -> dict[str, OpenTrade]:
    """Повертає копію словника відкритих угод."""
    return dict(_open_trades)
