"""
risk_manager.py — Управління ризиками: розмір позиції, SL/TP, денний ліміт.
"""

from dataclasses import dataclass
from datetime import date
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
        self._daily_date: date = date.today()
        self._start_balance: Optional[float] = None
        self._is_stopped: bool = False

    # ─── Денний ліміт ─────────────────────────────────────────────────────────

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if today != self._daily_date:
            log.info("📅 Новий день — скидаємо денний P&L")
            self._daily_pnl = 0.0
            self._daily_date = today
            self._start_balance = None
            self._is_stopped = False

    def record_trade_pnl(self, pnl_usdt: float) -> None:
        """Записує результат закритої угоди в денний P&L."""
        self._reset_daily_if_needed()
        self._daily_pnl += pnl_usdt
        log.info("💹 Денний P&L: %.4f USDT", self._daily_pnl)

    def check_daily_loss_limit(self, current_balance: float) -> bool:
        """
        Повертає True якщо денний ліміт збитків НЕ перевищено (можна торгувати).
        Повертає False якщо треба зупинити бота.
        Ліміт рахується від ефективного балансу (з урахуванням MAX_TRADING_BALANCE).
        """
        self._reset_daily_if_needed()

        if self._is_stopped:
            return False

        # Ефективний баланс з урахуванням ліміту
        effective_balance = current_balance
        if config.MAX_TRADING_BALANCE > 0 and current_balance > config.MAX_TRADING_BALANCE:
            effective_balance = config.MAX_TRADING_BALANCE

        # Запам'ятовуємо стартовий баланс першого дня
        if self._start_balance is None:
            self._start_balance = effective_balance
            log.info("📌 Стартовий торговий баланс дня: %.4f USDT", self._start_balance)
            return True

        # Замінюємо current_balance на effective_balance для порівняння
        current_balance = effective_balance

        # Якщо поточний баланс впав на > DAILY_LOSS_LIMIT від стартового
        loss_pct = (self._start_balance - current_balance) / self._start_balance
        if loss_pct >= config.DAILY_LOSS_LIMIT:
            self._is_stopped = True
            log.warning(
                "⛔️ Денний ліміт збитків досягнуто! "
                "Старт: %.2f → Поточний: %.2f (%.2f%%)",
                self._start_balance, current_balance, loss_pct * 100,
            )
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

        # Округлення кількості залежно від символу
        quantity = self._round_quantity(symbol, quantity)
        if quantity <= 0:
            log.error("Розрахована кількість = 0 для %s", symbol)
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
        Округлює кількість до допустимого кроку для символу.
        Для BTC — 3 знаки після коми, ETH — 3, SOL — 1.
        Для невідомих символів — 3 знаки.
        """
        step_map = {
            "BTCUSDT": 3,
            "ETHUSDT": 3,
            "SOLUSDT": 1,
        }
        decimals = step_map.get(symbol, 3)
        return round(quantity, decimals)

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
