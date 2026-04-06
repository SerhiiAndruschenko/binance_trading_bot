"""
config.py — Центральне місце для всіх налаштувань бота.
Змініть TESTNET = False щоб перейти на реальний API.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Режим роботи ────────────────────────────────────────────────────────────
TESTNET: bool = True   # True = Testnet  |  False = Реальний API

# ─── API ─────────────────────────────────────────────────────────────────────
API_KEY: str    = os.getenv("API_KEY", "")
API_SECRET: str = os.getenv("API_SECRET", "")

# ─── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Торгові пари ─────────────────────────────────────────────────────────────
SYMBOLS: list[str] = ["BNBUSDT", "XRPUSDT", "SOLUSDT", "ETHUSDT", "AVAXUSDT"]

# ─── Стратегія ────────────────────────────────────────────────────────────────
TIMEFRAME: str  = "15m"       # таймфрейм свічок
CANDLES_LIMIT: int = 100      # кількість свічок для розрахунку індикаторів

# Параметри EMA
EMA_FAST: int  = 21
EMA_SLOW: int  = 50

# Параметри RSI
RSI_PERIOD: int    = 14
RSI_LONG_MIN: float  = 40.0   # RSI для LONG: нижня межа
RSI_LONG_MAX: float  = 65.0   # RSI для LONG: верхня межа
RSI_SHORT_MIN: float = 35.0   # RSI для SHORT: нижня межа
RSI_SHORT_MAX: float = 60.0   # RSI для SHORT: верхня межа

# Параметри MACD
MACD_FAST: int   = 12
MACD_SLOW: int   = 26
MACD_SIGNAL: int = 9

# ─── Управління ризиками ──────────────────────────────────────────────────────
LEVERAGE: int            = 5      # плече (isolated margin)
RISK_PER_TRADE: float    = 0.02   # 2% від торгового балансу на угоду
TAKE_PROFIT_PCT: float   = 0.015  # +1.5% TP
STOP_LOSS_PCT: float     = 0.008  # -0.8% SL
DAILY_LOSS_LIMIT: float  = 0.05   # зупинка при -5% за день

# Максимальний баланс, який бот використовує для торгівлі (USDT).
# Якщо реальний баланс більший — бот оперує лише цією сумою.
# Встановлюється через .env (MAX_TRADING_BALANCE) або тут.
# 0 = без обмеження (використовується весь доступний баланс).
MAX_TRADING_BALANCE: float = float(os.getenv("MAX_TRADING_BALANCE", "100"))

# ─── Retry логіка ─────────────────────────────────────────────────────────────
API_RETRY_COUNT: int   = 3
API_RETRY_DELAY: float = 5.0   # секунд між спробами

# ─── Інтервал перевірки (секунди) ─────────────────────────────────────────────
SCAN_INTERVAL: int = 60   # кожну хвилину (пара сканується раз на 15-хв свічку)
