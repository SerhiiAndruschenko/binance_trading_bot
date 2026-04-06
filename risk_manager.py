"""
risk_manager.py — Управління ризиками: розмір позиції, SL/TP, денний ліміт.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import config
from binance_client import binance
from logger import log


@dataclass
class TradeParams:
    """Параметри угоди, розраховані ризик-менеджером."""
    symbol: str
    side: str           # 'BUY' (LONG) або 'SELL' (SHORT)
    quantity: float     # кількість контрактів/монет
    entry_price: float
    take_profit: float
    stop_loss: float
    usdt_risk: float    # скільки USDT ризикуємо


class RiskManager:
    """
    Відповідає за:
    - розрахунок розміру позиції на основі % ризику
    - розрахунок SL і TP
    - контроль денного ліміту збитків
    - захист від дублювання ордерів
    """

    def __init__(self) -> None:
        self._daily_pnl: float = 0.0
        self._daily_date: date = date.today()    # UTC-дата поточного дня
        self._start_balance: Optional[float] = None   # баланс на початок дня
        self._is_stopped: bool = False

    # ─── Денний ліміт ─────────────────────────────────────────────────────────

    def _utc_today(self) -> date:
        """Повертає поточну дату за UTC — скидання відбувається о 00:00 UTC."""
        return datetime.now(timezone.utc).date()

    def _reset_daily_if_needed(self, current_balance: float) -> None:
        """
        Перевіряє чи почався новий UTC-день.
        Якщо так — скидає денний P&L і зберігає поточний баланс
        як стартовий для нового дня.
        """
        today = self._utc_today()
        if today != self._daily_date:
            prev = self._daily_date
            self._daily_pnl    = 0.0
            self._daily_date   = today
            self._is_stopped   = False
            # Стартовий баланс нового дня = фактичний баланс о 00:00 UTC
            effective = current_balance
            if config.MAX_TRADING_BALANCE > 0 and current_balance > config.MAX_TRADING_BALANCE:
                effective = config.MAX_TRADING_BALANCE
            self._start_balance = effective
            log.info(
                "📅 Новий день (%s → %s) | Стартовий баланс: %.2f USDT",
                prev, today, self._start_balance,
            )

    def record_trade_pnl(self, pnl_usdt: float) -> None:
        """Записує результат закритої угоди в денний P&L."""
        self._daily_pnl += pnl_usdt
        log.info("💹 Денний P&L: %+.4f USDT", self._daily_pnl)

    def check_daily_loss_limit(self, current_balance: float) -> bool:
        """
        Повертає True якщо денний ліміт збитків НЕ перевищено.
        Повертає False якщо бот має зупинитись.

        Ліміт = DAILY_LOSS_LIMIT % від балансу на початок поточного UTC-дня.
        При переході на новий день стартовий баланс оновлюється автоматично.
        """
        # Перевірка і скидання при зміні дня (передаємо баланс для запису нового старту)
        self._reset_daily_if_needed(current_balance)

        if self._is_stopped:
            return False

        # Ефективний баланс з урахуванням MAX_TRADING_BALANCE
        effective_balance = current_balance
        if config.MAX_TRADING_BALANCE > 0 and current_balance > config.MAX_TRADING_BALANCE:
            effective_balance = config.MAX_TRADING_BALANCE

        # Перший запуск бота за поточний день — запам'ятовуємо стартовий баланс
        if self._start_balance is None:
            self._start_balance = effective_balance
            log.info("📌 Стартовий баланс дня: %.2f USDT", self._start_balance)
            return True

        # Збиток від початку дня у USDT і %
        loss_usdt = self._start_balance - effective_balance
        loss_pct  = loss_usdt / self._start_balance if self._start_balance > 0 else 0.0

        if loss_pct >= config.DAILY_LOSS_LIMIT:
            self._is_stopped = True
            log.warning(
                "⛔️ Денний ліміт збитків (-%.0f%%) досягнуто!\n"
                "   Баланс на початок дня: %.2f USDT\n"
                "   Поточний баланс:       %.2f USDT\n"
                "   Збиток:                -%.2f USDT",
                config.DAILY_LOSS_LIMIT * 100,
                self._start_balance, effective_balance, loss_usdt,
            )
            # Telegram сповіщення
            try:
                from notifications import notify_daily_limit_hit
                notify_daily_limit_hit(self._daily_pnl, effective_balance)
            except Exception:
                pass
            return False

        return True

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def is_stopped(self) -> bool:
        return self._is_stopped

    # ─── Розрахунок параметрів угоди ──────────────────────────────────────────

    def calculate_trade_params(
        self,
        symbol: str,
        signal: str,          # 'LONG' або 'SHORT'
        entry_price: float,
        balance: float,
    ) -> Optional[TradeParams]:
        """
        Розраховує:
        - кількість контрактів з урахуванням плеча та % ризику
        - рівні TP і SL

        Формула: qty = (balance * RISK_PCT * leverage) / entry_price
        SL = entry * (1 ∓ SL_PCT)
        TP = entry * (1 ± TP_PCT)
        """
        if balance <= 0:
            log.error("Баланс нульовий або від'ємний: %.4f", balance)
            return None

        # Обмеження торгового балансу (MAX_TRADING_BALANCE)
        effective_balance = balance
        if config.MAX_TRADING_BALANCE > 0 and balance > config.MAX_TRADING_BALANCE:
            effective_balance = config.MAX_TRADING_BALANCE
            log.info(
                "🔒 Баланс обмежено: реальний=%.2f USDT → торговий=%.2f USDT",
                balance, effective_balance,
            )

        # Максимальна сума ризику в USDT
        usdt_risk = effective_balance * config.RISK_PER_TRADE

        # Розмір позиції з урахуванням плеча
        position_usdt = usdt_risk * config.LEVERAGE
        quantity = position_usdt / entry_price

        # Округлення кількості відповідно до stepSize з біржі
        quantity = self._round_quantity(symbol, quantity)

        # Перевірка мінімального розміру ордера
        filters      = binance.get_symbol_filters(symbol)
        min_qty      = filters["min_qty"]
        min_notional = filters["min_notional"]
        notional     = quantity * entry_price   # вартість позиції в USDT

        if quantity <= 0 or quantity < min_qty:
            need_balance = (min_qty * entry_price / config.LEVERAGE) / config.RISK_PER_TRADE
            log.warning(
                "⚠️  [%s] Угода пропущена: qty=%.6f < мінімум %.6f. "
                "Потрібно ~%.0f USDT торгового балансу.",
                symbol, quantity, min_qty, need_balance,
            )
            return None

        if notional < min_notional:
            need_balance = (min_notional / config.LEVERAGE) / config.RISK_PER_TRADE
            log.warning(
                "⚠️  [%s] Угода пропущена: notional=%.2f USDT < мінімум %.0f USDT (-4164). "
                "Потрібно ~%.0f USDT торгового балансу.",
                symbol, notional, min_notional, need_balance,
            )
            return None

        if signal == "LONG":
            side        = "BUY"
            take_profit = entry_price * (1 + config.TAKE_PROFIT_PCT)
            stop_loss   = entry_price * (1 - config.STOP_LOSS_PCT)
        else:  # SHORT
            side        = "SELL"
            take_profit = entry_price * (1 - config.TAKE_PROFIT_PCT)
            stop_loss   = entry_price * (1 + config.STOP_LOSS_PCT)

        params = TradeParams(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            take_profit=round(take_profit, 2),
            stop_loss=round(stop_loss, 2),
            usdt_risk=round(usdt_risk, 4),
        )

        log.info(
            "📐 [%s] %s qty=%.4f | TP=%.2f | SL=%.2f | Ризик=%.2f USDT",
            symbol, signal, quantity,
            params.take_profit, params.stop_loss, usdt_risk,
        )
        return params

    def _round_quantity(self, symbol: str, quantity: float) -> float:
        """
        Округлює кількість до stepSize отриманого з біржі.
        Використовує floor (вниз) щоб не перевищити баланс.
        """
        import math
        filters = binance.get_symbol_filters(symbol)
        step = filters["step_size"]
        prec = filters["qty_precision"]
        # Floor до кроку: int(qty / step) * step
        floored = math.floor(quantity / step) * step
        return round(floored, prec)

    # ─── Перевірка відкритої позиції ──────────────────────────────────────────

    def has_open_position(self, symbol: str) -> bool:
        """
        Повертає True якщо для символу вже є відкрита позиція.
        Захист від дублювання ордерів.
        """
        try:
            positions = binance.get_open_positions()
            for pos in positions:
                if pos["symbol"] == symbol and float(pos["positionAmt"]) != 0:
                    log.debug("%s: вже є відкрита позиція (%s)", symbol, pos["positionAmt"])
                    return True
            return False
        except Exception as e:
            log.error("Помилка перевірки позицій: %s", e)
            return True  # У разі помилки — безпечніше вважати що позиція є


# Глобальний екземпляр
risk_manager = RiskManager()
