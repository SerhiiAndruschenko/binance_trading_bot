"""
notifications.py — Автоматичні Telegram-сповіщення від торгового бота.
Усі функції є fire-and-forget (asyncio.create_task або синхронний виклик).
"""

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import config
from logger import log

if TYPE_CHECKING:
    from trader import OpenTrade


# ─── Низькорівневий відправник ────────────────────────────────────────────────

async def _send(text: str) -> None:
    """Відправляє повідомлення в Telegram через Bot API."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("Telegram не налаштовано — пропускаємо сповіщення")
        return
    try:
        from telegram import Bot
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Telegram відправка не вдалася: %s", e)


def _send_sync(text: str) -> None:
    """Синхронна обгортка для виклику з не-async контексту."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Якщо вже є запущений event loop (telegram_bot.py) — плануємо задачу
            asyncio.ensure_future(_send(text))
        else:
            loop.run_until_complete(_send(text))
    except RuntimeError:
        # Немає event loop — створюємо новий
        asyncio.run(_send(text))


# ─── Шаблони повідомлень ──────────────────────────────────────────────────────

def notify_trade_opened(trade: "OpenTrade", filled_price: float) -> None:
    """Сповіщення про відкриту угоду."""
    emoji = "🟢" if trade.signal == "LONG" else "🔴"
    direction = "LONG" if trade.signal == "LONG" else "SHORT"
    p = trade.params

    # Розраховуємо приблизний розмір в базовій валюті
    base_currency = trade.symbol.replace("USDT", "")
    size_usdt = round(p.quantity * filled_price, 2)

    text = (
        f"{emoji} <b>{direction} відкрито</b> | {trade.symbol}\n"
        f"💰 Ціна входу: {filled_price:,.2f} USDT\n"
        f"📊 Розмір: {p.quantity:.4f} {base_currency} (~{size_usdt:.0f} USDT)\n"
        f"🎯 TP: {p.take_profit:,.2f} USDT (+{config.TAKE_PROFIT_PCT*100:.1f}%)\n"
        f"🛑 SL: {p.stop_loss:,.2f} USDT (-{config.STOP_LOSS_PCT*100:.1f}%)\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
    )
    log.debug("Надсилаємо Telegram (відкриття): %s", trade.symbol)
    _send_sync(text)


def notify_trade_closed(trade: "OpenTrade", exit_price: float,
                        pnl_usdt: float, pnl_pct: float) -> None:
    """Сповіщення про закриту угоду."""
    result_emoji = "✅" if pnl_usdt >= 0 else "❌"
    sign = "+" if pnl_usdt >= 0 else ""

    text = (
        f"🔴 <b>Позиція закрита</b> | {trade.symbol}\n"
        f"{result_emoji} Результат: {sign}{pnl_usdt:.4f} USDT ({sign}{pnl_pct:.2f}%)\n"
        f"📈 Вхід: {trade.params.entry_price:,.2f} → Вихід: {exit_price:,.2f}\n"
        f"⏱ Тривалість: {trade.duration_str()}"
    )
    _send_sync(text)


def notify_daily_limit_hit(daily_pnl: float, balance: float) -> None:
    """Сповіщення про спрацювання денного ліміту збитків."""
    text = (
        f"⛔️ <b>Бот зупинений</b>\n"
        f"Причина: досягнуто денний ліміт збитків "
        f"(-{config.DAILY_LOSS_LIMIT*100:.0f}%)\n"
        f"Збиток за сьогодні: {daily_pnl:.4f} USDT\n"
        f"Поточний баланс: {balance:.4f} USDT"
    )
    _send_sync(text)


def notify_bot_started(balance: float, mode: str) -> None:
    """Сповіщення про запуск бота."""
    text = (
        f"🤖 <b>Бот запущено</b>\n"
        f"Режим: {'🔧 TESTNET' if config.TESTNET else '🚀 MAINNET'}\n"
        f"💼 Баланс: {balance:.4f} USDT\n"
        f"📊 Пари: {', '.join(config.SYMBOLS)}\n"
        f"⚡️ Плече: x{config.LEVERAGE} | Ризик: {config.RISK_PER_TRADE*100:.0f}%/угоду"
    )
    _send_sync(text)


def notify_bot_paused(reason: str = "команда /pause") -> None:
    _send_sync(f"⏸ <b>Бот на паузі</b>\nПричина: {reason}")


def notify_bot_resumed() -> None:
    _send_sync("▶️ <b>Бот відновлено</b>")


def notify_error(message: str) -> None:
    _send_sync(f"⚠️ <b>Помилка бота</b>\n{message}")
