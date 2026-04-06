"""
telegram_bot.py — Telegram-бот з командами для управління торговим ботом.
Запускається в окремому потоці поряд із головним циклом.

Команди:
  /status  — поточний стан бота та позицій
  /today   — статистика за сьогодні
  /month   — статистика за поточний місяць
  /pause   — зупинити відкриття нових угод
  /resume  — відновити роботу
  /stop    — закрити всі позиції і зупинити бота
"""

import asyncio
import threading
from datetime import date, datetime, timezone
from collections import defaultdict
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import config
from logger import log


# ─── Спільний стан між ботом та головним циклом ───────────────────────────────

class BotState:
    """Thread-safe стан бота."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.is_paused: bool = False
        self.is_stopped: bool = False
        # Записи угод для статистики: list of dict
        self._trades: list[dict] = []

    def pause(self) -> None:
        with self._lock:
            self.is_paused = True
            log.info("⏸ Бот поставлено на паузу")

    def resume(self) -> None:
        with self._lock:
            self.is_paused = False
            log.info("▶️ Бот відновлено")

    def stop(self) -> None:
        with self._lock:
            self.is_stopped = True
            log.info("⛔️ Бот зупинено командою /stop")

    def record_trade(self, symbol: str, signal: str, pnl_usdt: float,
                     opened_at: datetime, closed_at: datetime) -> None:
        with self._lock:
            self._trades.append({
                "symbol": symbol,
                "signal": signal,
                "pnl": pnl_usdt,
                "opened_at": opened_at,
                "closed_at": closed_at,
            })

    def trades_today(self) -> list[dict]:
        today = date.today()
        with self._lock:
            return [t for t in self._trades
                    if t["closed_at"].date() == today]

    def trades_this_month(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        with self._lock:
            return [t for t in self._trades
                    if (t["closed_at"].year == now.year and
                        t["closed_at"].month == now.month)]


# Глобальний стан
bot_state = BotState()


# ─── Авторизація ──────────────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    """
    Перевіряє що команда від авторизованого користувача.

    В приватному чаті: effective_chat.id == особистий ID користувача.
    В груповому чаті:  effective_chat.id == ID групи (від'ємне число),
                       effective_user.id  == особистий ID користувача.
    Тому перевіряємо обидва — бот працює і в приватних, і в групових чатах.
    """
    if not config.TELEGRAM_CHAT_ID:
        return True  # якщо не налаштовано — пропускаємо перевірку

    allowed = str(config.TELEGRAM_CHAT_ID)
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""

    return allowed in (chat_id, user_id)


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    try:
        from binance_client import binance
        from trader import get_open_trades
        from risk_manager import risk_manager

        balance = binance.get_futures_balance()
        open_trades = get_open_trades()

        status_emoji = "⏸" if bot_state.is_paused else ("⛔️" if bot_state.is_stopped else "🤖")
        status_text  = "на паузі" if bot_state.is_paused else ("зупинений" if bot_state.is_stopped else "активний")

        lines = [
            f"{status_emoji} <b>Бот {status_text}</b>",
            f"💼 Баланс: {balance:.4f} USDT",
            f"📂 Відкриті позиції: {len(open_trades)}",
        ]

        if open_trades:
            # Отримуємо поточні ціни для P&L
            positions = binance.get_open_positions()
            pos_map = {p["symbol"]: p for p in positions}

            for sym, trade in open_trades.items():
                pos = pos_map.get(sym, {})
                unrealized = float(pos.get("unrealizedProfit", 0))
                entry = trade.params.entry_price
                sign = "+" if unrealized >= 0 else ""
                lines.append(
                    f"  └ {trade.signal} {sym} | {sign}{unrealized:.4f} USDT"
                )

        lines.append(f"📅 Денний P&L: {risk_manager.daily_pnl:+.4f} USDT")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        log.error("Помилка /status: %s", e)
        await update.message.reply_text(f"⚠️ Помилка отримання статусу: {e}")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    trades = bot_state.trades_today()
    today_str = datetime.now(timezone.utc).strftime("%d %B")

    if not trades:
        await update.message.reply_text(
            f"📅 Сьогодні, {today_str}\nУгод ще не було"
        )
        return

    total   = len(trades)
    winners = sum(1 for t in trades if t["pnl"] >= 0)
    losers  = total - winners
    pnl     = sum(t["pnl"] for t in trades)
    sign    = "+" if pnl >= 0 else ""

    text = (
        f"📅 <b>Сьогодні, {today_str}</b>\n"
        f"✅ Угод закрито: {total}\n"
        f"💚 Прибуткових: {winners} | 💔 Збиткових: {losers}\n"
        f"💰 P&L: {sign}{pnl:.4f} USDT"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    trades = bot_state.trades_this_month()
    month_str = datetime.now(timezone.utc).strftime("%B %Y")

    if not trades:
        await update.message.reply_text(
            f"📆 {month_str}\nУгод ще не було"
        )
        return

    total   = len(trades)
    winners = sum(1 for t in trades if t["pnl"] >= 0)
    win_pct = round(winners / total * 100) if total else 0
    pnl     = sum(t["pnl"] for t in trades)
    sign    = "+" if pnl >= 0 else ""

    # Максимальна просадка (накопичений збиток)
    running_pnl = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for t in sorted(trades, key=lambda x: x["closed_at"]):
        running_pnl += t["pnl"]
        if running_pnl > peak:
            peak = running_pnl
        drawdown = peak - running_pnl
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    text = (
        f"📆 <b>{month_str}</b>\n"
        f"✅ Угод всього: {total}\n"
        f"💚 Прибуткових: {winners} ({win_pct}%)\n"
        f"💰 P&L: {sign}{pnl:.4f} USDT\n"
        f"📉 Макс. просадка: -{max_drawdown:.4f} USDT"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    bot_state.pause()
    from notifications import notify_bot_paused
    notify_bot_paused("команда /pause")
    await update.message.reply_text(
        "⏸ Бот на паузі.\nНові угоди не відкриватимуться.\n"
        "Поточні позиції залишаються активними.\n"
        "Використайте /resume для відновлення."
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if bot_state.is_stopped:
        await update.message.reply_text(
            "⛔️ Бот зупинений командою /stop. Перезапустіть процес."
        )
        return
    bot_state.resume()
    from notifications import notify_bot_resumed
    notify_bot_resumed()
    await update.message.reply_text("▶️ Бот відновлено. Нові угоди відкриватимуться.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    text = (
        "🤖 <b>Binance Futures Bot — команди</b>\n\n"
        "<b>📊 Інформація</b>\n"
        "/status — стан бота, баланс, відкриті позиції\n"
        "/today  — статистика угод за сьогодні\n"
        "/month  — статистика за поточний місяць\n\n"
        "<b>⚙️ Управління</b>\n"
        "/pause  — зупинити нові угоди (поточні залишаються)\n"
        "/resume — відновити роботу після паузи\n"
        "/stop   — закрити всі позиції і зупинити бота\n\n"
        "<b>ℹ️ Поточні налаштування</b>\n"
        f"Пари: {', '.join(config.SYMBOLS)}\n"
        f"Таймфрейм: {config.TIMEFRAME} | Плече: x{config.LEVERAGE}\n"
        f"Ризик/угода: {config.RISK_PER_TRADE*100:.0f}% | "
        f"TP: +{config.TAKE_PROFIT_PCT*100:.1f}% | "
        f"SL: -{config.STOP_LOSS_PCT*100:.1f}%\n"
        f"Торговий баланс: {config.MAX_TRADING_BALANCE:.0f} USDT | "
        f"Денний ліміт: -{config.DAILY_LOSS_LIMIT*100:.0f}%"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    await update.message.reply_text(
        "⛔️ Зупиняю бота...\nЗакриваю всі відкриті позиції..."
    )

    try:
        from trader import close_all_positions
        close_all_positions("команда /stop")
        bot_state.stop()
        await update.message.reply_text(
            "✅ Всі позиції закриті. Бот зупинено.\n"
            "Перезапустіть скрипт для продовження роботи."
        )
    except Exception as e:
        log.error("Помилка при /stop: %s", e)
        await update.message.reply_text(f"⚠️ Помилка при зупинці: {e}")


# ─── Запуск бота ──────────────────────────────────────────────────────────────

def run_telegram_bot() -> None:
    """
    Запускає Telegram-бота в окремому потоці.
    python-telegram-bot v20+ керує власним event loop через run_polling(),
    тому використовуємо asyncio.run() — він створює чистий loop у потоці.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN не встановлено — Telegram бот не запущено")
        return

    log.info("🤖 Запуск Telegram бота…")

    def _thread_target() -> None:
        async def _run() -> None:
            app = (
                Application.builder()
                .token(config.TELEGRAM_BOT_TOKEN)
                .build()
            )

            app.add_handler(CommandHandler("help",   cmd_help))
            app.add_handler(CommandHandler("start",  cmd_help))  # аліас для /start
            app.add_handler(CommandHandler("status", cmd_status))
            app.add_handler(CommandHandler("today",  cmd_today))
            app.add_handler(CommandHandler("month",  cmd_month))
            app.add_handler(CommandHandler("pause",  cmd_pause))
            app.add_handler(CommandHandler("resume", cmd_resume))
            app.add_handler(CommandHandler("stop",   cmd_stop))

            log.info("✅ Telegram бот підключено — слухаємо команди")
            # initialize/start/polling/stop — правильний lifecycle для v20+
            async with app:
                await app.start()
                # allowed_updates потрібен щоб отримувати команди з групових чатів.
                # Без нього Telegram може не доставляти повідомлення з груп.
                await app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["message", "callback_query"],
                )
                # Тримаємо потік живим поки бот не зупинено
                while not bot_state.is_stopped:
                    await asyncio.sleep(1)
                await app.updater.stop()
                await app.stop()

        try:
            asyncio.run(_run())
        except Exception as e:
            log.error("Telegram бот завершився з помилкою: %s", e)

    thread = threading.Thread(target=_thread_target, daemon=True, name="TelegramBot")
    thread.start()
    log.info("🧵 Telegram бот запущено у фоновому потоці")
