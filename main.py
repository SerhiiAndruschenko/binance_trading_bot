"""
main.py — Точка входу. Головний цикл торгового бота.

Запуск:
    python main.py

При старті:
  1. Перевірка з'єднання з Binance API
  2. Вивід балансу та відкритих позицій
  3. Запуск Telegram бота (якщо налаштовано)
  4. Циклічне сканування пар кожні SCAN_INTERVAL секунд
"""

import sys
import time
import signal
from datetime import datetime, timezone

import config
from logger import log
from binance_client import binance
from strategy import analyze, pick_most_active_symbol, Signal
from trader import open_position, check_exit_by_signal, close_all_positions
from risk_manager import risk_manager
from notifications import notify_bot_started, notify_daily_limit_hit, notify_error


# ─── Graceful shutdown ────────────────────────────────────────────────────────

_running = True

def _handle_signal(signum, frame):
    global _running
    log.info("Отримано сигнал завершення (%s) — зупиняємо…", signum)
    _running = False

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ─── Стартова перевірка ───────────────────────────────────────────────────────

def startup_check() -> float:
    """
    Перевіряє з'єднання, виводить стан акаунту.
    Повертає поточний баланс або завершує програму при помилці.
    """
    mode = "🔧 TESTNET" if config.TESTNET else "🚀 MAINNET"
    log.info("=" * 60)
    log.info("🤖 Binance Futures Bot стартує…")
    log.info("Режим: %s", mode)
    log.info("Пари: %s", ", ".join(config.SYMBOLS))
    log.info("Таймфрейм: %s | Плече: x%d | Ризик/угода: %.0f%%",
             config.TIMEFRAME, config.LEVERAGE, config.RISK_PER_TRADE * 100)
    log.info("=" * 60)

    # Ping
    if not binance.ping():
        log.error("❌ Немає з'єднання з Binance API. Перевірте ключі та мережу.")
        sys.exit(1)
    log.info("✅ З'єднання з Binance API встановлено")

    # Баланс
    balance = binance.get_futures_balance()
    log.info("💼 Баланс: %.4f USDT", balance)

    # Відкриті позиції
    open_pos = binance.get_open_positions()
    if open_pos:
        log.info("📂 Відкритих позицій: %d", len(open_pos))
        for p in open_pos:
            log.info(
                "  └ %s | Amt: %s | Entry: %s | PnL: %s USDT",
                p.get("symbol"), p.get("positionAmt"),
                p.get("entryPrice"), p.get("unrealizedProfit"),
            )
    else:
        log.info("📂 Відкритих позицій: 0")

    # Тестове отримання свічок
    log.info("📊 Тестуємо отримання свічок для BTCUSDT…")
    klines = binance.get_klines("BTCUSDT", config.TIMEFRAME, limit=5)
    if klines:
        last_close = float(klines[-1][4])
        log.info("✅ BTCUSDT остання ціна закриття: %.2f USDT", last_close)
    else:
        log.warning("⚠️ Не вдалося отримати свічки BTCUSDT")

    log.info("=" * 60)
    return balance


# ─── Один цикл сканування ─────────────────────────────────────────────────────

def scan_cycle(symbols: list[str], iteration: int) -> None:
    """
    Аналізує кожен символ і приймає торгові рішення.
    """
    from telegram_bot import bot_state

    # Перевірка паузи / зупинки
    if bot_state.is_stopped:
        log.debug("Бот зупинено — пропускаємо цикл")
        return

    # Денний ліміт
    balance = binance.get_futures_balance()
    if not risk_manager.check_daily_loss_limit(balance):
        notify_daily_limit_hit(risk_manager.daily_pnl, balance)
        log.warning("⛔️ Денний ліміт — бот зупиняється")
        bot_state.stop()
        return

    # Виводимо зведення по кожній парі кожні 5 ітерацій (≈5 хв)
    # і завжди при першій ітерації
    verbose = (iteration == 1) or (iteration % 5 == 0)

    for symbol in symbols:
        try:
            result = analyze(symbol)

            if verbose:
                # Завжди показуємо поточні індикатори в консолі
                trend = "⬆️ UP" if result.ema_fast > result.ema_slow else "⬇️ DOWN"
                macd_diff = result.macd - result.macd_signal
                log.info(
                    "📊 [%s] Ціна=%.2f | EMA%d/EMA%d %s | RSI=%.1f | "
                    "MACD diff=%.4f | → %s",
                    symbol, result.price,
                    config.EMA_FAST, config.EMA_SLOW, trend,
                    result.rsi, macd_diff,
                    result.signal.value if result.signal.value != "NONE"
                    else f"немає ({result.reason.split(':')[0]})",
                )

            # Якщо є сигнал — перевіряємо на закриття протилежного
            if result.signal != Signal.NONE:
                check_exit_by_signal(symbol, result.signal)

            # Якщо бот не на паузі — намагаємося відкрити позицію
            if not bot_state.is_paused and result.signal != Signal.NONE:
                open_position(result)

        except Exception as e:
            log.error("Помилка при скануванні %s: %s", symbol, e)
            notify_error(f"Помилка при скануванні {symbol}: {e}")


# ─── Головний цикл ────────────────────────────────────────────────────────────

def main() -> None:
    global _running

    # 1. Стартова перевірка
    balance = startup_check()

    # 2. Telegram бот
    try:
        from telegram_bot import run_telegram_bot
        run_telegram_bot()
    except Exception as e:
        log.warning("Telegram бот не запущено: %s", e)

    # 3. Сповіщення про запуск
    try:
        notify_bot_started(balance, "TESTNET" if config.TESTNET else "MAINNET")
    except Exception as e:
        log.warning("Стартове Telegram сповіщення не надіслано: %s", e)

    # 4. Визначаємо найактивнішу пару + фіксуємо всі пари
    active_symbol = pick_most_active_symbol(config.SYMBOLS)
    log.info("🎯 Активна пара: %s | Всі пари: %s",
             active_symbol, ", ".join(config.SYMBOLS))

    # 5. Головний цикл
    log.info("🔄 Запускаємо торговий цикл (інтервал: %d сек)…", config.SCAN_INTERVAL)
    iteration = 0

    while _running:
        iteration += 1
        log.info("─── Скан #%d (%s UTC) ───",
                 iteration, datetime.now(timezone.utc).strftime("%H:%M:%S"))

        scan_cycle(config.SYMBOLS, iteration)

        # Кожні 100 ітерацій оновлюємо найактивнішу пару
        if iteration % 100 == 0:
            active_symbol = pick_most_active_symbol(config.SYMBOLS)

        # Чекаємо до наступного циклу
        for _ in range(config.SCAN_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    # 6. Завершення
    log.info("🛑 Бот зупиняється… Закриваємо позиції…")
    close_all_positions("завершення роботи бота")
    log.info("👋 Бот завершив роботу")


if __name__ == "__main__":
    main()
