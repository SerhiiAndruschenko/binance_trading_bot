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


# ─── Відновлення стану після перезапуску ──────────────────────────────────────

def reconcile_open_trades() -> None:
    """
    Відновлює список відкритих угод з біржі після перезапуску бота.
    Викликається один раз при старті, перед першим торговим циклом.

    - Якщо _open_trades вже не порожній (нормальний старт) — нічого не робить.
    - Для кожної позиції з positionAmt != 0 відновлює OpenTrade з параметрами
      SL/TP розрахованими за поточним конфігом від ціни входу.
    """
    if _open_trades:
        log.debug("reconcile_open_trades: _open_trades вже заповнений — пропускаємо")
        return

    try:
        positions = binance.get_open_positions()
    except Exception as e:
        log.error("reconcile_open_trades: не вдалося отримати позиції: %s", e)
        return

    restored: list[str] = []

    for pos in positions:
        pos_amt = float(pos.get("positionAmt", 0))
        if pos_amt == 0:
            continue

        symbol      = pos["symbol"]
        entry_price = float(pos.get("entryPrice", 0))

        if entry_price <= 0:
            log.warning(
                "reconcile_open_trades: немає ціни входу для %s — пропускаємо",
                symbol,
            )
            continue

        signal_str = "LONG" if pos_amt > 0 else "SHORT"
        quantity   = abs(pos_amt)

        # SL/TP відновлюємо за поточним конфігом від ціни входу
        if signal_str == "LONG":
            side        = "BUY"
            take_profit = round(entry_price * (1 + config.TAKE_PROFIT_PCT), 2)
            stop_loss   = round(entry_price * (1 - config.STOP_LOSS_PCT), 2)
        else:
            side        = "SELL"
            take_profit = round(entry_price * (1 - config.TAKE_PROFIT_PCT), 2)
            stop_loss   = round(entry_price * (1 + config.STOP_LOSS_PCT), 2)

        params = TradeParams(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            usdt_risk=0.0,  # невідомо після перезапуску
        )

        update_time_ms = int(pos.get("updateTime", 0))
        opened_at = (
            datetime.fromtimestamp(update_time_ms / 1000, tz=timezone.utc)
            if update_time_ms > 0
            else datetime.now(timezone.utc)
        )

        trade = OpenTrade(
            symbol=symbol,
            signal=signal_str,
            params=params,
            order_id=0,  # невідомо після перезапуску
            opened_at=opened_at,
        )
        _open_trades[symbol] = trade
        restored.append(f"{symbol} ({signal_str})")

    if restored:
        log.info(
            "♻️ Відновлено %d відкритих позицій після перезапуску: %s",
            len(restored), ", ".join(restored),
        )
    else:
        log.info("♻️ Відкритих позицій на біржі не знайдено — починаємо з нуля")


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

    # 2а. Глобальний ліміт відкритих позицій
    try:
        all_positions = binance.get_open_positions()
        # Binance повертає всі символи (включно з positionAmt=0) — рахуємо тільки реальні
        total_open = len([p for p in all_positions if float(p.get("positionAmt", 0)) != 0])
        if total_open >= config.MAX_OPEN_TRADES_GLOBAL:
            log.info(
                "[%s] Пропуск: досягнуто глобальний ліміт %d позицій (зараз відкрито: %d)",
                symbol, config.MAX_OPEN_TRADES_GLOBAL, total_open,
            )
            return None
    except Exception as e:
        log.warning("Не вдалося перевірити глобальний ліміт позицій: %s", e)

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

    # 7. Намагаємося виставити SL/TP на біржі (одна спроба, без retry).
    #    Помилка -4120 = постійне обмеження Testnet/API — retry марний.
    #    Soft monitoring в циклі гарантує виконання SL/TP у будь-якому разі.
    close_side = "SELL" if signal == Signal.LONG else "BUY"
    sl_on_exchange = False
    tp_on_exchange = False

    try:
        binance.place_stop_order(symbol, close_side, params.quantity, params.stop_loss)
        log.info("[%s] ✅ SL ордер на біржі: %.2f USDT", symbol, params.stop_loss)
        sl_on_exchange = True
    except Exception as e:
        # -4120 = Algo Order endpoint потрібен (Testnet обмеження) — не помилка, просто fallback
        log.debug("[%s] SL не виставлено на біржі (%s) — soft monitoring активний", symbol, e)

    try:
        binance.place_take_profit_order(symbol, close_side, params.quantity, params.take_profit)
        log.info("[%s] ✅ TP ордер на біржі: %.2f USDT", symbol, params.take_profit)
        tp_on_exchange = True
    except Exception as e:
        log.debug("[%s] TP не виставлено на біржі (%s) — soft monitoring активний", symbol, e)

    # Завжди повідомляємо де саме контролюються SL/TP
    if sl_on_exchange and tp_on_exchange:
        log.info("[%s] 🛡 SL/TP виставлено на біржі", symbol)
    else:
        log.info(
            "[%s] 🔁 SL/TP моніторяться ботом: SL=%.2f | TP=%.2f (перевірка щохвилини)",
            symbol, params.stop_loss, params.take_profit,
        )

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

