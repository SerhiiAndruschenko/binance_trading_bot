"""
logger.py — Налаштування централізованого логування.
Логи пишуться і в файл bot.log і виводяться в консоль.
"""

import logging
import sys
from pathlib import Path


def setup_logger(name: str = "binance_bot", log_file: str = "bot.log") -> logging.Logger:
    """
    Створює та повертає налаштований Logger.
    Формат: [2025-04-06 14:32:01] INFO     binance_bot — повідомлення
    """
    logger = logging.getLogger(name)

    # Уникаємо подвійних хендлерів при повторному виклику
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Файловий хендлер ──────────────────────────────────────────────────────
    log_path = Path(log_file)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # ── Консольний хендлер ────────────────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# Глобальний логер — імпортуй його у всіх модулях
log = setup_logger()