# Комісія Binance Futures taker (0.05%) × 2 сторони (вхід + вихід)
TAKER_FEE = 0.0005


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

        pos_amt     = float(pos["positionAmt"])
        entry_price = float(pos.get("entryPrice", 0))

        # Скасовуємо всі відкриті ордери (SL/TP)
        binance.cancel_all_open_orders(symbol)

        # Закриваємо позицію
        order      = binance.close_position(symbol, pos_amt)
        exit_price = float(order.get("avgPrice") or 0)
        if exit_price == 0:
            try:
                exit_price = binance.get_symbol_price(symbol)
                log.debug(
                    "[%s] avgPrice відсутній — використано поточну ціну %.2f",
                    symbol, exit_price,
                )
            except Exception:
                exit_price = entry_price  # крайній fallback
                log.warning(
                    "[%s] Не вдалось отримати ціну виходу — використано ціну входу",
                    symbol,
                )

        # Розраховуємо P&L (без комісії)
        pnl_usdt = 0.0
        pnl_pct  = 0.0
        if entry_price > 0 and exit_price > 0:
            if pos_amt > 0:  # LONG
                pnl_pct  = (exit_price - entry_price) / entry_price * 100 * config.LEVERAGE
                pnl_usdt = (exit_price - entry_price) * abs(pos_amt)
            else:  # SHORT
                pnl_pct  = (entry_price - exit_price) / entry_price * 100 * config.LEVERAGE
                pnl_usdt = (entry_price - exit_price) * abs(pos_amt)

        # Комісія: taker 0.05% від вартості кожної ноги окремо (вхід + вихід)
        commission = abs(pos_amt) * TAKER_FEE * (entry_price + exit_price)
        pnl_usdt  -= commission

        risk_manager.record_trade_pnl(pnl_usdt)

        log.info(
            "🔴 [%s] Позиція закрита (%s) | Вхід: %.2f → Вихід: %.2f | "
            "P&L: %.4f USDT (%.2f%%) | Комісія: -%.4f USDT",
            symbol, reason, entry_price, exit_price,
            pnl_usdt, pnl_pct, commission,
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
        if float(pos.get("positionAmt", 0)) == 0:
            continue
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


# ─── Soft SL/TP моніторинг ───────────────────────────────────────────────────

def check_sl_tp_all() -> None:
    """
    Перевіряє всі відкриті угоди: чи досягли вони SL або TP за поточною ціною.
    Викликається в кожному циклі сканування.

    Використовується замість exchange-side STOP_MARKET / TAKE_PROFIT_MARKET,
    бо Binance Testnet повертає помилку -4120 для цих типів ордерів.

    P&L відображається з урахуванням плеча (x{LEVERAGE}).
    """
    if not _open_trades:
        return

    for symbol, trade in list(_open_trades.items()):
        try:
            current_price = binance.get_symbol_price(symbol)
            tp = trade.params.take_profit
            sl = trade.params.stop_loss

            if trade.signal == "LONG":
                if current_price >= tp:
                    log.info(
                        "🎯 [%s] TP досягнуто: %.2f >= %.2f",
                        symbol, current_price, tp,
                    )
                    close_position(symbol, f"TP {tp:.2f}")
                elif current_price <= sl:
                    log.info(
                        "🛑 [%s] SL спрацював: %.2f <= %.2f",
                        symbol, current_price, sl,
                    )
                    close_position(symbol, f"SL {sl:.2f}")
                else:
                    # P&L з урахуванням плеча
                    pnl_pct = (
                        (current_price - trade.params.entry_price)
                        / trade.params.entry_price * 100 * config.LEVERAGE
                    )
                    log.info(
                        "📌 [%s] LONG | Ціна=%.2f | TP=%.2f | SL=%.2f | P&L=%.2f%%",
                        symbol, current_price, tp, sl, pnl_pct,
                    )

            else:  # SHORT
                if current_price <= tp:
                    log.info(
                        "🎯 [%s] TP досягнуто: %.2f <= %.2f",
                        symbol, current_price, tp,
                    )
                    close_position(symbol, f"TP {tp:.2f}")
                elif current_price >= sl:
                    log.info(
                        "🛑 [%s] SL спрацював: %.2f >= %.2f",
                        symbol, current_price, sl,
                    )
                    close_position(symbol, f"SL {sl:.2f}")
                else:
                    # P&L з урахуванням плеча
                    pnl_pct = (
                        (trade.params.entry_price - current_price)
                        / trade.params.entry_price * 100 * config.LEVERAGE
                    )
                    log.info(
                        "📌 [%s] SHORT | Ціна=%.2f | TP=%.2f | SL=%.2f | P&L=%.2f%%",
                        symbol, current_price, tp, sl, pnl_pct,
                    )

        except Exception as e:
            log.error("Помилка SL/TP перевірки для %s: %s", symbol, e)
