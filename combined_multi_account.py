#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram: архиватор медиа + рассылка основного аккаунта с единой панелью управления.

После первоначальной настройки и запуска сервиса основное управление выполняется
в Telegram-боте: добавление аккаунтов, авторизация по коду/2FA, запуск и остановка,
пауза архиватора, настройка группы назначения и просмотр логов.

  python combined.py setup        первичная настройка основного аккаунта и бота
  python combined.py run          рабочий режим (обычно запускается через systemd)

Дополнительные профили используют тот же API ID/API Hash, но отдельные Telegram-сессии,
SQLite-базы, настройки и папки данных. Они автоматически поднимаются вместе с сервисом.
Каждый дополнительный профиль также может использовать независимую рассылку:
собственный пост из своего «Избранного», отдельные настройки, историю и список
исключений. Все группы включаются по умолчанию; Telegram проверяет право публикации при отправке.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import ipaddress
import inspect
import json
import logging
import math
import os
import random
import re
import socket
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tempfile
import zipfile
import platform
from contextvars import ContextVar
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

# --- UTF-8 везде, даже если на VPS локаль C/POSIX ---------------------------
for _s in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

try:
    from telethon import Button, TelegramClient, errors, events, functions, types, utils
    from telethon.tl.types import (InputMessagesFilterRoundVideo,
                                   InputMessagesFilterPhotos, InputMessagesFilterVideo,
                                   InputMessagesFilterDocument)
except ImportError:
    sys.exit("Не установлен Telethon. Установите: pip install -U 'telethon>=1.45.0,<2'")

try:
    import broadcast_core as bcore   # умный чёрный список со сроком перепроверки (файл лежит рядом)
except ImportError:
    sys.exit("Нет broadcast_core.py рядом со скриптом.")

if not hasattr(bcore, "DurableActionQueue") or not all(
    callable(getattr(bcore.DurableActionQueue, method, None))
    for method in ("batch_action_count", "start_batch_pause")
):
    sys.exit("broadcast_core.py устарел или несовместим. Обнови его вместе с combined_multi_account.py.")

try:
    from dotenv import load_dotenv
    load_dotenv()  # API_ID / API_HASH из .env, как в старой рассылке
except ImportError:
    pass

try:
    import aiohttp  # нужен только для раскрытия ссылок-редиректов
except ImportError:
    aiohttp = None

try:
    import fcntl  # только Linux/macOS
except ImportError:  # pragma: no cover
    fcntl = None

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("CIRCLES_CONFIG", str(BASE / "config.json")))
DEFAULT_CONFIG_PATH = CONFIG_PATH
ACCOUNTS_DIR = BASE / "accounts"
SERVICE = "circles-archiver"
APP_VERSION = "3.9.6"
AUTOSUB_OWNER_PAUSE_REASON = "приостановлена владельцем"


# =============================================================================
#  Единый стиль Telegram-панели
# =============================================================================
_BUTTON_STYLE_WARNED = False
_UI_CUSTOM_EMOJI = {}


def _ui_button(text, data, style=None, icon=None):
    """Create a consistently styled inline button, with optional Telegram custom emoji."""
    global _BUTTON_STYLE_WARNED
    payload = data.decode("utf-8", "ignore") if isinstance(data, bytes) else str(data or "")
    label = str(text or "").casefold()
    if style is None:
        destructive = any(x in payload for x in (
            ":stop", "stopall", "clean:go", "restart:go", "delete", "remove", "logout", "acc:stop"
        )) or any(x in label for x in ("остановить", "удалить", "сбросить", "отключить", "пауза"))
        positive = any(x in payload for x in (
            ":go", "abgo:", "start", "startall", "abpin", "abp:", "abadm"
        )) or any(x in label for x in (
            "запустить", "продолжить", "включить", "сохранить", "подтвердить", "выбрать", "добавить"
        ))
        if destructive:
            style = "danger"
        elif positive:
            style = "success"
        else:
            style = "primary"

    if icon is None:
        # Custom emoji IDs are optional and must be supplied by the owner.
        # Standard Unicode emoji in labels remain the default when no ID is configured.
        key = "default"
        if any(x in payload for x in ("stop", "delete", "remove", "logout")) or any(
                x in label for x in ("остановить", "удалить", "сбросить", "пауза")):
            key = "stop"
        elif any(x in payload for x in ("start", "abgo", "abadm")) or any(
                x in label for x in ("запустить", "продолжить", "включить", "добавить")):
            key = "start"
        elif "acc:" in payload or "accounts" in label or "аккаунт" in label:
            key = "accounts"
        elif "broadcast" in payload or "ab" in payload or "рассыл" in label:
            key = "broadcast"
        elif "diag" in payload or "диагност" in label:
            key = "diagnostics"
        elif "set" in payload or "настрой" in label:
            key = "settings"
        elif "log" in payload or "истор" in label or "журнал" in label:
            key = "history"
        elif "main" in payload or "меню" in label or "назад" in label:
            key = "back"
        configured_icon = _UI_CUSTOM_EMOJI.get(key) or _UI_CUSTOM_EMOJI.get("default")
        if configured_icon:
            try:
                candidate_icon_id = int(configured_icon)
                if candidate_icon_id > 0:
                    icon = candidate_icon_id
            except (TypeError, ValueError):
                # Ignore placeholders and malformed IDs instead of creating
                # a button that Telegram would reject at serialization time.
                icon = None

    kwargs = {"style": style} if style in {"primary", "success", "danger"} else {}
    if icon:
        kwargs["icon"] = icon
    if kwargs:
        try:
            return Button.inline(text, data, **kwargs)
        except TypeError:
            if not _BUTTON_STYLE_WARNED:
                log.warning("Установленный Telethon не поддерживает цветные/custom emoji кнопки. Обновите до >=1.45.0.")
                _BUTTON_STYLE_WARNED = True
    return Button.inline(text, data)

log = logging.getLogger("circles")
blog = logging.getLogger("broadcast")
ACTIVE_PROFILE = ContextVar("circles_active_profile", default="default")

# =============================================================================
#  Конфиг
# =============================================================================
DEFAULTS = {
    "api_id": 0,
    "api_hash": "",
    "target_chat_id": 0,
    "save_outgoing": True,   # сохранять и мои исходящие медиа
    "info_message": True,    # подпись с датой под медиафайлом
    "keep_local": True,      # хранить копии на диске VPS
    # Backward-compatible: existing installs continue archiving circles only until owner selects more types.
    "archive_media_types": ["round_video"],  # round_video, photo, video
    "data_dir": "data",
    "session_name": "circles",
    "ignore_users": [],      # id людей, чьи медиа не сохраняем
    "bot_token": "",         # токен бота-панели от @BotFather (необязательно)
    "paused": False,         # пауза архивации (переключается кнопкой в боте)
    "broadcast_interval_min": 60,   # рассылка: пауза между кругами, минут
    "broadcast_hours": 12,          # рассылка: сколько часов работать после старта
    "broadcast_autostart": False,   # запускать рассылку вместе с сервисом
    "broadcast_auto_subscribe": True,  # фоновая обработка ссылок от ботов независимо от состояния рассылки
    "broadcast_auto_subscribe_user_set": False,  # сохранить явный выбор владельца
    "broadcast_auto_subscribe_gap_seconds": 90,  # пауза между успешными действиями автоподписки
    "broadcast_auto_subscribe_daily_limit": 15,   # консервативный rolling limit для вступлений за 24 часа
    "broadcast_auto_subscribe_max_per_message": 24,  # максимум ссылок из одного сообщения в очередь
    "broadcast_auto_subscribe_batch_size": 5,  # успешных действий до плановой паузы
    "broadcast_auto_subscribe_batch_pause_minutes": 30,  # плановый отдых между пакетами
    "broadcast_spambot": False,     # писать @SpamBot при спам-блоке
    "broadcast_exclude": [],        # id групп, куда рассылка НЕ идёт (переключается в боте)
    "broadcast_allowlist_only": False,
    "broadcast_allowlist": [],      # legacy compatibility; not used in all-groups mode
    "broadcast_notify_waves": True, # присылать отчёт после каждой волны
    "owner_id": 0,                  # id владельца (запоминается сам при запуске)
    "tz_offset": 3,                 # часовой пояс для времени в боте (UTC+N)
    "low_disk_percent": 10,         # предупреждать, если свободно меньше N% диска
    "broadcast_delay_min": 0.6,     # рассылка: пауза после каждого чата, сек (от)
    "broadcast_delay_max": 1.2,     # рассылка: пауза после каждого чата, сек (до)
    "broadcast_long_pause_chance": 0.02,  # шанс длинной паузы после чата (0.02 = 2%)
    "ui_custom_emoji": {},  # необязательные Telegram custom emoji ID по ключам интерфейса
    "web_manager_enabled": True,  # локальная Web UI Smart Cleaner; только через текущие клиенты профилей
}


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            sys.exit(f"Файл {CONFIG_PATH} повреждён: {e}")
    for key, env, conv in (("api_id", "API_ID", int), ("api_hash", "API_HASH", str),
                           ("target_chat_id", "TARGET_CHAT_ID", int)):
        if not cfg[key] and os.getenv(env):
            cfg[key] = conv(os.environ[env])
    # The path travels with the in-memory config so concurrent profiles never overwrite one another.
    cfg["_config_path"] = str(CONFIG_PATH)
    return cfg


def save_cfg(cfg: dict) -> None:
    path = Path(cfg.get("_config_path") or CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    serializable = {k: v for k, v in cfg.items() if k != "_config_path"}
    tmp.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # внутри api_hash
    except OSError:
        pass
    os.replace(tmp, path)  # атомарно - файл профиля не побьётся при сбое


def data_dir(cfg: dict) -> Path:
    p = Path(cfg["data_dir"])
    p = p if p.is_absolute() else BASE / p
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)  # session databases and media are private to this OS user
    except OSError:
        pass
    return p


def profile_config_path(name: str) -> Path:
    """Return a safe per-account config path. Profile names are directory names, not paths."""
    if name == "default":
        raise ValueError("Имя «default» зарезервировано для основного аккаунта")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", name or ""):
        raise ValueError("Имя профиля: 1–40 символов, только латиница, цифры, . _ -; первый символ — буква/цифра")
    return ACCOUNTS_DIR / name / "config.json"


def account_profiles() -> list[tuple[str, Path]]:
    """Return all configured accounts (legacy default + named profiles)."""
    result: list[tuple[str, Path]] = []
    if DEFAULT_CONFIG_PATH.exists():
        result.append(("default", DEFAULT_CONFIG_PATH))
    if ACCOUNTS_DIR.exists():
        for folder in sorted(ACCOUNTS_DIR.iterdir()):
            candidate = folder / "config.json"
            if folder.is_dir() and candidate.is_file():
                result.append((folder.name, candidate))
    return result


def read_profile_config(path: Path) -> dict:
    global CONFIG_PATH
    old = CONFIG_PATH
    try:
        CONFIG_PATH = path
        return load_cfg()
    finally:
        CONFIG_PATH = old


def _web_manager_profiles():
    result = []
    for name, path in account_profiles():
        try:
            cfg = read_profile_config(path)
            result.append((name, cfg, data_dir(cfg)))
        except Exception as exc:  # noqa: BLE001
            log.warning("Web Manager: не удалось прочитать профиль %s: %s", name, type(exc).__name__)
    return result


try:
    from telegram_manager_web import TelegramManagerRuntime
    WEB_MANAGER_RUNTIME = TelegramManagerRuntime(base_dir=BASE, profiles_provider=_web_manager_profiles)
except ImportError as _web_import_error:
    WEB_MANAGER_RUNTIME = None
    logging.getLogger("circles").warning("Web Manager не загружен: %s. Установите зависимости из requirements-smart-cleaner.txt", _web_import_error)


def add_account_profile(name: str) -> Path:
    """Clone safe defaults into an isolated account profile; never clone auth/session data."""
    target = profile_config_path(name)
    if target.exists():
        raise FileExistsError(f"Профиль '{name}' уже существует")
    base_cfg = read_profile_config(DEFAULT_CONFIG_PATH)
    cfg = dict(base_cfg)
    cfg.update({
        "owner_id": 0,
        "account_label": name,
        "session_name": "circles",
        "data_dir": str(Path("accounts") / name / "data"),
        "bot_token": "",             # each profile needs its own bot token, if a panel is desired
        "ignore_users": [],
        "archive_media_types": ["round_video"],
        "broadcast_exclude": [],
        # Every profile starts with its own independent broadcast defaults.
        # Never inherit the primary account's selected recipients or timing.
        "broadcast_allowlist_only": False,
        "broadcast_allowlist": [],
        "broadcast_auto_subscribe": True,
        "broadcast_auto_subscribe_user_set": False,
        "broadcast_auto_subscribe_gap_seconds": 90,
        "broadcast_auto_subscribe_daily_limit": 15,
        "broadcast_auto_subscribe_max_per_message": 24,
        "broadcast_auto_subscribe_batch_size": 5,
        "broadcast_auto_subscribe_batch_pause_minutes": 30,
        "broadcast_interval_min": 60,
        "broadcast_hours": 12,
        "broadcast_notify_waves": True,
        "broadcast_delay_min": 0.6,
        "broadcast_delay_max": 1.2,
        "broadcast_long_pause_chance": 0.02,
        "paused": False,
        "broadcast_autostart": False,
        "_config_path": str(target),
    })
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    global CONFIG_PATH
    old = CONFIG_PATH
    try:
        CONFIG_PATH = target
        save_cfg(cfg)
    finally:
        CONFIG_PATH = old
    return target


def print_accounts() -> None:
    profiles = account_profiles()
    if not profiles:
        print("Профилей пока нет. Сначала настройте основной аккаунт или выполните add-account NAME.")
        return
    print("\nАккаунты Telegram:")
    for name, path in profiles:
        cfg = read_profile_config(path)
        ddir = data_dir(cfg)
        sess = ddir / (cfg.get("session_name", "circles") + ".session")
        state = "файл сессии есть" if sess.exists() else "не авторизован"
        phone_label = cfg.get("account_label") or name
        print(f"  • {phone_label}: {state} | config: {path.relative_to(BASE)}")


def cfg_problems(cfg: dict) -> list[str]:
    bad = []
    if not cfg["api_id"]:
        bad.append("не задан API ID")
    if not cfg["api_hash"]:
        bad.append("не задан API HASH")
    if not re.fullmatch(r"-100\d+", str(cfg.get("target_chat_id") or "")):
        bad.append("ID группы-форума должен быть в формате -100...")
    return bad


def setup_logging(ddir: Path, console: bool = True) -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(ddir / "circles.log", maxBytes=5 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    # рассылка пишет в свой файл, чтобы не забивать лог архива
    blog.setLevel(logging.INFO)
    bfh = RotatingFileHandler(ddir / "broadcast.log", maxBytes=5 * 1024 * 1024,
                              backupCount=3, encoding="utf-8")
    bfh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    blog.addHandler(bfh)
    if console:
        bsh = logging.StreamHandler()
        bsh.setFormatter(logging.Formatter("%(asctime)s [рассылка] %(message)s"))
        blog.addHandler(bsh)
    ring = ErrorRing()
    for lg in (log, blog, logging.getLogger("telethon")):
        lg.addHandler(ring)
    logging.getLogger("telethon").setLevel(logging.WARNING)


# =============================================================================
#  Общие утилиты: время, уведомления владельцу, кольцо последних ошибок
# =============================================================================
RECENT_ERRORS: deque = deque(maxlen=60)


class ErrorRing(logging.Handler):
    """Копит последние предупреждения/ошибки, чтобы показать их в боте."""

    def emit(self, record):
        if record.levelno < logging.WARNING:
            return
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1]:
                msg += f" [{type(record.exc_info[1]).__name__}: {record.exc_info[1]}]"
            RECENT_ERRORS.append((time.time(), record.levelname, record.name, msg[:300]))
        except Exception:  # noqa: BLE001
            pass


class ProfileLogFilter(logging.Filter):
    """Route each account's records to its own log files in run --all-accounts mode."""

    def __init__(self, profile_name: str):
        super().__init__()
        self.profile_name = profile_name

    def filter(self, record):
        return ACTIVE_PROFILE.get() == self.profile_name


def setup_multi_logging(items: list[tuple[str, dict, Path]]) -> None:
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    blog.setLevel(logging.INFO)
    for name, _cfg, ddir in items:
        for logger, filename, formatter in (
            (log, "circles.log", fmt),
            (blog, "broadcast.log", logging.Formatter("%(asctime)s %(message)s")),
        ):
            handler = RotatingFileHandler(ddir / filename, maxBytes=5 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(formatter)
            handler.addFilter(ProfileLogFilter(name))
            logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    broadcast_console = logging.StreamHandler()
    broadcast_console.setFormatter(logging.Formatter("%(asctime)s [рассылка] %(message)s"))
    blog.addHandler(broadcast_console)
    ring = ErrorRing()
    for logger in (log, blog, logging.getLogger("telethon")):
        logger.addHandler(ring)
    logging.getLogger("telethon").setLevel(logging.WARNING)


def fmt_ts(cfg: dict, ts, fmt: str = "%d.%m %H:%M") -> str:
    """Время в часовом поясе владельца. ts - секунды, datetime или ISO-строка."""
    if not ts:
        return "-"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ts[:16]
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.timestamp()
    tz = timezone(timedelta(hours=cfg.get("tz_offset", 3)))
    return datetime.fromtimestamp(ts, tz).strftime(fmt)


def fmt_dur(sec) -> str:
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} с"
    return f"{s} с"


class Notifier:
    """Отправка сообщений владельцу. alert() не повторяет одно и то же чаще, чем раз в cooldown."""

    def __init__(self):
        self.send_fn = None   # async def (text, buttons)
        self._last: dict = {}

    async def send(self, text: str, buttons=None) -> bool:
        if not self.send_fn:
            return False
        try:
            await self.send_fn(text, buttons)
            return True
        except Exception as e:  # noqa: BLE001
            log.info("Не смог написать владельцу (открой бота и нажми /start): %s", e)
            return False

    async def alert(self, key: str, text: str, cooldown: float = 1800) -> bool:
        now = time.monotonic()
        if cooldown and now - self._last.get(key, -1e12) < cooldown:
            return False
        self._last[key] = now
        return await self.send(text, [[_ui_button("📋 Меню", b"main")]])


# =============================================================================
#  База данных
# =============================================================================
def _locked(fn):
    """Все обращения к sqlite идут под замком: DB можно дёргать и из потока (to_thread), и из цикла."""
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class DB:
    def __init__(self, path: Path, readonly: bool = False):
        self._lock = threading.RLock()
        self.path = Path(path)
        self.readonly = bool(readonly)
        if self.readonly:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    user_id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL,
                    title TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS circles (
                    user_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
                    date_utc TEXT, duration INTEGER, outgoing INTEGER,
                    file_path TEXT, sent INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
                    topic_id INTEGER, media_type TEXT DEFAULT 'round_video',
                    mime_type TEXT DEFAULT '', file_size INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, msg_id));
                CREATE INDEX IF NOT EXISTS idx_circles_user_date
                    ON circles(user_id, date_utc);
                CREATE INDEX IF NOT EXISTS idx_circles_sent_attempts
                    ON circles(sent, attempts);
                CREATE TABLE IF NOT EXISTS topic_cleanup_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    title TEXT, circle_count INTEGER NOT NULL DEFAULT 0,
                    last_circle TEXT, criterion TEXT NOT NULL,
                    result TEXT NOT NULL, error TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS idx_topic_cleanup_created
                    ON topic_cleanup_log(created_at DESC);
                CREATE TABLE IF NOT EXISTS backfill_state (
                    user_id INTEGER NOT NULL, media_type TEXT NOT NULL,
                    last_msg_id INTEGER NOT NULL DEFAULT 0,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, media_type));
                """
            )
            # Migrate legacy installations once. Old rows can only be associated
            # with the currently mapped topic; future rows store topic_id explicitly.
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(circles)").fetchall()}
            if "topic_id" not in columns:
                self.conn.execute("ALTER TABLE circles ADD COLUMN topic_id INTEGER")
                self.conn.execute(
                    """UPDATE circles
                       SET topic_id=(SELECT topic_id FROM topics WHERE topics.user_id=circles.user_id)
                       WHERE topic_id IS NULL""")
            if "media_type" not in columns:
                self.conn.execute("ALTER TABLE circles ADD COLUMN media_type TEXT NOT NULL DEFAULT 'round_video'")
            if "mime_type" not in columns:
                self.conn.execute("ALTER TABLE circles ADD COLUMN mime_type TEXT NOT NULL DEFAULT ''")
            if "file_size" not in columns:
                self.conn.execute("ALTER TABLE circles ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0")
            # Upgrade old per-type history cursors to independent server-side filters.
            # The old photo/video cursors are valid for their native Telegram media filters.
            # Additional document passes start at zero to discover images/videos sent as files.
            for user_id, last_msg_id, scanned, archived, completed in self.conn.execute(
                    "SELECT user_id, last_msg_id, scanned, archived, completed FROM backfill_state WHERE media_type='photo'").fetchall():
                self.conn.execute(
                    "INSERT OR IGNORE INTO backfill_state(user_id,media_type,last_msg_id,scanned,archived,completed) VALUES(?,?,?,?,?,?)",
                    (user_id, "photo:photos", last_msg_id, scanned, archived, completed))
                self.conn.execute(
                    "INSERT OR IGNORE INTO backfill_state(user_id,media_type,last_msg_id,scanned,archived,completed) VALUES(?,?,?,?,?,?)",
                    (user_id, "photo:documents", 0, 0, 0, 0))
            self.conn.execute("DELETE FROM backfill_state WHERE media_type='photo'")
            for user_id, last_msg_id, scanned, archived, completed in self.conn.execute(
                    "SELECT user_id, last_msg_id, scanned, archived, completed FROM backfill_state WHERE media_type='video'").fetchall():
                self.conn.execute(
                    "INSERT OR IGNORE INTO backfill_state(user_id,media_type,last_msg_id,scanned,archived,completed) VALUES(?,?,?,?,?,?)",
                    (user_id, "video:videos", last_msg_id, scanned, archived, completed))
                self.conn.execute(
                    "INSERT OR IGNORE INTO backfill_state(user_id,media_type,last_msg_id,scanned,archived,completed) VALUES(?,?,?,?,?,?)",
                    (user_id, "video:documents", 0, 0, 0, 0))
            self.conn.execute("DELETE FROM backfill_state WHERE media_type='video'")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_circles_topic_date ON circles(topic_id, date_utc)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_circles_user_date_msg "
                "ON circles(user_id, date_utc DESC, msg_id DESC)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_circles_media_type ON circles(media_type, sent, date_utc)")
            self.conn.commit()

        self._table_names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        circle_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(circles)").fetchall()}
        self._has_topic_id = "topic_id" in circle_columns
        self._has_media_type = "media_type" in circle_columns
        self._has_media_metadata = {"mime_type", "file_size"}.issubset(circle_columns)
        self._has_cleanup_log = "topic_cleanup_log" in self._table_names

    @_locked
    def get_topic(self, user_id):
        r = self.conn.execute("SELECT topic_id FROM topics WHERE user_id=?", (user_id,)).fetchone()
        return r[0] if r else None

    @_locked
    def set_topic(self, user_id, topic_id, title):
        self.conn.execute("INSERT OR REPLACE INTO topics(user_id, topic_id, title) VALUES (?,?,?)",
                          (user_id, topic_id, title))
        self.conn.commit()

    @_locked
    def drop_topic(self, user_id):
        self.conn.execute("DELETE FROM topics WHERE user_id=?", (user_id,))
        self.conn.commit()

    @_locked
    def is_done(self, user_id, msg_id) -> bool:
        r = self.conn.execute("SELECT sent FROM circles WHERE user_id=? AND msg_id=?",
                              (user_id, msg_id)).fetchone()
        return bool(r and r[0])

    @_locked
    def save_circle(self, user_id, msg_id, date, duration, outgoing, path, sent, topic_id=None,
                    media_type="round_video", mime_type="", file_size=0):
        r = self.conn.execute("SELECT attempts, topic_id FROM circles WHERE user_id=? AND msg_id=?",
                              (user_id, msg_id)).fetchone()
        attempts = (r[0] if r else 0) + (0 if sent else 1)
        # Retain the previously associated topic if a retry has not created a new one yet.
        if topic_id is None and r:
            topic_id = r[1]
        self.conn.execute(
            """INSERT OR REPLACE INTO circles
               (user_id, msg_id, date_utc, duration, outgoing, file_path, sent, attempts, topic_id,
                media_type, mime_type, file_size)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, msg_id, date, duration, int(outgoing), str(path), int(sent), attempts,
             int(topic_id) if topic_id is not None else None, str(media_type or "round_video"),
             str(mime_type or ""), max(0, int(file_size or 0))))
        self.conn.commit()

    @_locked
    def unsent(self, max_attempts=5):
        return self.conn.execute(
            "SELECT user_id, msg_id FROM circles WHERE sent=0 AND attempts<? ORDER BY date_utc",
            (max_attempts,)).fetchall()

    @_locked
    def sent_with_files(self):
        return self.conn.execute(
            "SELECT user_id, msg_id, file_path FROM circles WHERE sent=1 AND file_path<>''").fetchall()

    @_locked
    def clear_paths(self, pairs):
        self.conn.executemany("UPDATE circles SET file_path='' WHERE user_id=? AND msg_id=?", pairs)
        self.conn.commit()

    @_locked
    def quick_check(self) -> str:
        """Run SQLite's lightweight integrity check without modifying user data."""
        row = self.conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "no result"

    @_locked
    def stats(self) -> dict:
        t = self.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        total, sent, last = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(sent=1),0), MAX(date_utc) FROM circles").fetchone()
        by_type = {str(kind): int(count) for kind, count in self.conn.execute(
            "SELECT COALESCE(media_type, 'round_video'), COUNT(*) FROM circles GROUP BY media_type").fetchall()}
        return {"topics": t, "total": total, "sent": sent, "unsent": total - sent,
                "last": last, "by_type": by_type}

    @_locked
    def media_type_counts(self):
        if not self._has_media_type:
            total = self.conn.execute("SELECT COUNT(*) FROM circles").fetchone()[0]
            return {"round_video": int(total or 0)}
        return {str(kind): int(count) for kind, count in self.conn.execute(
            "SELECT COALESCE(media_type, 'round_video'), COUNT(*) FROM circles GROUP BY media_type").fetchall()}

    @_locked
    def media_type_status(self):
        """Counts by media type, split into archived and pending records."""
        if not self._has_media_type:
            row = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(sent=1),0), COALESCE(SUM(sent=0),0) FROM circles"
            ).fetchone()
            total, sent, pending = (int(x or 0) for x in row)
            return {"round_video": {"total": total, "sent": sent, "pending": pending}}
        rows = self.conn.execute(
            """SELECT COALESCE(media_type, 'round_video'), COUNT(*),
                      COALESCE(SUM(sent=1),0), COALESCE(SUM(sent=0),0)
               FROM circles GROUP BY media_type"""
        ).fetchall()
        return {str(kind): {"total": int(total or 0), "sent": int(sent or 0),
                            "pending": int(pending or 0)}
                for kind, total, sent, pending in rows}

    @_locked
    def save_backfill_state(self, user_id, media_type, last_msg_id, scanned, archived, completed=False):
        self.conn.execute(
            """INSERT INTO backfill_state
               (user_id, media_type, last_msg_id, scanned, archived, completed, updated_at)
               VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(user_id, media_type) DO UPDATE SET
                 last_msg_id=excluded.last_msg_id,
                 scanned=backfill_state.scanned + excluded.scanned,
                 archived=backfill_state.archived + excluded.archived,
                 completed=excluded.completed,
                 updated_at=CURRENT_TIMESTAMP""",
            (int(user_id), str(media_type), int(last_msg_id or 0), int(scanned or 0),
             int(archived or 0), int(bool(completed))))
        self.conn.commit()

    @_locked
    def backfill_cursor(self, user_id, media_type):
        row = self.conn.execute(
            "SELECT last_msg_id, completed, scanned, archived FROM backfill_state WHERE user_id=? AND media_type=?",
            (int(user_id), str(media_type))).fetchone()
        return row if row else (0, 0, 0, 0)

    @_locked
    def reset_backfill_state(self, media_types):
        """Reset all filter cursors for selected types; existing archived messages remain untouched."""
        kinds = [str(x) for x in media_types if str(x) in ARCHIVE_MEDIA_LABELS]
        if not kinds:
            return 0
        clauses = []
        params = []
        for kind in kinds:
            clauses.append("(media_type=? OR media_type LIKE ?)")
            params.extend((kind, kind + ":%"))
        cur = self.conn.execute(
            "UPDATE backfill_state SET last_msg_id=0, scanned=0, archived=0, completed=0, "
            "updated_at=CURRENT_TIMESTAMP WHERE " + " OR ".join(clauses), params)
        self.conn.commit()
        return int(cur.rowcount or 0)

    @_locked
    def backfill_summary(self):
        rows = self.conn.execute(
            """SELECT CASE
                       WHEN media_type='photo' OR media_type LIKE 'photo:%' THEN 'photo'
                       WHEN media_type='video' OR media_type LIKE 'video:%' THEN 'video'
                       ELSE media_type END AS display_type,
                      SUM(scanned), SUM(archived), SUM(completed=1), COUNT(*)
               FROM backfill_state GROUP BY display_type"""
        ).fetchall()
        return {str(kind): {"scanned": int(scanned or 0), "archived": int(archived or 0),
                            "completed_chats": int(done or 0), "chats": int(chats or 0)}
                for kind, scanned, archived, done, chats in rows}

    @_locked
    def people(self):
        return self.conn.execute(
            """SELECT t.user_id, t.title, t.topic_id, COUNT(c.msg_id), MAX(c.date_utc)
               FROM topics t LEFT JOIN circles c ON c.topic_id=t.topic_id
               GROUP BY t.user_id ORDER BY MAX(c.date_utc) DESC""").fetchall()

    @_locked
    def catalog_rows(self):
        """Aggregated archive catalogue. Legacy read-only DBs fall back to user_id joins."""
        join = "c.topic_id=t.topic_id" if self._has_topic_id else "c.user_id=t.user_id"
        return self.conn.execute(
            f"""SELECT t.user_id, t.title, t.topic_id,
                      COUNT(c.msg_id) AS circle_count,
                      COALESCE(SUM(CASE WHEN c.sent=1 THEN 1 ELSE 0 END), 0) AS sent_count,
                      COALESCE(SUM(CASE WHEN c.sent=0 THEN 1 ELSE 0 END), 0) AS pending_count,
                      MAX(c.date_utc) AS last_circle,
                      MIN(c.date_utc) AS first_circle,
                      t.created_at,
                      COALESCE(SUM(c.duration), 0) AS total_duration
               FROM topics t LEFT JOIN circles c ON {join}
               GROUP BY t.user_id, t.title, t.topic_id, t.created_at"""
        ).fetchall()

    @_locked
    def catalog_detail(self, user_id):
        join = "c.topic_id=t.topic_id" if self._has_topic_id else "c.user_id=t.user_id"
        return self.conn.execute(
            f"""SELECT t.user_id, t.title, t.topic_id,
                      COUNT(c.msg_id) AS circle_count,
                      COALESCE(SUM(CASE WHEN c.sent=1 THEN 1 ELSE 0 END), 0) AS sent_count,
                      COALESCE(SUM(CASE WHEN c.sent=0 THEN 1 ELSE 0 END), 0) AS pending_count,
                      MAX(c.date_utc) AS last_circle,
                      MIN(c.date_utc) AS first_circle,
                      t.created_at,
                      COALESCE(SUM(c.duration), 0) AS total_duration
               FROM topics t LEFT JOIN circles c ON {join}
               WHERE t.user_id=?
               GROUP BY t.user_id, t.title, t.topic_id, t.created_at""",
            (int(user_id),)).fetchone()

    @_locked
    def topic_circles(self, user_id, limit=12):
        """Return newest media records for a sender/topic (legacy method name retained)."""
        join = "c.topic_id=t.topic_id" if self._has_topic_id else "c.user_id=t.user_id"
        media_type = "c.media_type" if self._has_media_type else "'round_video'"
        mime_type = "c.mime_type" if self._has_media_metadata else "''"
        file_size = "c.file_size" if self._has_media_metadata else "0"
        return self.conn.execute(
            f"""SELECT c.msg_id, c.date_utc, c.duration, c.outgoing,
                      c.sent, c.attempts, c.file_path, {media_type}, {mime_type}, {file_size}
               FROM topics t JOIN circles c ON {join}
               WHERE t.user_id=?
               ORDER BY c.date_utc DESC, c.msg_id DESC LIMIT ?""",
            (int(user_id), max(1, min(50, int(limit))))).fetchall()

    @_locked
    def record_topic_cleanup(self, user_id, topic_id, title, circle_count,
                             last_circle, criterion, result, error=None):
        self.conn.execute(
            """INSERT INTO topic_cleanup_log
               (user_id, topic_id, title, circle_count, last_circle, criterion, result, error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (int(user_id), int(topic_id), str(title or '')[:160], int(circle_count or 0),
             last_circle, str(criterion), str(result), str(error)[:500] if error else None))
        self.conn.commit()

    @_locked
    def topic_cleanup_history(self, limit=10):
        if not self._has_cleanup_log:
            return []
        return self.conn.execute(
            """SELECT user_id, topic_id, title, circle_count, criterion, result, error, created_at
               FROM topic_cleanup_log ORDER BY id DESC LIMIT ?""", (int(limit),)).fetchall()

    @_locked
    def close(self):
        self.conn.close()


# =============================================================================
#  Защита от двойного запуска (две копии на одной сессии ломают её)
# =============================================================================
def acquire_lock(ddir: Path):
    if fcntl is None:
        return True
    f = open(ddir / "run.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def make_client(cfg: dict, ddir: Path) -> TelegramClient:
    return TelegramClient(str(ddir / cfg["session_name"]), int(cfg["api_id"]), cfg["api_hash"],
                          connection_retries=-1, retry_delay=5, auto_reconnect=True)


# =============================================================================
#  Основная логика
# =============================================================================
_CTRL = re.compile(r"[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e]")


def display_name(u) -> str:
    name = " ".join(x for x in (u.first_name, u.last_name) if x).strip()
    name = _CTRL.sub("", name).strip()
    if not name:
        name = f"Удалённый аккаунт {u.id}" if getattr(u, "deleted", False) else f"User {u.id}"
    if getattr(u, "username", None):
        name += f" (@{u.username})"
    return name[:128]


ARCHIVE_MEDIA_LABELS = {
    "round_video": "Кружки",
    "photo": "Фотографии",
    "video": "Обычные видео",
}
ARCHIVE_MEDIA_FILTERS = {
    "round_video": InputMessagesFilterRoundVideo,
    "photo": InputMessagesFilterPhotos,
    "video": InputMessagesFilterVideo,
}
# A separate document pass is required for media sent with "Send as file".
# Each media type owns its own cursor so enabling a type later never skips older files.
ARCHIVE_MEDIA_SCAN_FILTERS = {
    "round_video": (("round_video", InputMessagesFilterRoundVideo),),
    "photo": (("photo:photos", InputMessagesFilterPhotos),
              ("photo:documents", InputMessagesFilterDocument)),
    # Native videos use InputMessagesFilterVideo; generic uploads such as .mp4/.mov
    # sent as files need the additional document pass.
    "video": (("video:videos", InputMessagesFilterVideo),
              ("video:documents", InputMessagesFilterDocument)),
}


def configured_media_types(cfg: dict) -> set[str]:
    """Return validated per-profile media selection; old configs default to circles only."""
    raw = cfg.get("archive_media_types", ["round_video"])
    if not isinstance(raw, (list, tuple, set)):
        raw = ["round_video"]
    return {str(item) for item in raw if str(item) in ARCHIVE_MEDIA_LABELS}


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mpeg", ".mpg", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".bmp", ".tif", ".tiff"}


def classify_archive_media(message) -> str | None:
    """Classify circles, photos and ordinary videos; exclude GIFs, stickers and other files."""
    if message is None or getattr(message, "media", None) is None:
        return None
    if getattr(message, "video_note", None):
        return "round_video"
    if getattr(message, "photo", None):
        return "photo"

    document = getattr(message, "document", None)
    if document is None:
        return None
    mime = str(getattr(document, "mime_type", "") or "").lower()
    attrs = list(getattr(document, "attributes", None) or [])
    if any(isinstance(attr, getattr(types, "DocumentAttributeAnimated", ())) for attr in attrs):
        return None
    # Stickers/custom emoji are not ordinary photos, even if their MIME is image/webp.
    sticker_classes = tuple(cls for cls in (
        getattr(types, "DocumentAttributeSticker", None),
        getattr(types, "DocumentAttributeCustomEmoji", None),
    ) if cls is not None)
    if sticker_classes and any(isinstance(attr, sticker_classes) for attr in attrs):
        return None

    video_attr_cls = getattr(types, "DocumentAttributeVideo", None)
    video_attr = next((a for a in attrs if video_attr_cls and isinstance(a, video_attr_cls)), None)
    if video_attr is not None and bool(getattr(video_attr, "round_message", False)):
        return "round_video"

    file_obj = getattr(message, "file", None)
    ext = str(getattr(file_obj, "ext", "") or "").lower()
    if not ext:
        filename_cls = getattr(types, "DocumentAttributeFilename", None)
        filename_attr = next((a for a in attrs if filename_cls and isinstance(a, filename_cls)), None)
        if filename_attr:
            ext = Path(str(getattr(filename_attr, "file_name", "") or "")).suffix.lower()

    # A document can be a video/photo sent without Telegram's usual media attribute.
    if mime.startswith("video/") or video_attr is not None or ext in VIDEO_EXTENSIONS:
        return "video"
    if (mime.startswith("image/") and mime not in {"image/gif", "image/svg+xml"}) or ext in IMAGE_EXTENSIONS:
        return "photo"
    return None


def media_extension(message, media_type: str) -> str:
    """Choose a safe extension so photos and videos remain recognizable on disk."""
    file_obj = getattr(message, "file", None)
    ext = str(getattr(file_obj, "ext", "") or "").lower()
    if not ext:
        document = getattr(message, "document", None)
        attrs = list(getattr(document, "attributes", None) or [])
        filename_cls = getattr(types, "DocumentAttributeFilename", None)
        filename_attr = next((a for a in attrs if filename_cls and isinstance(a, filename_cls)), None)
        if filename_attr:
            ext = Path(str(getattr(filename_attr, "file_name", "") or "")).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = {"round_video": ".mp4", "photo": ".jpg", "video": ".mp4"}.get(media_type, ".bin")
    return ext


def media_metadata(message, media_type: str) -> dict:
    file_obj = getattr(message, "file", None)
    document = getattr(message, "document", None)
    mime = str(getattr(file_obj, "mime_type", None) or getattr(document, "mime_type", None) or "")
    attrs = list(getattr(document, "attributes", None) or [])
    video_attr_cls = getattr(types, "DocumentAttributeVideo", None)
    video_attr = next((a for a in attrs if video_attr_cls and isinstance(a, video_attr_cls)), None)
    return {
        "media_type": media_type,
        "mime_type": mime,
        "file_size": int(getattr(file_obj, "size", 0) or getattr(document, "size", 0) or 0),
        "duration": int(getattr(video_attr, "duration", 0) or 0),
        "width": int(getattr(video_attr, "w", 0) or getattr(file_obj, "width", 0) or 0),
        "height": int(getattr(video_attr, "h", 0) or getattr(file_obj, "height", 0) or 0),
        "as_document": bool(media_type == "photo" and document is not None and not getattr(message, "photo", None)),
    }


def is_topic_error(e: Exception) -> bool:
    s = str(e).upper()
    return any(k in s for k in ("TOPIC", "MESSAGE_ID_INVALID", "REPLY_TO_INVALID"))


class Archiver:
    PAUSE = 1        # пауза между архивируемыми файлами
    FLOOD_PAD = 1

    def __init__(self, cfg, ddir, db, client):
        self.cfg, self.ddir, self.db, self.client = cfg, ddir, db, client
        self.videos = ddir / "videos"
        self.videos.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()   # медиа обрабатываем строго по очереди
        self.target = None
        self.notifier = None     # Notifier, ставится из run_service
        self.stop_flag = False   # остановка выгрузки из бота
        self.backfill_scanned = 0
        self.backfill_archived = 0
        self.backfill_current = ""

    def selected_media_types(self) -> set[str]:
        return configured_media_types(self.cfg)

    def media_type(self, message) -> str | None:
        kind = classify_archive_media(message)
        return kind if kind in self.selected_media_types() else None

    async def setup_target(self):
        """Возвращает текст ошибки или None, если всё хорошо."""
        tid = int(self.cfg["target_chat_id"])
        try:
            target = await self.client.get_entity(tid)      # быстро: из кеша сессии
        except Exception:  # noqa: BLE001
            try:
                await self.client.get_dialogs()             # долго, только если в кеше нет
                target = await self.client.get_entity(tid)
            except Exception as e:  # noqa: BLE001
                return f"Не удалось найти группу {tid}: {e}"
        if not getattr(target, "forum", False):
            return "В целевой группе не включены темы (Topics). Включите их в настройках группы."
        self.target = target   # присваиваем только когда всё в порядке
        return None

    async def _alert(self, key, text, cooldown=3600):
        if self.notifier:
            await self.notifier.alert(key, text, cooldown)

    async def create_topic(self, user) -> int:
        title = display_name(user)
        res = await self.client(functions.messages.CreateForumTopicRequest(
            peer=self.target, title=title, random_id=random.randint(1, 2 ** 62)))
        for upd in res.updates:
            msg = getattr(upd, "message", None)
            if isinstance(msg, types.MessageService):
                self.db.set_topic(user.id, msg.id, title)
                log.info("Создана тема '%s' (id=%s)", title, msg.id)
                return msg.id
        raise RuntimeError("Не удалось получить id созданной темы")

    async def ensure_topic(self, user) -> int:
        tid = self.db.get_topic(user.id)
        return tid if tid is not None else await self.create_topic(user)

    async def delete_archive_topic(self, topic_id: int):
        """Delete an archive forum topic. Requires manage_topics rights on the archive forum."""
        if self.target is None:
            raise RuntimeError("Целевая группа архива не настроена")
        topic_id = int(topic_id)
        if topic_id <= 1:
            raise ValueError("Системная тема General защищена от удаления")
        input_peer = await self.client.get_input_entity(self.target)

        # Current Telegram forum API uses messages.deleteTopicHistory(InputPeer, top_msg_id).
        messages_ns = getattr(functions, "messages", None)
        request_cls = getattr(messages_ns, "DeleteTopicHistoryRequest", None)
        if request_cls is not None:
            return await self.client(request_cls(peer=input_peer, top_msg_id=topic_id))

        # Compatibility with API layers exposing channels.deleteTopicHistory(InputChannel, ...).
        channels_ns = getattr(functions, "channels", None)
        request_cls = getattr(channels_ns, "DeleteTopicHistoryRequest", None)
        if request_cls is not None:
            input_channel = utils.get_input_channel(input_peer)
            return await self.client(request_cls(channel=input_channel, top_msg_id=topic_id))

        raise RuntimeError(
            "В установленном Telethon нет DeleteTopicHistoryRequest; "
            "обнови Telethon до актуальной версии"
        )

    async def send_media(self, topic_id, msg, path, media_type, metadata):
        """Send original media reference first; upload the validated local copy as fallback."""
        kwargs = {"reply_to": topic_id}
        if media_type == "video":
            kwargs.update(supports_streaming=True, force_document=False)
        elif media_type == "photo":
            kwargs.update(force_document=bool(metadata.get("as_document", False)))
        try:
            return await self.client.send_file(self.target, msg.media, **kwargs)
        except errors.FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            if is_topic_error(e):
                raise
            log.warning("Отправка по file_id не удалась (%s), заливаю файл", e)
        if not Path(path).exists():
            raise RuntimeError("локальный файл отсутствует")
        if media_type == "round_video":
            a = next((x for x in (getattr(getattr(msg, "document", None), "attributes", None) or [])
                      if isinstance(x, types.DocumentAttributeVideo)), None)
            return await self.client.send_file(
                self.target, str(path), reply_to=topic_id, video_note=True,
                attributes=[types.DocumentAttributeVideo(
                    duration=a.duration if a else metadata["duration"],
                    w=a.w if a else 384, h=a.h if a else 384, round_message=True)])
        if media_type == "video":
            return await self.client.send_file(
                self.target, str(path), reply_to=topic_id, supports_streaming=True,
                force_document=False, attributes=[types.DocumentAttributeVideo(
                    duration=metadata["duration"], w=metadata["width"] or 1280,
                    h=metadata["height"] or 720, round_message=False)])
        return await self.client.send_file(
            self.target, str(path), reply_to=topic_id,
            force_document=bool(metadata.get("as_document", False)))

    async def process(self, msg, user, force=False):
        if self.cfg.get("paused") and not force:
            return
        # удалённые аккаунты НЕ пропускаем: ранее полученные медиа тоже архивируем
        if getattr(user, "bot", False) or getattr(user, "is_self", False):
            return
        if user.id in self.cfg["ignore_users"]:
            return
        media_type = self.media_type(msg)
        if media_type is None:
            return
        if msg.out and not self.cfg["save_outgoing"]:
            return
        if self.db.is_done(user.id, msg.id):
            return
        if self.target is None:   # группа не настроена: ничего не теряем, «Выгрузить старые» подберёт позже
            return

        async with self.lock:
            if self.db.is_done(user.id, msg.id):
                return
            if self.media_type(msg) != media_type:
                return
            dt = msg.date.astimezone(timezone.utc)
            folder = self.videos / str(user.id)
            folder.mkdir(parents=True, exist_ok=True)
            metadata = media_metadata(msg, media_type)
            extension = media_extension(msg, media_type)
            path = folder / f"{media_type}_{dt:%Y%m%d_%H%M%S}_{msg.id}{extension}"
            duration = metadata["duration"]

            # 1. скачивание
            try:
                expected_size = getattr(getattr(msg, "file", None), "size", None)
                if path.exists() and (path.stat().st_size <= 0 or
                                      (expected_size and path.stat().st_size < int(expected_size))):
                    path.unlink(missing_ok=True)
                if not path.exists():
                    await self.client.download_media(msg, file=str(path))
                actual_size = path.stat().st_size if path.exists() else 0
                if actual_size <= 0 or (expected_size and actual_size < int(expected_size)):
                    path.unlink(missing_ok=True)
                    raise RuntimeError(f"неполная загрузка файла ({actual_size} из {expected_size or '?'} байт)")
            except Exception:  # noqa: BLE001
                log.exception("Не удалось скачать медиа %s от %s", msg.id, user.id)
                self.db.save_circle(user.id, msg.id, dt.isoformat(), duration, msg.out, "", False,
                                    topic_id=self.db.get_topic(user.id), media_type=media_type,
                                    mime_type=metadata["mime_type"], file_size=metadata["file_size"])
                await self._alert("media-dl", f"⚠️ Не удалось скачать {ARCHIVE_MEDIA_LABELS[media_type].lower()} от {display_name(user)} "
                                  "(попробую ещё раз позже, подробности в 📜 Логи).")
                return
            metadata["file_size"] = actual_size
            log.info("Скачан %s (%s, %s байт)", path.name, display_name(user), actual_size)
            if self.media_type(msg) != media_type:
                # Owner disabled this media type while download_media was awaiting Telegram.
                path.unlink(missing_ok=True)
                return

            # 2. отправка в тему
            sent = False
            for _ in range(3):
                try:
                    topic_id = await self.ensure_topic(user)
                    await self.send_media(topic_id, msg, path, media_type, metadata)
                    sent = True
                    break
                except errors.FloodWaitError as e:
                    log.warning("FloodWait %s сек", e.seconds)
                    await asyncio.sleep(e.seconds + self.FLOOD_PAD)
                except Exception as e:  # noqa: BLE001
                    if is_topic_error(e):
                        log.warning("Тема недоступна (%s) - создам заново", e)
                        self.db.drop_topic(user.id)
                    else:
                        log.exception("Ошибка отправки")
                        break

            if not sent:
                await self._alert("media-send", f"⚠️ Не удалось отправить {ARCHIVE_MEDIA_LABELS[media_type].lower()} от {display_name(user)} "
                                  "в архив. Он сохранён и будет отправлен повторно (подробности в 📜 Логи).")

            # 3. подпись (необязательная - сбой не отменяет успешную отправку)
            if sent and self.cfg["info_message"]:
                try:
                    who = "➡️ я" if msg.out else "⬅️ собеседник"
                    kind_label = ARCHIVE_MEDIA_LABELS[media_type]
                    await self.client.send_message(
                        self.target,
                        f"{kind_label} · 🕒 {dt:%Y-%m-%d %H:%M} UTC"
                        + (f" · {duration // 60}:{duration % 60:02d}" if duration else "")
                        + f" · {who}",
                        reply_to=self.db.get_topic(user.id))
                except Exception as e:  # noqa: BLE001
                    log.warning("Подпись не отправлена: %s", e)

            keep = self.cfg["keep_local"] or not sent
            if sent and not keep:
                path.unlink(missing_ok=True)
            self.db.save_circle(user.id, msg.id, dt.isoformat(), duration, msg.out,
                                str(path) if keep else "", sent,
                                topic_id=self.db.get_topic(user.id), media_type=media_type,
                                mime_type=metadata["mime_type"], file_size=metadata["file_size"])
            await asyncio.sleep(self.PAUSE)  # пауза против флуд-лимитов

    async def retry_unsent(self):
        for user_id, msg_id in self.db.unsent():
            try:
                msg = await self.client.get_messages(user_id, ids=msg_id)
                if msg and self.media_type(msg):
                    await self.process(msg, await self.client.get_entity(user_id))
            except Exception:  # noqa: BLE001
                log.exception("Повтор для %s/%s не удался", user_id, msg_id)

    async def periodic_retry(self, first_delay=10, period=1800):
        await asyncio.sleep(first_delay)
        while True:
            try:
                if self.target is None and await self.setup_target() is None and self.notifier:
                    await self.notifier.send("✅ Целевая группа найдена - архиватор снова работает. "
                                             "Пропущенные ранее медиа можно забрать через «📥 Старые медиа».")
                if self.target is not None:
                    await self.retry_unsent()
            except Exception:  # noqa: BLE001
                log.exception("Сбой повторной отправки")
            await asyncio.sleep(period)

    async def cleanup_sent(self):
        """Удаляет с диска копии уже отправленных медиафайлов. Возвращает (штук, байт)."""
        rows = self.db.sent_with_files()

        def work():
            n = freed = 0
            done = []
            for uid, mid, p in rows:
                f = Path(p)
                try:
                    if f.exists():
                        freed += f.stat().st_size
                        f.unlink()
                    done.append((uid, mid))
                    n += 1
                except OSError:
                    pass
            return n, freed, done

        n, freed, done = await asyncio.get_running_loop().run_in_executor(None, work)
        self.db.clear_paths(done)
        return n, freed

    async def backfill(self, limit=None, progress=print) -> int:
        """Resume historical private-dialog scan with independent cursors per media filter."""
        n = 0
        if self.target is None:
            raise RuntimeError("Группа архива не настроена. Сначала проверь ID целевой группы и права записи.")
        self.backfill_scanned = 0
        self.backfill_archived = 0
        self.backfill_current = "Подготовка списка личных чатов"
        selected = [kind for kind in ("round_video", "photo", "video")
                    if kind in self.selected_media_types()]
        if not selected:
            raise RuntimeError("Не выбраны типы медиа. Откройте «🎞 Типы медиа» и включите нужные.")

        async for dialog in self.client.iter_dialogs():
            if self.stop_flag:
                break
            if not dialog.is_user:
                continue
            user = dialog.entity
            if user.bot or user.is_self:
                continue
            per_chat_archived = 0
            per_chat_scanned = 0  # unique message IDs, for honest progress counts
            per_chat_filter_yielded = 0  # raw server-filter results, for safe limit handling
            per_chat_seen_ids = set()
            per_chat_processed_ids = set()
            for media_type in selected:
                if self.stop_flag or (limit is not None and per_chat_filter_yielded >= int(limit)):
                    break
                scan_specs = ARCHIVE_MEDIA_SCAN_FILTERS.get(media_type, ())
                if not scan_specs:
                    raise RuntimeError(f"Установленный Telethon не поддерживает фильтры {media_type}")
                for cursor_key, filter_cls in scan_specs:
                    if self.stop_flag or (limit is not None and per_chat_filter_yielded >= int(limit)):
                        break
                    last_msg_id, _completed, _old_scanned, _old_archived = self.db.backfill_cursor(
                        user.id, cursor_key)
                    cursor = int(last_msg_id or 0)
                    scanned_delta = 0
                    archived_delta = 0
                    remaining = None if limit is None else max(0, int(limit) - per_chat_filter_yielded)
                    filter_yielded = 0
                    self.backfill_current = f"{display_name(user)} · {ARCHIVE_MEDIA_LABELS[media_type]}"
                    iterator = self.client.iter_messages(
                        user, filter=filter_cls, reverse=True, min_id=cursor,
                        limit=remaining, wait_time=1)
                    async for msg in iterator:
                        if self.stop_flag:
                            break
                        filter_yielded += 1
                        per_chat_filter_yielded += 1
                        detected = classify_archive_media(msg)
                        # A type may be disabled while the historical scan is running.
                        # Do not advance its cursor over an item that was not archived.
                        if detected == media_type and detected not in self.selected_media_types():
                            self.stop_flag = True
                            break
                        cursor = max(cursor, int(msg.id))
                        message_id = int(msg.id)
                        if message_id not in per_chat_seen_ids:
                            per_chat_seen_ids.add(message_id)
                            scanned_delta += 1
                            per_chat_scanned += 1
                            self.backfill_scanned += 1
                        if (detected == media_type and detected in self.selected_media_types()
                                and message_id not in per_chat_processed_ids):
                            per_chat_processed_ids.add(message_id)
                            before = self.db.is_done(user.id, msg.id)
                            await self.process(msg, user, force=True)
                            if not before and self.db.is_done(user.id, msg.id):
                                n += 1
                                per_chat_archived += 1
                                archived_delta += 1
                                self.backfill_archived += 1
                                progress(f"[{n}] {display_name(user)} · {ARCHIVE_MEDIA_LABELS[media_type]}")
                        # Persist every examined attachment. If a download fails, retry_unsent
                        # can recover it; if the process crashes, the cursor resumes safely.
                        self.db.save_backfill_state(user.id, cursor_key, cursor,
                                                    scanned_delta, archived_delta, completed=False)
                        scanned_delta = 0
                        archived_delta = 0

                    if self.stop_flag:
                        self.db.save_backfill_state(user.id, cursor_key, cursor,
                                                    scanned_delta, archived_delta, completed=False)
                        return n

                    # If the filter had no matching records, move its cursor to the current
                    # dialog tail so future runs only inspect newer messages.
                    if cursor == int(last_msg_id or 0):
                        try:
                            latest = await self.client.get_messages(user, limit=1)
                            if latest:
                                cursor = max(cursor, int(latest[0].id))
                        except Exception as exc:  # noqa: BLE001
                            log.debug("Не удалось получить последний ID диалога %s: %s", user.id, exc)
                    # If the iterator yielded exactly its requested limit, more results may
                    # remain even when some IDs overlapped with another media filter.
                    # Keep the cursor resumable; the next run will prove exhaustion.
                    completed = remaining is None or filter_yielded < remaining
                    self.db.save_backfill_state(user.id, cursor_key, cursor,
                                                scanned_delta, archived_delta, completed=completed)
            self.backfill_current = f"{display_name(user)} · готово"
        return n


# =============================================================================
#  Рассылка (пост из «Избранного» конкретного аккаунта по выбранным группам)
# =============================================================================
REASON_LABELS = {"noperm": "нет прав на запись", "flood": "FloodWait", "slow": "медленный режим",
                 "spam": "ограничение аккаунта", "account_restricted": "ограничение аккаунта",
                 "source_restricted": "пересылка исходного поста запрещена", "uncertain": "результат отправки неизвестен",
                 "other": "прочее", "paid": "платные сообщения", "noplain": "запрещён обычный пост",
                 "topic": "топик закрыт", "nolinks": "запрещены ссылки"}
# вердикт broadcast_core -> причина в статистике волны
KIND_TO_REASON = {"paid": "paid", "no_plain": "noplain", "topic_closed": "topic",
                  "no_links": "nolinks", "no_rights": "noperm"}
# после этих причин сообщение не ушло мгновенно - длинная пауза между чатами не нужна
FAST_FAIL = ("noperm", "other", "slow", "paid", "noplain", "topic", "nolinks",
             "account_restricted", "source_restricted", "uncertain")


class Broadcaster:
    DELAY_MIN, DELAY_MAX = 0.6, 1.2          # переопределяются из config.json
    SUB_DELAY = 2
    LONG_PAUSE_CHANCE = 0.02
    LONG_PAUSE_MIN, LONG_PAUSE_MAX = 8, 15
    FAIL_DELAY = 0.3                          # после неудачи (ничего не ушло) ждём меньше
    MAX_SUBS_PER_MENTION = 24
    AUTOSUB_EVENT_CACHE_SIZE = 2048
    SAVE_EVERY_N = 10
    FLOOD_MAX_WAIT = 600     # FloodWait до этого значения пережидаем, больше - прерываем волну
    FLOOD_PAD = 1
    SPAM_WAIT = 3            # сколько ждать ответа @SpamBot
    SPAM_STOP_AFTER = 3      # столько спам-блоков подряд - рассылка останавливается
    HISTORY_KEEP = 30
    # USER_BANNED_IN_CHANNEL is destination-specific: it means this account
    # cannot post in THIS supergroup/channel, not that the whole account is spam-limited.
    NOPERM_ERRORS = tuple(getattr(errors, n) for n in (
        "ChatWriteForbiddenError", "ChatAdminRequiredError", "ChatRestrictedError",
        "ChatSendMediaForbiddenError", "UserNotParticipantError", "ChannelPrivateError",
        "UserBannedInChannelError") if hasattr(errors, n))
    # PeerFlood is account-scoped. Do not confuse it with USER_BANNED_IN_CHANNEL.
    ACCOUNT_RESTRICTION_ERRORS = tuple(getattr(errors, n) for n in ("PeerFloodError",)
                                       if hasattr(errors, n))
    SOURCE_FORWARD_ERRORS = tuple(getattr(errors, n) for n in (
        "ChatForwardsRestrictedError", "MessageForwardsRestrictedError") if hasattr(errors, n))

    def __init__(self, cfg, client, ddir, notifier=None, files_dir=None, enabled=True):
        self.cfg, self.client, self.ddir = cfg, client, Path(ddir)
        self.enabled = bool(enabled)
        self.notifier = notifier or Notifier()
        # Legacy root-level subscription files belong only to the primary account.
        # Never import them into secondary profiles: their broadcast state is isolated.
        self._legacy = (files_dir is None and
                        Path(cfg.get("_config_path") or DEFAULT_CONFIG_PATH).resolve() == DEFAULT_CONFIG_PATH.resolve())
        fd = Path(files_dir) if files_dir else self.ddir
        self.f_subs, self.f_banned, self.f_bots = (fd / "subscriptions.json", fd / "banned_chats.json",
                                                   fd / "started_bots.json")
        # Pending private-invite join requests are stored by SHA-256 fingerprint only;
        # raw invite hashes are credentials and must not be written to disk or logs.
        self.f_pending_invites = fd / "pending_invites.json"
        self.pending_invites: dict = {}
        self.f_state = self.ddir / "broadcast_state.json"
        self.f_slow = self.ddir / "slow_mode_until.json"
        self.joined: dict = {}                 # historical labels, retained for compatibility
        self.current_memberships: set[int] = set()  # refreshed from live Telegram dialogs
        self._dialogs_scanned_at = 0.0
        self.banned: set = set()
        self.skip = bcore.Store(str(fd / "chat_skip.db"))   # временный чёрный список (платные, topic closed и т.д.)
        # Private invite hashes and bot start payloads are stored only in this
        # owner-only per-profile queue DB. It is deliberately excluded from portable backups.
        self.autosub_queue = bcore.DurableActionQueue(str(self.ddir / "autosub_queue.db"))
        self._slow_until: dict = {}                          # chat_id -> когда истечёт медленный режим
        self.started_bots: set = set()
        self.state = self._new_state()
        self.total_sent = 0            # с момента старта процесса
        self.running = False
        self.suspending = False        # остановка из-за выключения сервиса (рассылка потом продолжится)
        self.task = None
        self.progress = (0, 0)         # (чат, всего) в текущей волне
        self.chat_cache: list = []
        self.stop_event = asyncio.Event()
        self.schedule_changed = asyncio.Event()
        self.save_lock = asyncio.Lock()
        self.dirty = 0
        self._seen_autosub_events: dict[str, float] = {}
        self._subscribe_lock = asyncio.Lock()
        self._resumed = False
        self._abort = self._fatal = False
        self._fatal_reason = ""
        self._flood_wait = 0
        self._consec_spam = 0
        self._scan_task = None         # идущее сейчас сканирование диалогов (чтобы не запускать два одновременно)
        self._apply_delays()
        # Link capture is session-level, but queued actions are executed only while
        # a broadcast campaign is active. The durable queue remains available offline.
        self._subscribe_tasks: set[asyncio.Task] = set()  # legacy batch tasks; retained for graceful migration
        self._autosub_worker_task: asyncio.Task | None = None
        self._autosub_wakeup = asyncio.Event()
        self._autosub_worker_stopping = False
        if self.enabled:
            client.add_event_handler(self._on_mention, events.NewMessage(incoming=True))
            # Some bots edit the same requirement message to add/fix invite buttons.
            client.add_event_handler(self._on_mention, events.MessageEdited(incoming=True))

    def _apply_delays(self):
        """Паузы берём из config.json (меняются в боте, действуют со следующей волны)."""
        try:
            lo = max(0.0, float(self.cfg.get("broadcast_delay_min", 0.6)))
            hi = max(lo, float(self.cfg.get("broadcast_delay_max", 1.2)))
            ch = min(1.0, max(0.0, float(self.cfg.get("broadcast_long_pause_chance", 0.02))))
        except (TypeError, ValueError):
            lo, hi, ch = 0.6, 1.2, 0.02
        self.DELAY_MIN, self.DELAY_MAX, self.LONG_PAUSE_CHANCE = lo, hi, ch

    # ----- состояние на диске -----
    @staticmethod
    def _new_state():
        return {"active": False, "end": 0.0, "interval": 60, "post_id": 0, "next": 0.0,
                "flood_until": 0.0, "wave": 0, "started": 0.0, "run_ok": 0, "run_fail": 0,
                "life_ok": 0, "life_fail": 0, "life_waves": 0, "history": [], "pending_wave": None,
                "last_error": ""}

    @staticmethod
    def _read(path: Path, default):
        """Read JSON defensively; a valid JSON value of the wrong shape is still invalid here."""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, type(default)) else default
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return default

    @staticmethod
    def _write(path: Path, data):
        """Durable atomic JSON write: flush file data before replacing the checkpoint."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def _save_state(self) -> bool:
        try:
            self._write(self.f_state, self.state)
            return True
        except Exception as e:  # noqa: BLE001
            blog.warning("Не удалось сохранить состояние рассылки: %s", e)
            return False

    def load_files(self):
        if self._legacy:   # подхватываем кеш от старого bot-6.py, лежавший рядом со скриптом
            for f in (self.f_subs, self.f_banned, self.f_bots):
                old = BASE / f.name
                if old.exists() and not f.exists():
                    try:
                        shutil.copy(old, f)
                    except OSError:
                        pass
        raw_joined = self._read(self.f_subs, {})
        self.joined = {str(k): str(v) for k, v in raw_joined.items()}
        raw_banned = self._read(self.f_banned, {})
        self.banned = set()
        for key in raw_banned:
            try:
                self.banned.add(int(key))
            except (TypeError, ValueError):
                blog.warning("Пропускаю некорректный ID в бан-листе аккаунта: %r", key)
        raw_bots = self._read(self.f_bots, [])
        # Legacy files contain only usernames. Keep those markers for no-parameter
        # /start links while new records are keyed by bot + exact referral payload.
        self.started_bots = {str(x) for x in raw_bots if isinstance(x, (str, int))}
        raw_pending = self._read(self.f_pending_invites, {})
        self.pending_invites = {str(k): dict(v) for k, v in raw_pending.items()
                                if isinstance(v, dict)}
        st = self._new_state()
        raw_state = self._read(self.f_state, {})
        st.update(raw_state)

        def safe_int(value, default=0, minimum=0):
            try:
                return max(minimum, int(value))
            except (TypeError, ValueError, OverflowError):
                return default

        def safe_float(value, default=0.0, minimum=0.0):
            try:
                number = float(value)
                return max(minimum, number) if math.isfinite(number) else default
            except (TypeError, ValueError, OverflowError):
                return default

        st["active"] = st.get("active") is True
        for key in ("end", "next", "flood_until", "started"):
            st[key] = safe_float(st.get(key), 0.0)
        for key, default in (("interval", 60), ("post_id", 0), ("wave", 0), ("run_ok", 0),
                             ("run_fail", 0), ("life_ok", 0), ("life_fail", 0), ("life_waves", 0)):
            st[key] = safe_int(st.get(key), default, minimum=1 if key == "interval" else 0)
        st["last_error"] = str(st.get("last_error") or "")[:240]
        raw_history = st.get("history")
        st["history"] = [x for x in raw_history if isinstance(x, dict)][-self.HISTORY_KEEP:] \
            if isinstance(raw_history, list) else []
        pending = st.get("pending_wave")
        if pending is not None and not isinstance(pending, dict):
            blog.warning("Повреждён checkpoint рассылки; незавершённая волна сброшена")
            pending = None
        if isinstance(pending, dict):
            raw_chat_ids = pending.get("chat_ids", [])
            if not isinstance(raw_chat_ids, list):
                raw_chat_ids = []
            chat_ids = []
            for value in raw_chat_ids:
                try:
                    chat_ids.append(int(value))
                except (TypeError, ValueError, OverflowError):
                    continue
            if not chat_ids:
                blog.warning("Checkpoint не содержит корректных ID чатов; незавершённая волна сброшена")
                pending = None
            else:
                raw_completed = pending.get("completed_ids", [])
                completed = set()
                if isinstance(raw_completed, list):
                    for value in raw_completed:
                        try:
                            completed.add(int(value))
                        except (TypeError, ValueError, OverflowError):
                            continue
                pending["chat_ids"] = list(dict.fromkeys(chat_ids))
                pending["completed_ids"] = sorted(completed.intersection(pending["chat_ids"]))
                pending["post_id"] = safe_int(pending.get("post_id"), 0)
                pending["started"] = safe_float(pending.get("started"), time.time())
                for key in ("ok", "fail", "skipped"):
                    pending[key] = safe_int(pending.get(key), 0)
                raw_errors = pending.get("errors", {})
                pending["errors"] = {str(k): safe_int(v, 0) for k, v in raw_errors.items()} \
                    if isinstance(raw_errors, dict) else {}
                raw_failed = pending.get("failed", [])
                pending["failed"] = [list(x[:2]) for x in raw_failed
                                      if isinstance(x, (list, tuple)) and len(x) >= 2][-50:] \
                    if isinstance(raw_failed, list) else []
        st["pending_wave"] = pending
        self.state = st

        # Slow-mode deadlines survive service/VPS restarts, so the same chat is not
        # retried before Telegram's stated per-chat cooldown has elapsed.
        raw_slow = self._read(self.f_slow, {})
        now = time.time()
        self._slow_until = {}
        if isinstance(raw_slow, dict):
            for raw_id, raw_until in raw_slow.items():
                try:
                    cid, until = int(raw_id), float(raw_until)
                    if math.isfinite(until) and until > now:
                        self._slow_until[cid] = until
                except (TypeError, ValueError, OverflowError):
                    continue
        self._save_slow_modes()
        blog.info("Состояние: подписок %s, забаненных чатов %s, запущенных ботов %s, SlowMode-кулдаунов %s",
                  len(self.joined), len(self.banned), len(self.started_bots), len(self._slow_until))

    def _save_slow_modes(self) -> bool:
        now = time.time()
        self._slow_until = {int(cid): float(until) for cid, until in self._slow_until.items()
                            if float(until) > now}
        try:
            self._write(self.f_slow, {str(cid): until for cid, until in self._slow_until.items()})
            return True
        except Exception as e:  # noqa: BLE001
            blog.warning("Не удалось сохранить SlowMode-кулдауны: %s", e)
            return False

    @staticmethod
    def _int_ids(values) -> set[int]:
        if not isinstance(values, (list, tuple, set)):
            return set()
        result = set()
        for value in values:
            try:
                result.add(int(value))
            except (TypeError, ValueError, OverflowError):
                continue
        return result

    async def save_subs(self, force=False) -> bool:
        self.dirty += 1
        if not force and self.dirty < self.SAVE_EVERY_N:
            return True
        async with self.save_lock:
            try:
                await asyncio.to_thread(self._write, self.f_subs, dict(self.joined))
                self.dirty = 0
                return True
            except Exception as e:  # noqa: BLE001
                blog.warning("Не удалось сохранить подписки: %s", e)
                return False

    async def add_sub(self, channel_id, label):
        self.joined[str(channel_id)] = label
        await self.save_subs()

    async def _save_banned(self, data) -> bool:
        async with self.save_lock:
            try:
                await asyncio.to_thread(self._write, self.f_banned, data)
                return True
            except Exception as e:  # noqa: BLE001
                blog.warning("Не удалось сохранить бан-лист: %s", e)
                return False

    async def ban_chat(self, chat_id, name):
        self.banned.add(chat_id)
        data = self._read(self.f_banned, {})
        if not isinstance(data, dict):
            data = {}
        data[str(chat_id)] = name
        if not await self._save_banned(data):
            raise OSError("Не удалось сохранить запрет отправки для чата")

    async def unban_chat(self, chat_id):
        data = self._read(self.f_banned, {})
        if not isinstance(data, dict):
            data = {}
        data.pop(str(chat_id), None)
        if not await self._save_banned(data):
            raise OSError("Не удалось сохранить повторное включение чата")
        self.banned.discard(chat_id)

    async def save_bots(self):
        async with self.save_lock:
            try:
                await asyncio.to_thread(self._write, self.f_bots, sorted(self.started_bots))
            except Exception as e:  # noqa: BLE001
                blog.warning("Не удалось сохранить started_bots: %s", e)

    # ----- сообщения владельцу -----
    async def say(self, text, buttons=None):
        blog.info(text.replace("\n", " | "))
        return await self.notifier.send(text, buttons)

    async def alert(self, key, text, cooldown=1800):
        blog.warning(text.replace("\n", " | "))
        return await self.notifier.alert(key, text, cooldown)

    # ----- диалоги и чаты -----
    async def _scan_dialogs(self):
        """Один проход по диалогам. Параллельные вызовы (волна + кнопка в боте) делят один проход."""
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.ensure_future(self._scan_dialogs_impl())
        return await asyncio.shield(self._scan_task)

    async def _scan_dialogs_impl(self):
        groups, new = [], 0
        current_memberships = set()
        async for dialog in self.client.iter_dialogs(ignore_migrated=True):
            ent = dialog.entity
            if not isinstance(ent, (types.Channel, types.Chat)):
                continue
            current_memberships.add(int(ent.id))
            eid = str(ent.id)
            if eid not in self.joined:
                self.joined[eid] = getattr(ent, "username", None) or eid
                new += 1
            if getattr(ent, "deactivated", False):
                continue
            # Broadcast recipients are groups only: basic groups + supergroups/gigagroups.
            # A regular broadcast channel is never a campaign destination, even if this
            # account has admin rights there. Keep all memberships above for auto-subscribe.
            is_group = isinstance(ent, types.Chat)
            if isinstance(ent, types.Channel):
                is_group = (bool(getattr(ent, "megagroup", False))
                            or bool(getattr(ent, "gigagroup", False))) \
                    and not bool(getattr(ent, "broadcast", False))
            if is_group:
                groups.append(ent)
        self.current_memberships = current_memberships
        self._dialogs_scanned_at = time.time()
        if new:
            await self.save_subs(force=True)
        self.chat_cache = [(e.id, getattr(e, "title", None) or str(e.id)) for e in groups]

        # Old versions could save broadcast-channel IDs in the recipient allowlist.
        # Prune them (and IDs no longer in the current group list) before the UI or
        # campaign can reuse that saved selection.
        if self.cfg.get("broadcast_allowlist_only"):
            raw_allowlist = self.cfg.get("broadcast_allowlist", [])
            selected = self._int_ids(raw_allowlist)
            group_ids = {int(entity.id) for entity in groups}
            valid_ids = sorted(selected.intersection(group_ids))
            if not isinstance(raw_allowlist, list) or raw_allowlist != valid_ids:
                self.cfg["broadcast_allowlist"] = valid_ids
                save_cfg(self.cfg)
        return groups

    async def load_dialogs(self) -> list:
        groups = await self._scan_dialogs()
        excl = self._int_ids(self.cfg.get("broadcast_exclude", []))
        now = time.time()
        if any(until <= now for until in self._slow_until.values()):
            self._save_slow_modes()
        chats, n_skip, n_slow = [], 0, 0
        # The web control center can switch a profile to explicit-recipient mode.
        # In that mode only IDs deliberately selected in the UI are eligible.
        allowlist_only = bool(self.cfg.get("broadcast_allowlist_only", False))
        allowlist = self._int_ids(self.cfg.get("broadcast_allowlist", [])) if allowlist_only else set()
        for e in groups:
            if allowlist_only and int(e.id) not in allowlist:
                continue
            if e.id in self.banned or e.id in excl:
                continue
            if self.skip.blocked_until(e.id) is not None:
                n_skip += 1
                continue
            if self._slow_until.get(e.id, 0) > now:
                n_slow += 1
                continue
            # No administrator check: Telegram enforces real send permissions when
            # forward() runs. Forbidden chats are recorded and skipped individually.
            chats.append(e)
        blog.info("Групп для рассылки: %s (всего групп %s; пропущено: чёрный список %s, медленный режим %s, исключения %s)",
                  len(chats), len(groups), n_skip, n_slow, len(excl))
        return chats

    async def chat_list(self, refresh=False):
        if refresh or not self.chat_cache:
            await self._scan_dialogs()
        return list(self.chat_cache)

    def chat_status(self, cid) -> str:
        if cid in self.banned:
            return "ban"
        if cid in self._int_ids(self.cfg.get("broadcast_exclude", [])):
            return "ex"
        return "ok"

    async def toggle_chat(self, cid) -> str:
        """Toggle a group in the exclusion list; all other groups are included by default."""
        if cid in self.banned:
            await self.unban_chat(cid)
        else:
            excluded = self._int_ids(self.cfg.get("broadcast_exclude", []))
            if cid in excluded:
                excluded.remove(cid)
            else:
                excluded.add(cid)
            self.cfg["broadcast_exclude"] = sorted(excluded)
        self.cfg["broadcast_allowlist_only"] = False
        self.cfg["broadcast_allowlist"] = []
        save_cfg(self.cfg)
        return self.chat_status(cid)

    # ----- пост -----
    async def saved_posts(self, limit: int = 10) -> list:
        """Return the newest messages from THIS user account's Saved Messages."""
        limit = max(1, min(int(limit), 10))
        messages = await self.client.get_messages("me", limit=limit)
        return [m for m in (messages or []) if m and not getattr(m, "action", None)]

    async def select_post(self, message_id: int):
        """Pin a concrete message from this account's own Saved Messages."""
        msg = await self.client.get_messages("me", ids=int(message_id))
        if not msg:
            return None
        self.state["post_id"] = int(msg.id)
        self._save_state()
        return self._post_info(msg, True)

    async def select_all_groups(self, retry_unavailable: bool = False) -> tuple[int, int]:
        """Include every group without an admin filter; optionally recheck old no-write chats.

        The normal selection keeps Telegram-rejected destinations excluded. If the owner
        explicitly asks to retry unavailable groups, clear only those still present in
        the current dialog list; Telegram will re-apply the destination restriction if it
        remains in force.
        """
        groups = await self._scan_dialogs()
        checked = len(groups)
        if retry_unavailable:
            current_ids = {int(entity.id) for entity in groups}
            retry_ids = self.banned.intersection(current_ids)
            if retry_ids:
                stored = self._read(self.f_banned, {})
                if not isinstance(stored, dict):
                    stored = {}
                for cid in retry_ids:
                    stored.pop(str(cid), None)
                if not await self._save_banned(stored):
                    raise OSError("Не удалось сохранить повторную проверку недоступных групп")
                self.banned.difference_update(retry_ids)
        self.cfg["broadcast_allowlist_only"] = False
        self.cfg["broadcast_allowlist"] = []
        self.cfg["broadcast_exclude"] = []
        save_cfg(self.cfg)
        selected = sum(1 for entity in groups if int(entity.id) not in self.banned)
        return selected, checked

    async def get_post(self):
        st = self.state
        if not st["post_id"]:
            saved = await self.client.get_messages("me", limit=1)
            if not saved:
                await self.alert("nopost", "❌ Избранное этого аккаунта пустое! Отправь пост в его «Избранное» и выбери его в панели.", 0)
                return None
            st["post_id"] = saved[0].id
            self._save_state()
            return saved[0]
        msg = await self.client.get_messages("me", ids=st["post_id"])
        if not msg:
            st["post_id"] = 0
            self._save_state()
            await self.alert("nopost", "❌ Выбранный пост удалён из «Избранного» этого аккаунта. Выбери другой пост в панели.", 0)
            return None
        return msg

    @staticmethod
    def _post_info(msg, pinned):
        return {"id": msg.id, "text": getattr(msg, "text", None) or "",
                "media": bool(getattr(msg, "media", None)), "pinned": pinned}

    async def preview(self):
        """Что сейчас уйдёт в рассылку: закреплённый пост или (если не закреплён) последний из Избранного."""
        pid = self.state["post_id"]
        if pid:
            msg = await self.client.get_messages("me", ids=pid)
            if msg:
                return self._post_info(msg, True)
        saved = await self.client.get_messages("me", limit=1)
        return self._post_info(saved[0], False) if saved else None

    async def repin(self):
        saved = await self.client.get_messages("me", limit=1)
        if not saved:
            return None
        self.state["post_id"] = saved[0].id
        self._save_state()
        return self._post_info(saved[0], True)

    # ----- упоминания ботом -> подписка -----
    async def _is_safe_redirect_target(self, url: str) -> bool:
        """Only resolve public HTTP(S) URLs; reject local/private network targets."""
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return False
            if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                return False
            host = parsed.hostname.rstrip(".").lower()
            if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
                return False
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
                addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
            return bool(addresses) and all(addr.is_global for addr in addresses)
        except (ValueError, OSError, socket.gaierror):
            return False

    async def resolve_redirect(self, url: str) -> str:
        """Follow at most five public redirects, validating each hop to limit SSRF risk."""
        if aiohttp is None:
            return url
        current = url
        timeout = aiohttp.ClientTimeout(total=6, connect=3, sock_read=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                for _ in range(5):
                    if not await self._is_safe_redirect_target(current):
                        return url
                    async with session.get(
                        current, allow_redirects=False,
                        headers={"User-Agent": "Mozilla/5.0"},
                    ) as response:
                        location = response.headers.get("Location")
                        if response.status not in {301, 302, 303, 307, 308} or not location:
                            return str(response.url)
                        current = urljoin(current, location)
                return current if await self._is_safe_redirect_target(current) else url
        except Exception as e:  # noqa: BLE001
            blog.warning("Редирект не раскрыт: %s", type(e).__name__)
            return url

    @staticmethod
    def _parse_telegram_link(url: str):
        """Parse public chat links, private invites and bot deep links.

        Supported examples:
          https://t.me/channel_name
          https://username.t.me/123
          https://t.me/+InviteHash / https://t.me/joinchat/InviteHash
          tg://join?invite=InviteHash
          https://t.me/mybot?start=ref_123
          tg://resolve?domain=mybot&start=ref_123

        startgroup/startapp/startattach links intentionally are not treated as
        private /start commands: those flows require an explicit user context.
        """
        try:
            raw = (url or "").strip().strip("<>").rstrip(".,;!?)\\\"']")
            if not raw:
                return None
            # Telegram clients also accept bare t.me/... links in message text.
            if raw.lower().startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/",
                                       "telegram.dog/", "www.telegram.dog/")):
                raw = "https://" + raw
            parsed = urlsplit(raw)
            scheme = (parsed.scheme or "").lower()
            query = parse_qs(parsed.query, keep_blank_values=True)

            def bot_target(username, params):
                username = (username or "").lstrip("@").strip()
                if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                    return None
                # Telegram bot deep-link payload: 1..64 URL-safe characters.
                if "start" in params:
                    payload = (params.get("start") or [""])[0]
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload):
                        return None
                    return {"type": "bot", "value": username, "start_param": payload}
                if any(k in params for k in ("startgroup", "startapp", "startattach")):
                    return None
                return {"type": "username", "value": username}

            if scheme == "tg":
                action = (parsed.netloc or parsed.path.lstrip("/")).lower()
                if action == "join":
                    invite = (query.get("invite") or [""])[0]
                    if invite and re.fullmatch(r"[A-Za-z0-9_-]+", invite):
                        return {"type": "invite_hash", "value": invite}
                    return None
                if action == "resolve":
                    return bot_target((query.get("domain") or [""])[0], query)
                return None

            if scheme not in {"http", "https"}:
                return None
            host = (parsed.hostname or "").lower().rstrip(".")
            official_hosts = {"t.me", "www.t.me", "telegram.me", "www.telegram.me",
                              "telegram.dog", "www.telegram.dog"}
            parts = [unquote(part) for part in parsed.path.split("/") if part]

            # Telegram documents <username>.t.me as a username link too.
            subdomain_username = None
            for suffix in (".t.me", ".telegram.me", ".telegram.dog"):
                if host.endswith(suffix) and host not in {"www" + suffix}:
                    candidate = host[:-len(suffix)]
                    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", candidate):
                        subdomain_username = candidate
                    break

            if host not in official_hosts and subdomain_username is None:
                return None
            if subdomain_username:
                return bot_target(subdomain_username, query)
            if not parts:
                return None
            if parts[0].lower() == "joinchat" and len(parts) >= 2:
                invite = parts[1]
                return {"type": "invite_hash", "value": invite} if re.fullmatch(r"[A-Za-z0-9_-]+", invite) else None
            if parts[0].startswith("+") and len(parts[0]) > 1:
                invite = parts[0][1:]
                return {"type": "invite_hash", "value": invite} if re.fullmatch(r"[A-Za-z0-9_-]+", invite) else None
            if parts[0].lower() == "s" and len(parts) >= 2:
                parts = parts[1:]
            reserved = {"share", "login", "proxy", "socks", "addstickers", "addemoji",
                        "addtheme", "addlist", "boost", "c", "iv", "confirmphone",
                        "joinchat", "setlanguage", "giftcode", "invoice", "contact",
                        "auction", "auth", "m", "nft", "web", "a", "k", "z"}
            username = parts[0].lstrip("@").strip()
            if username.lower() in reserved:
                return None
            return bot_target(username, query)
        except (TypeError, ValueError, UnicodeError):
            return None

    @staticmethod
    def _slice_utf16(text: str, offset: int, length: int) -> str:
        """Slice Telegram entity offsets, which are measured in UTF-16 code units."""
        try:
            raw = (text or "").encode("utf-16-le")
            return raw[max(0, int(offset)) * 2:(max(0, int(offset)) + max(0, int(length))) * 2].decode(
                "utf-16-le", errors="ignore")
        except (TypeError, ValueError, OverflowError):
            return ""

    @staticmethod
    def _message_urls(message) -> list[str]:
        """Collect URLs from inline buttons, raw markup and Telegram text entities."""
        urls, seen = [], set()

        def add(value):
            if isinstance(value, str):
                value = value.strip()
                if value and value not in seen:
                    seen.add(value)
                    urls.append(value)

        rows = getattr(message, "buttons", None) or (
            getattr(getattr(message, "reply_markup", None), "rows", None) or []
        )
        for row in rows or []:
            row_buttons = getattr(row, "buttons", None)
            if row_buttons is None:
                row_buttons = row
            for btn in row_buttons or []:
                add(getattr(btn, "url", None))

        text = (getattr(message, "raw_text", None) or getattr(message, "message", None)
                or getattr(message, "text", None) or "")
        for entity in getattr(message, "entities", None) or []:
            entity_url = getattr(entity, "url", None)  # MessageEntityTextUrl
            if entity_url:
                add(entity_url)
            elif type(entity).__name__ == "MessageEntityUrl":
                add(Broadcaster._slice_utf16(text, getattr(entity, "offset", 0),
                                             getattr(entity, "length", 0)))

        # Fallback for plain text in lightweight message wrappers or incomplete entities.
        # Do not scan arbitrary prose for @mentions; only recognizable Telegram URL forms.
        url_pattern = re.compile(
            r"(?i)(?:(?:https?://)?(?:www\.|[A-Za-z0-9_]{5,32}\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"'`]+|"
            r"tg://(?:join|resolve)\?[^\s<>\"'`]+)"
        )
        for match in url_pattern.finditer(text):
            add(match.group(0).rstrip(".,;!?)]}\"'`"))
        return urls

    @staticmethod
    def _inline_button_urls(message) -> list[str]:
        """Snapshot URL buttons before any await; the bot may delete its message."""
        urls, seen = [], set()

        def add_button(button):
            url = getattr(button, "url", None)
            if isinstance(url, str):
                url = url.strip()
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)

        try:
            matrix = getattr(message, "buttons", None)
        except Exception:
            matrix = None
        for row in matrix or []:
            for button in (getattr(row, "buttons", None) or row or []):
                add_button(button)
        markup = getattr(message, "reply_markup", None)
        for row in (getattr(markup, "rows", None) or []):
            for button in (getattr(row, "buttons", None) or []):
                add_button(button)
        return urls

    @staticmethod
    def _bot_start_key(username: str, start_param: str = "") -> str:
        # Deep-link payloads may contain short-lived auth/referral tokens. Persist
        # only a digest so deduplication works without writing the raw token to disk.
        token_id = hashlib.sha256(start_param.encode("utf-8")).hexdigest()[:20] if start_param else "plain"
        return f"bot:{(username or '').casefold()}:{token_id}"

    def _bot_start_was_sent(self, username: str, start_param: str = "") -> bool:
        key = self._bot_start_key(username, start_param)
        if key in self.started_bots:
            return True
        # Backward compatibility: previous releases stored only bot usernames.
        # A legacy marker suppresses a plain /start, but does not suppress a new
        # referral payload for the same bot.
        return not start_param and username.casefold() in {x.casefold() for x in self.started_bots}

    async def _start_bot(self, bot, username: str, start_param: str = "") -> bool:
        if not getattr(bot, "bot", False):
            blog.info("Ссылка @%s ведёт на обычного пользователя, не запускаю", username)
            return False
        start_param = start_param or ""
        if self._bot_start_was_sent(username, start_param):
            blog.info("Бот @%s с таким start-параметром уже запускался", username)
            return False
        text = f"/start {start_param}" if start_param else "/start"
        # Reconcile the narrow crash window: Telegram may have accepted /start
        # immediately before the process died and before local state was saved.
        # Checking recent outgoing commands avoids blindly sending the same start twice.
        try:
            recent = await self.client.get_messages(bot, limit=12)
            for prior in recent or []:
                if getattr(prior, "out", False) and (getattr(prior, "raw_text", None)
                        or getattr(prior, "message", None) or "").strip() == text:
                    self.started_bots.add(self._bot_start_key(username, start_param))
                    await self.save_bots()
                    blog.info("Подтвердил ранее отправленную команду /start для @%s", username)
                    return False
        except Exception as exc:  # noqa: BLE001
            blog.debug("Не удалось сверить историю /start для @%s: %s", username, type(exc).__name__)
        await self.client.send_message(bot, text)
        self.started_bots.add(self._bot_start_key(username, start_param))
        await self.save_bots()
        blog.info("Запустил бота @%s (%s)", username,
                  "с referral/start-параметром" if start_param else "без параметра")
        return True

    def _invite_fingerprint(self, invite_hash: str) -> str:
        return hashlib.sha256(str(invite_hash).encode("utf-8")).hexdigest()

    async def _save_pending_invites(self) -> bool:
        async with self.save_lock:
            try:
                await asyncio.to_thread(self._write, self.f_pending_invites, self.pending_invites)
                return True
            except Exception as e:  # noqa: BLE001
                blog.warning("Не удалось сохранить ожидающие приглашения: %s", type(e).__name__)
                return False

    async def extract_channels(self, message=None, *, urls=None) -> list:
        """Extract unique Telegram destinations from a pre-copied URL list or message."""
        found, seen = [], set()
        source_urls = list(urls) if urls is not None else self._message_urls(message)
        for original_url in source_urls:
            target = self._parse_telegram_link(original_url)
            if target is None and not original_url.lower().startswith("tg://"):
                # Non-Telegram short links are followed only through the existing
                # public-HTTP redirect resolver with private-network SSRF checks.
                target = self._parse_telegram_link(await self.resolve_redirect(original_url))
            if not target:
                continue
            # Distinct referral payloads for the same bot are distinct actions.
            key = (target["type"], target["value"].casefold(), target.get("start_param", ""))
            if key in seen:
                continue
            seen.add(key)
            found.append(target)
        return found

    async def _subscribe_one(self, ch: dict) -> dict:
        """Execute one queued Telegram action and return a durable, explicit outcome."""
        action_type = str(ch.get("type") or "")
        value = str(ch.get("value") or "")
        start_param = str(ch.get("start_param") or "")
        if not value:
            return {"status": "failed_permanent", "reason": "empty_target"}

        try:
            if action_type == "bot":
                ent = await self.client.get_entity(value)
                if not getattr(ent, "bot", False):
                    return {"status": "unsupported", "reason": "not_a_bot"}
                sent = await self._start_bot(ent, value, start_param)
                return {"status": "completed" if sent else "already_done", "reason": "start_sent" if sent else "start_already_sent", "rate_kind": "bot"}

            if action_type == "username":
                ent = await self.client.get_entity(value)
                if isinstance(ent, types.User):
                    if getattr(ent, "bot", False):
                        sent = await self._start_bot(ent, value, "")
                        return {"status": "completed" if sent else "already_done",
                                "reason": "start_sent" if sent else "start_already_sent", "rate_kind": "bot"}
                    return {"status": "unsupported", "reason": "ordinary_user_profile"}

                # The persisted subscriptions.json is historical and may be stale.
                # Check the current dialog list before deciding that we already joined.
                if time.time() - self._dialogs_scanned_at > 60:
                    await self._scan_dialogs()
                if int(ent.id) in self.current_memberships:
                    await self.add_sub(ent.id, f"@{value}")
                    await self.save_subs(force=True)
                    return {"status": "already_done", "reason": "already_member"}
                try:
                    await self.client(functions.channels.JoinChannelRequest(ent))
                except Exception as join_error:  # noqa: BLE001
                    if type(join_error).__name__ != "UserAlreadyParticipantError":
                        raise
                self.current_memberships.add(int(ent.id))
                await self.add_sub(ent.id, f"@{value}")
                await self.save_subs(force=True)
                return {"status": "completed", "reason": "joined_public_chat", "rate_kind": "join"}

            if action_type == "invite_hash":
                invite_hash = value
                fingerprint = self._invite_fingerprint(invite_hash)
                pending = self.pending_invites.get(fingerprint)
                if pending and pending.get("status") == "request_pending":
                    try:
                        checked = await self.client(functions.messages.CheckChatInviteRequest(invite_hash))
                        already_chat = getattr(checked, "chat", None)
                        if already_chat:
                            self.pending_invites.pop(fingerprint, None)
                            await self._save_pending_invites()
                            self.current_memberships.add(int(already_chat.id))
                            await self.add_sub(already_chat.id, getattr(already_chat, "title", None) or f"invite:{fingerprint[:10]}")
                            await self.save_subs(force=True)
                            return {"status": "completed", "reason": "request_approved", "rate_kind": "join",
                                    "action_already_counted": True}
                        if getattr(checked, "request_needed", False):
                            return {"status": "waiting_approval", "reason": "approval_pending"}
                    except Exception as check_error:  # noqa: BLE001
                        check_name = type(check_error).__name__
                        if check_name in {"FloodWaitError", "PeerFloodError", "ChannelsTooMuchError",
                                          "UserChannelsTooMuchError"}:
                            raise
                        if check_name in {"InviteHashExpiredError", "InviteHashInvalidError", "InviteHashEmptyError"}:
                            self.pending_invites.pop(fingerprint, None)
                            await self._save_pending_invites()
                            return {"status": "failed_permanent", "reason": check_name}
                        blog.warning("Не удалось проверить заявку по приватной ссылке: %s", check_name)
                        return {"status": "retry", "reason": check_name, "delay": 300}

                try:
                    result = await self.client(functions.messages.ImportChatInviteRequest(invite_hash))
                    chats = list(getattr(result, "chats", None) or [])
                    if not chats:
                        candidate = getattr(result, "chat", None)
                        if candidate is not None:
                            chats = [candidate]
                    if chats:
                        chat = chats[0]
                        self.current_memberships.add(int(chat.id))
                        await self.add_sub(chat.id, getattr(chat, "title", None) or f"invite:{fingerprint[:10]}")
                        await self.save_subs(force=True)
                    self.pending_invites.pop(fingerprint, None)
                    await self._save_pending_invites()
                    return {"status": "completed", "reason": "joined_private_chat", "rate_kind": "join"}
                except Exception as invite_error:  # noqa: BLE001
                    error_name = type(invite_error).__name__
                    error_text = str(invite_error).lower()
                    if error_name == "UserAlreadyParticipantError":
                        self.pending_invites.pop(fingerprint, None)
                        await self._save_pending_invites()
                        try:
                            check = await self.client(functions.messages.CheckChatInviteRequest(invite_hash))
                            chat = getattr(check, "chat", None)
                            if chat:
                                self.current_memberships.add(int(chat.id))
                                await self.add_sub(chat.id, getattr(chat, "title", None) or f"invite:{fingerprint[:10]}")
                                await self.save_subs(force=True)
                        except Exception:
                            pass
                        return {"status": "already_done", "reason": "already_member", "rate_kind": "join"}
                    if error_name in {"InviteRequestSentError", "InviteRequestSent"} or "invite_request_sent" in error_text:
                        self.pending_invites[fingerprint] = {
                            "status": "request_pending", "requested_at": int(time.time()),
                            "label": f"invite:{fingerprint[:10]}"
                        }
                        if not await self._save_pending_invites():
                            return {"status": "needs_review", "reason": "pending_state_not_saved"}
                        return {"status": "waiting_approval", "reason": "approval_pending"}
                    if error_name in {"InviteHashExpiredError", "InviteHashInvalidError", "InviteHashEmptyError"}:
                        return {"status": "failed_permanent", "reason": error_name}
                    if error_name in {"ChannelsTooMuchError", "UserChannelsTooMuchError"}:
                        return {"status": "account_paused", "reason": "join_capacity", "delay": DAY}
                    if error_name == "StarsPaymentRequiredError":
                        return {"status": "needs_review", "reason": "stars_payment_required"}
                    raise

            return {"status": "unsupported", "reason": "unsupported_link_type"}

        except errors.FloodWaitError as exc:
            seconds = max(1, int(getattr(exc, "seconds", 1)))
            return {"status": "retry", "reason": "flood_wait", "delay": seconds + 2, "account_pause": True}
        except self.ACCOUNT_RESTRICTION_ERRORS as exc:
            return {"status": "account_paused", "reason": f"account_restricted:{type(exc).__name__}"}
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            text = str(exc).lower()
            if name in {"ChannelsTooMuchError", "UserChannelsTooMuchError"}:
                return {"status": "account_paused", "reason": "join_capacity", "delay": DAY}
            if name in {"UsernameInvalidError", "UsernameNotOccupiedError", "InviteHashExpiredError",
                        "InviteHashInvalidError", "InviteHashEmptyError", "ChannelPrivateError"}:
                return {"status": "failed_permanent", "reason": name}
            if name == "StarsPaymentRequiredError":
                return {"status": "needs_review", "reason": "stars_payment_required"}
            if "nobody is using this username" in text or "no user has" in text:
                return {"status": "failed_permanent", "reason": "username_not_found"}
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
                return {"status": "retry", "reason": name, "delay": 60}
            blog.warning("Автоподписка: %s: %s", name, str(exc)[:180])
            return {"status": "retry", "reason": name, "delay": 60}

    async def subscribe(self, channels: list, _flood_retry: int = 0):
        """Compatibility entry point: persist actions and let the per-account worker process them."""
        inserted, duplicates = self.autosub_queue.enqueue_many(channels)
        if inserted and self.running and self.state.get("active"):
            self._autosub_wakeup.set()
            self._ensure_autosub_worker()
        return {"inserted": inserted, "duplicates": duplicates}

    def _ensure_autosub_worker(self, *, manual_resume: bool = False):
        if manual_resume:
            self.autosub_queue.clear_pause()
        if (not self.enabled or not self.cfg.get("broadcast_auto_subscribe", True)
                or not self.running or not self.state.get("active")
                or self._autosub_worker_stopping):
            return None
        if self._autosub_worker_task is None or self._autosub_worker_task.done():
            self._autosub_worker_task = asyncio.create_task(
                self._autosub_worker_loop(), name=f"autosub-worker:{self.ddir.name}")
        self._autosub_wakeup.set()
        return self._autosub_worker_task

    async def _autosub_worker_loop(self):
        """Drain the durable per-account queue; honor FloodWait and stop without losing jobs."""
        while not self._autosub_worker_stopping:
            if (not self.enabled or not self.cfg.get("broadcast_auto_subscribe", True)
                    or not self.running or not self.state.get("active")):
                return
            pause_until, pause_reason = self.autosub_queue.pause_info()
            now = time.time()
            if pause_until is not None and (pause_until < 0 or pause_until > now):
                wait_for = 15.0 if pause_until < 0 else min(30.0, max(1.0, pause_until - now))
                self._autosub_wakeup.clear()
                try:
                    await asyncio.wait_for(self._autosub_wakeup.wait(), timeout=wait_for)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                gap_seconds = min(3600.0, max(2.0, float(
                    self.cfg.get("broadcast_auto_subscribe_gap_seconds", 90))))
                daily_limit = max(1, int(
                    self.cfg.get("broadcast_auto_subscribe_daily_limit", 15)))
                batch_size = min(50, max(1, int(
                    self.cfg.get("broadcast_auto_subscribe_batch_size", 5))))
                batch_pause_minutes = min(1440, max(5, int(
                    self.cfg.get("broadcast_auto_subscribe_batch_pause_minutes", 30))))
            except (TypeError, ValueError):
                gap_seconds, daily_limit = 90.0, 15
                batch_size, batch_pause_minutes = 5, 30

            # The batch counter lives in the same durable SQLite queue as jobs.
            # A restart therefore cannot silently skip a scheduled rest.
            if self.autosub_queue.batch_action_count() >= batch_size:
                pause_until = now + batch_pause_minutes * 60
                self.autosub_queue.start_batch_pause(
                    pause_until,
                    f"плановая пауза после пакета из {batch_size} действий",
                )
                self._autosub_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._autosub_wakeup.wait(),
                        timeout=min(30.0, max(1.0, pause_until - time.time())),
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            rate_wait, rate_reason = self.autosub_queue.rate_limit_wait(
                gap_seconds=gap_seconds, daily_limit=daily_limit, now=now)
            if rate_wait > 0:
                self._autosub_wakeup.clear()
                try:
                    await asyncio.wait_for(self._autosub_wakeup.wait(), timeout=min(30.0, max(0.5, rate_wait)))
                except asyncio.TimeoutError:
                    pass
                continue

            job = self.autosub_queue.claim_next(now)
            if job is None:
                due = self.autosub_queue.next_due()
                timeout = 10.0 if due is None else min(10.0, max(0.5, due - time.time()))
                self._autosub_wakeup.clear()
                try:
                    await asyncio.wait_for(self._autosub_wakeup.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                outcome = await self._subscribe_one(job.get("payload") or {})
            except asyncio.CancelledError:
                # Queue DB will recover processing rows on next service start. Requeue
                # in-process too, while avoiding retry of side-effecting calls blindly.
                self.autosub_queue.finish(job["id"], "retry", result="interrupted",
                                          error="worker stopped; action will be reconciled before retry",
                                          next_attempt_at=time.time() + 5)
                raise
            except Exception as exc:  # noqa: BLE001
                outcome = {"status": "retry", "reason": type(exc).__name__, "delay": 60}

            status = str(outcome.get("status") or "needs_review")
            reason = str(outcome.get("reason") or "")
            attempts = int(job.get("attempts") or 1)
            if status in {"completed", "already_done", "failed_permanent", "unsupported", "needs_review"}:
                payload_kind = str((job.get("payload") or {}).get("type") or "")
                if status == "completed":
                    rate_kind = str(outcome.get("rate_kind") or
                                    ("join" if payload_kind in {"username", "invite_hash"} else "bot"))
                    # The original invite-request RPC was already counted when
                    # Telegram returned InviteRequestSent. Approval polling only
                    # reconciles membership; it is not a second join attempt.
                    if not outcome.get("action_already_counted"):
                        self.autosub_queue.record_action(rate_kind)
                self.autosub_queue.finish(job["id"], status, result=reason, error="")
                if status == "needs_review":
                    await self.notifier.alert("autosub-review", f"⚠️ Автоподписка требует ручной проверки: {reason}.", 1800)
                continue
            if status == "waiting_approval":
                # Count the initial join-request action once; later six-hour checks
                # only reconcile approval state and must not consume another slot.
                if attempts == 1:
                    self.autosub_queue.record_action("join")
                self.autosub_queue.finish(job["id"], "waiting_approval", result=reason,
                                          next_attempt_at=time.time() + 6 * 3600)
                continue
            if status == "account_paused":
                delay = outcome.get("delay")
                if delay:
                    until = time.time() + max(60, float(delay))
                    self.autosub_queue.finish(job["id"], "retry", result=reason,
                                              next_attempt_at=until)
                    self.autosub_queue.set_pause(until, reason)
                    await self.notifier.alert("autosub-join-limit",
                        "⚠️ Telegram сообщил об ограничении участия в каналах/группах. Очередь сохранена и поставлена на паузу.", 3600)
                else:
                    self.autosub_queue.finish(job["id"], "account_paused", result=reason, error=reason)
                    self.autosub_queue.set_pause(None, reason)
                    await self.notifier.alert("autosub-account-restricted",
                        "🛑 Telegram ограничил действия аккаунта при автоподписке. Очередь сохранена и остановлена. Проверь статус аккаунта; возобновление — вручную после проверки.", 0)
                continue
            if status == "retry":
                delay = max(5, float(outcome.get("delay") or min(3600, 60 * (2 ** max(0, attempts - 1)))))
                if not outcome.get("account_pause") and attempts >= 5:
                    self.autosub_queue.finish(job["id"], "needs_review", result=reason, error=reason)
                    await self.notifier.alert("autosub-review", f"⚠️ Не удалось обработать ссылку после {attempts} попыток: {reason}. Она сохранена для ручной проверки.", 1800)
                else:
                    next_at = time.time() + delay
                    self.autosub_queue.finish(job["id"], "retry", result=reason, error=reason,
                                              next_attempt_at=next_at)
                    if outcome.get("account_pause"):
                        self.autosub_queue.set_pause(next_at, reason)
                        await self.notifier.alert("autosub-flood",
                            f"⏳ Telegram установил FloodWait. Очередь сохранена; продолжу после {fmt_ts(self.cfg, next_at)}.", 1800)
                continue
            self.autosub_queue.finish(job["id"], "needs_review", result="unknown_status", error=reason)

    def resume_autosub_after_review(self) -> bool:
        """Owner-invoked resume after reviewing an indefinite account restriction.

        Timed FloodWait/join-capacity pauses are never cleared by this control; only
        Telegram's deadline can release them. An indefinite PeerFlood pause requires
        explicit owner acknowledgement after checking the account status.
        """
        pause_until, pause_reason = self.autosub_queue.pause_info()
        if pause_until is not None and pause_until >= 0 and pause_until > time.time():
            self._ensure_autosub_worker()
            return False
        if pause_until is not None and pause_until < 0:
            self.autosub_queue.clear_pause()
            self.autosub_queue.db.execute(
                "UPDATE autosub_jobs SET status='queued',next_attempt_at=0,updated_at=? WHERE status='account_paused'",
                (time.time(),))
            self.autosub_queue.db.commit()
        self._ensure_autosub_worker()
        return True

    def resume_autosub_manual_pause(self) -> bool:
        """Clear only an owner-requested pause; never clear Telegram restrictions."""
        pause_until, pause_reason = self.autosub_queue.pause_info()
        if pause_until is not None and pause_until < 0:
            if pause_reason != AUTOSUB_OWNER_PAUSE_REASON:
                return False
            self.autosub_queue.clear_pause()
        elif pause_until is not None:
            if pause_until > time.time():
                return False
            self.autosub_queue.clear_pause()
        self._ensure_autosub_worker()
        self._autosub_wakeup.set()
        return True

    async def _run_autosubscribe_batch(self, channels: list, chat_id=None, message_id=None):
        """Legacy entry point retained: enqueue durably instead of holding work in memory."""
        inserted, duplicates = self.autosub_queue.enqueue_many(channels, chat_id, message_id)
        if inserted:
            self._ensure_autosub_worker()

    def _remember_autosub_event(self, key: str) -> bool:
        """Deduplicate NewMessage/MessageEdited delivery per message content, not per chat."""
        now = time.time()
        # Prune old entries first; edits with changed URLs/text get a different key.
        expired = [k for k, ts in self._seen_autosub_events.items() if now - ts > 6 * 3600]
        for old_key in expired:
            self._seen_autosub_events.pop(old_key, None)
        if key in self._seen_autosub_events:
            return False
        self._seen_autosub_events[key] = now
        if len(self._seen_autosub_events) > self.AUTOSUB_EVENT_CACHE_SIZE:
            oldest = sorted(self._seen_autosub_events.items(), key=lambda item: item[1])
            for old_key, _ in oldest[:len(self._seen_autosub_events) - self.AUTOSUB_EVENT_CACHE_SIZE]:
                self._seen_autosub_events.pop(old_key, None)
        return True

    async def _message_mentions_this_account(self, event, message) -> bool:
        """Match Telegram's actual mention of this logged-in account.

        Telegram sets the `mentioned` flag for incoming mentions and includes a
        MessageEntityMentionName carrying the target user ID. Some bots render the
        nickname as a text URL (`tg://user?id=...` or `https://t.me/username`), so
        those representations are checked too. A reply to this account's own group
        message is also eligible even when Telegram does not set `mentioned`.
        """
        if bool(getattr(event, "mentioned", False)) or bool(getattr(message, "mentioned", False)):
            return True

        try:
            own_id = int(self.cfg.get("owner_id") or 0)
        except (TypeError, ValueError):
            own_id = 0

        # Telethon does not necessarily set `mentioned` on a reply. Resolve only
        # an actual reply target and accept it only when the original message was
        # authored by this logged-in account.
        reply_header = getattr(message, "reply_to", None)
        if reply_header is not None or getattr(message, "reply_to_msg_id", None):
            get_reply = getattr(event, "get_reply_message", None)
            if callable(get_reply):
                try:
                    replied = await get_reply()
                    if replied is not None:
                        replied_sender = getattr(replied, "sender_id", None)
                        if replied_sender is None:
                            from_id = getattr(replied, "from_id", None)
                            replied_sender = getattr(from_id, "user_id", None)
                        try:
                            replied_sender = int(replied_sender) if replied_sender is not None else 0
                        except (TypeError, ValueError):
                            replied_sender = 0
                        if own_id and replied_sender == own_id:
                            return True
                        if bool(getattr(replied, "out", False)) and (not own_id or not replied_sender or replied_sender == own_id):
                            return True
                except Exception as exc:  # noqa: BLE001
                    blog.debug("Не удалось проверить reply для автоподписки: %s", type(exc).__name__)

        own_username = str(self.cfg.get("account_username") or self.cfg.get("username") or "").lstrip("@")
        entities = list(getattr(message, "entities", None) or [])
        text = (getattr(message, "raw_text", None) or getattr(message, "message", None)
                or getattr(message, "text", None) or "")

        # First check ID-based mentions and profile hyperlinks. This is the exact
        # form used when a bot displays the account nickname as a clickable name.
        username_lookup_needed = False
        identity_lookup_needed = False
        for entity in entities:
            kind = type(entity).__name__
            if kind in {"MessageEntityMentionName", "InputMessageEntityMentionName"}:
                identity_lookup_needed = identity_lookup_needed or not own_id
                try:
                    if own_id and int(getattr(entity, "user_id", 0)) == own_id:
                        return True
                except (TypeError, ValueError):
                    pass
            elif kind == "MessageEntityTextUrl":
                target_url = str(getattr(entity, "url", "") or "").strip()
                if not target_url:
                    continue
                try:
                    parsed = urlsplit(target_url)
                    query = parse_qs(parsed.query)
                    action = (parsed.netloc or parsed.path.lstrip("/")).lower()
                    if parsed.scheme.lower() == "tg" and action == "user":
                        linked_id = int((query.get("id") or ["0"])[0])
                        if own_id and linked_id == own_id:
                            return True
                    elif parsed.scheme.lower() in {"http", "https"} and (parsed.hostname or "").lower() in {
                            "t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
                        username_lookup_needed = True
                        linked_name = next((part for part in parsed.path.split("/") if part), "").lstrip("@")
                        if own_username and linked_name.casefold() == own_username.casefold():
                            return True
                except (TypeError, ValueError):
                    continue
            elif kind == "MessageEntityMention":
                username_lookup_needed = True
                mentioned_text = self._slice_utf16(
                    text, getattr(entity, "offset", 0), getattr(entity, "length", 0)).strip().lstrip("@")
                if own_username and mentioned_text.casefold() == own_username.casefold():
                    return True

        # The account ID is normally populated from client.get_me() before the
        # Broadcaster is constructed. Resolve own username only for username-based
        # nickname links / @mentions, and cache it for subsequent updates.
        if (username_lookup_needed and not own_username) or identity_lookup_needed:
            try:
                me = await self.client.get_me()
                if not own_id:
                    try:
                        own_id = int(getattr(me, "id", 0) or 0)
                    except (TypeError, ValueError):
                        own_id = 0
                own_username = own_username or str(getattr(me, "username", None) or "").lstrip("@")
                if own_username:
                    self.cfg["account_username"] = own_username
            except Exception as exc:  # noqa: BLE001
                blog.debug("Не удалось получить username своего аккаунта для проверки mention: %s",
                           type(exc).__name__)
        for entity in entities:
            if type(entity).__name__ in {"MessageEntityMentionName", "InputMessageEntityMentionName"}:
                try:
                    if own_id and int(getattr(entity, "user_id", 0)) == own_id:
                        return True
                except (TypeError, ValueError):
                    pass
        if own_username:
            for entity in entities:
                if type(entity).__name__ == "MessageEntityMention":
                    value = self._slice_utf16(
                        text, getattr(entity, "offset", 0), getattr(entity, "length", 0)).strip().lstrip("@")
                    if value.casefold() == own_username.casefold():
                        return True
                elif type(entity).__name__ == "MessageEntityTextUrl":
                    target_url = str(getattr(entity, "url", "") or "").strip()
                    try:
                        parsed = urlsplit(target_url)
                        if (parsed.scheme.lower() in {"http", "https"}
                                and (parsed.hostname or "").lower() in {
                                    "t.me", "www.t.me", "telegram.me", "www.telegram.me"}):
                            linked_name = next((part for part in parsed.path.split("/") if part), "").lstrip("@")
                            if linked_name.casefold() == own_username.casefold():
                                return True
                    except ValueError:
                        pass
        return False

    async def _on_mention(self, event):
        try:
            if not self.enabled or not self.cfg.get("broadcast_auto_subscribe", True):
                return
            message = getattr(event, "message", None)
            if (message is None or bool(getattr(event, "is_private", False))
                    or not bool(getattr(event, "is_group", False))):
                return

            # Snapshot every supported link synchronously: URL buttons, visible
            # Telegram URLs and hidden text-URL entities. Do not await before copy.
            source_urls = self._message_urls(message)
            if not source_urls:
                return

            # Only the exact logged-in account mention is a trigger. This checks
            # Telegram's mentioned flag and ID-based clickable nickname entities.
            if not await self._message_mentions_this_account(event, message):
                return

            try:
                sender = getattr(event, "sender", None)
                if sender is None:
                    sender = await event.get_sender()
            except Exception:
                sender = await message.get_sender()
            if not getattr(sender, "bot", False):
                return

            channels = await self.extract_channels(message, urls=source_urls)
            if not channels:
                blog.info("Автоподписка: бот отметил аккаунт в чате %s, но сообщение не содержит поддерживаемых Telegram-ссылок",
                          getattr(event, "chat_id", None))
                return

            chat_id = getattr(event, "chat_id", None)
            message_id = getattr(message, "id", None) or getattr(event, "id", None)
            text = (getattr(message, "raw_text", None) or getattr(message, "message", None)
                    or getattr(message, "text", None) or "")
            event_payload = json.dumps(channels, ensure_ascii=False, sort_keys=True)
            signature = hashlib.sha256((str(chat_id) + ":" + str(message_id) + ":" +
                                        text + ":" + event_payload).encode("utf-8", errors="replace")).hexdigest()
            # Do not mark the event as seen before durable enqueue succeeds.
            # The SQLite action_key is the authoritative dedupe mechanism; the
            # in-memory event cache must never suppress a worker wake-up.
            try:
                max_per_message = min(100, max(1, int(
                    self.cfg.get("broadcast_auto_subscribe_max_per_message", self.MAX_SUBS_PER_MENTION))))
            except (TypeError, ValueError):
                max_per_message = self.MAX_SUBS_PER_MENTION
            accepted = channels[:max_per_message]
            omitted = max(0, len(channels) - len(accepted))
            blog.info("Автоподписка: бот %s отметил аккаунт в чате %s, сообщение %s — найдено ссылок: %s; валидных действий: %s; в очереди: %s",
                      getattr(sender, "username", None) or getattr(sender, "id", "?"),
                      chat_id, message_id, len(source_urls), len(channels), len(accepted))
            if omitted:
                await self.notifier.alert(
                    "autosub-overflow",
                    f"⚠️ В сообщении найдено {len(channels)} Telegram-ссылок. "
                    f"В очередь поставлены первые {len(accepted)} (лимит профиля); ещё {omitted} не добавлены. "
                    "Увеличь лимит ссылок на сообщение в настройках профиля, если это действительно нужно.", 900)

            # Persist every recognized action before acknowledging/deduplicating the event.
            # Invite hashes and referral payloads live only in a 0600 per-profile DB;
            # they are intentionally excluded from portable backups and logs.
            inserted, duplicates = self.autosub_queue.enqueue_many(accepted, chat_id, message_id)
            is_new_event = self._remember_autosub_event(signature)
            blog.info("Автоподписка: в постоянную очередь добавлено %s действий, дубликатов %s",
                      inserted, duplicates)
            # Always ensure/wake the worker after a successful DB enqueue call.
            # This also recovers a worker that exited while the queue already
            # contained the same durable action.
            if self.running and self.state.get("active"):
                self._ensure_autosub_worker()
                self._autosub_wakeup.set()
            if not is_new_event:
                blog.debug("Повторное событие упоминания: SQLite-дедупликация сохранена")
        except Exception:  # noqa: BLE001
            blog.exception("Ошибка в обработчике упоминаний")

    async def close(self):
        """Finish/stop background work before its Telegram client is disconnected."""
        self._autosub_worker_stopping = True
        if self._autosub_worker_task and not self._autosub_worker_task.done():
            self._autosub_worker_task.cancel()
            await asyncio.gather(self._autosub_worker_task, return_exceptions=True)
        tasks = list(self._subscribe_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subscribe_tasks.clear()
        # Wait for the broadcast worker before closing the SQLite Store it uses
        # for report()/blocked_until() calls. Otherwise shutdown races with the
        # final send_wave log and raises "Cannot operate on a closed database".
        broadcast_task = self.task
        if broadcast_task and not broadcast_task.done() and broadcast_task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(broadcast_task), timeout=15)
            except asyncio.TimeoutError:
                blog.warning("Рассылка не завершилась за 15 с при выключении; отменяю задачу с сохранённым checkpoint")
                broadcast_task.cancel()
                await asyncio.gather(broadcast_task, return_exceptions=True)
            except Exception:
                blog.exception("Ошибка при завершении задачи рассылки")
        try:
            self.skip.close()
        except Exception:
            pass
        try:
            self.autosub_queue.close()
        except Exception as exc:  # noqa: BLE001
            blog.warning("Не удалось корректно закрыть очередь автоподписки: %s", type(exc).__name__)

    # ----- отправка -----
    @staticmethod
    def _spam_bot_limited_status(text: str):
        """Return True=limited, False=explicitly clear, None=ambiguous response."""
        value = re.sub(r"\s+", " ", (text or "").casefold()).strip()
        if not value:
            return None
        clear_phrases = (
            "no limits are currently applied", "no limits are applied", "good news, no limits",
            "you're free as a bird", "you are free as a bird", "no restrictions are currently applied",
            "ограничений нет", "ограничения не применяются", "аккаунт не ограничен",
            "у вашего аккаунта нет ограничений", "ограничения отсутствуют",
        )
        if any(phrase in value for phrase in clear_phrases):
            return False
        limited_patterns = (
            r"(?:your )?account\s+(?:is|was|has been|is now|is currently)\s+(?:temporarily\s+)?(?:limited|restricted)",
            r"we have\s+(?:temporarily\s+)?limited your account",
            r"account\s+(?:will remain|remains)\s+(?:limited|restricted)",
            r"аккаунт\s+(?:сейчас\s+)?(?:ограничен|заблокирован)",
            r"ограничения\s+(?:на ваш аккаунт\s+)?(?:наложены|действуют)",
            r"вам\s+(?:временно\s+)?ограничили",
        )
        if any(re.search(pattern, value) for pattern in limited_patterns):
            return True
        # A bare word like “limited” may occur in a reassuring message (“not limited”).
        # Ambiguous output must not be treated as a confirmed account restriction.
        return None

    async def check_spam_block(self, alert=True) -> bool:
        try:
            sent = await self.client.send_message("SpamBot", "/start")
            msgs = []
            deadline = time.monotonic() + self.SPAM_WAIT
            while time.monotonic() < deadline:
                await asyncio.sleep(0.4)
                msgs = await self.client.get_messages("SpamBot", limit=5, min_id=sent.id)
                if any(not m.out for m in msgs):
                    break
            replies = [m.text or "" for m in msgs if not getattr(m, "out", False)]
            status = self._spam_bot_limited_status(" ".join(replies))
            if status is True:
                if alert:
                    await self.alert("spamblock", "⚠️ @SpamBot подтвердил ограничение аккаунта. "
                                     "Рассылка остановлена; проверь срок ограничения в @SpamBot.", 600)
                return False
            if status is None:
                blog.warning("Ответ @SpamBot не удалось однозначно распознать; проверка не блокирует запуск")
                if alert:
                    await self.alert("spamcheck-unknown", "ℹ️ Не удалось однозначно определить статус у @SpamBot. "
                                     "Перед продолжением проверь его ответ вручную.", 3600)
            else:
                blog.info("@SpamBot подтвердил отсутствие ограничений")
        except Exception as e:  # noqa: BLE001
            blog.warning("Не удалось проверить @SpamBot: %s", type(e).__name__)
            if alert:
                await self.alert("spamcheck-error", "ℹ️ Не удалось проверить статус через @SpamBot; "
                                 "Telegram всё равно будет сообщать фактические ограничения.", 3600)
        return True

    async def _sleep(self, sec) -> bool:
        """Interruptible sleep. True means stop was requested."""
        try:
            timeout = max(0.0, float(sec))
        except (TypeError, ValueError, OverflowError):
            timeout = 0.0
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return self.stop_event.is_set()

    async def _wait_account_flood(self) -> bool:
        """Honor a Telegram account-level FloodWait persisted before a process restart."""
        try:
            until = float(self.state.get("flood_until") or 0.0)
        except (TypeError, ValueError, OverflowError):
            until = 0.0
        if not math.isfinite(until) or until <= time.time():
            if self.state.get("flood_until"):
                self.state["flood_until"] = 0.0
                self._save_state()
            return False
        remaining = max(0.0, until - time.time())
        await self.say(f"⏳ Telegram установил паузу для этого аккаунта. Продолжение примерно через {fmt_dur(remaining)}.")
        if await self._sleep(remaining):
            return True
        self.state["flood_until"] = 0.0
        self._save_state()
        return False

    async def _wait_next_wave(self) -> bool:
        """Wait until the next wave, campaign end, stop, or a live schedule edit."""
        while True:
            if self.stop_event.is_set():
                return True
            self.schedule_changed.clear()
            now = time.time()
            next_at = float(self.state.get("next") or now)
            end_at = float(self.state.get("end") or now)
            deadline = min(next_at, end_at)
            timeout = max(0.0, deadline - now)
            if timeout <= 0:
                return False
            stop_task = asyncio.create_task(self.stop_event.wait())
            change_task = asyncio.create_task(self.schedule_changed.wait())
            try:
                await asyncio.wait({stop_task, change_task}, timeout=timeout,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (stop_task, change_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stop_task, change_task, return_exceptions=True)
            if self.stop_event.is_set():
                return True
            if self.schedule_changed.is_set():
                continue
            return False

    async def forward(self, msg, chat):
        """Send one permitted destination; classify account-wide and chat-local errors separately."""
        name = getattr(chat, "title", str(chat.id))
        for attempt in (1, 2):
            try:
                result = await self.client.forward_messages(chat, msg, from_peer="me", drop_author=True)
                # Treat delivery as confirmed only when Telegram returned a Message ID.
                messages = result if isinstance(result, (list, tuple)) else [result]
                confirmed = any(item is not None and getattr(item, "id", None) for item in messages)
                if not confirmed:
                    return False, "uncertain"
                self._consec_spam = 0
                return True, ""
            except errors.FloodWaitError as e:
                wait_seconds = max(0, int(getattr(e, "seconds", 0)))
                until = time.time() + wait_seconds + self.FLOOD_PAD
                self.state["flood_until"] = max(float(self.state.get("flood_until") or 0), until)
                if not self._save_state():
                    raise OSError("Не удалось сохранить обязательный FloodWait checkpoint")
                # Persist before waiting: a VPS restart must not forget FloodWait.
                if attempt == 1 and wait_seconds <= self.FLOOD_MAX_WAIT:
                    blog.warning("FloodWait %sс в '%s' - жду и повторяю", wait_seconds, name)
                    await self.notifier.alert("flood", f"⏳ Telegram попросил подождать {wait_seconds} с. Жду точный срок.", 1800)
                    if await self._sleep(wait_seconds + self.FLOOD_PAD):
                        return False, "stop"
                    self.state["flood_until"] = 0.0
                    self._save_state()
                    if self.state.get("end") and time.time() >= float(self.state["end"]):
                        return False, "stop"
                    continue
                self._abort = True
                self._flood_wait = max(self._flood_wait, wait_seconds)
                await self.alert("flood-long", f"⏳ Большой FloodWait: {fmt_dur(wait_seconds)}. "
                                 "Текущая волна остановлена; продолжение будет после указанного Telegram срока.", 1800)
                return False, "flood"
            except errors.SlowModeWaitError as e:
                seconds = max(0, int(getattr(e, "seconds", 0)))
                self._slow_until[int(chat.id)] = time.time() + seconds + 5
                if not self._save_slow_modes():
                    # Do not continue without persisting the server-provided cooldown.
                    raise OSError("Не удалось сохранить SlowMode checkpoint")
                blog.warning("SlowMode %sс в '%s' - пропускаю до окончания ограничения", seconds, name)
                return False, "slow"
            except self.ACCOUNT_RESTRICTION_ERRORS:
                self._abort = self._fatal = True
                self._fatal_reason = "account_restricted"
                blog.error("Telegram вернул PeerFlood для аккаунта; рассылка остановлена")
                return False, "account_restricted"
            except self.SOURCE_FORWARD_ERRORS:
                self._abort = self._fatal = True
                self._fatal_reason = "source_restricted"
                blog.error("Telegram запретил пересылку выбранного исходного сообщения")
                return False, "source_restricted"
            except self.NOPERM_ERRORS:
                await self.ban_chat(chat.id, name)
                blog.warning("Нет права отправки в '%s' - исключил только этот чат", name)
                return False, "noperm"
            except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as e:
                # The request may have reached Telegram even if the acknowledgement was lost.
                # Do not blindly retry an ambiguous send: that can create duplicate messages.
                blog.warning("Неизвестен результат отправки в '%s' после сетевой ошибки %s", name, type(e).__name__)
                return False, "uncertain"
            except Exception as e:  # noqa: BLE001
                v = bcore.classify(e)
                if v is not None and v.scope == "global" and v.kind == "flood":
                    wait_seconds = max(1, int(v.wait or 1))
                    self.state["flood_until"] = max(float(self.state.get("flood_until") or 0),
                                                       time.time() + wait_seconds + self.FLOOD_PAD)
                    if not self._save_state():
                        raise OSError("Не удалось сохранить FloodWait checkpoint")
                    self._abort = True
                    self._flood_wait = max(self._flood_wait, wait_seconds)
                    return False, "flood"
                if v is not None and v.scope == "account":
                    self._abort = self._fatal = True
                    self._fatal_reason = "account_restricted"
                    return False, "account_restricted"
                if v is not None and v.scope == "source":
                    self._abort = self._fatal = True
                    self._fatal_reason = "source_restricted"
                    return False, "source_restricted"
                if v is not None and v.scope == "chat" and v.ttl and v.kind in KIND_TO_REASON:
                    self.skip.touch(chat.id, name)
                    self.skip.block(chat.id, v, f"{type(e).__name__}: {e}")
                    blog.warning("Чат '%s' на паузу %s дн.: %s%s", name, round(v.ttl / 86400),
                                 v.kind, f" ({v.price}★)" if v.price else "")
                    return False, KIND_TO_REASON[v.kind]
                blog.error("Ошибка в '%s': %s", name, e)
                return False, "other"
        return False, "other"

    @staticmethod
    def _pending_result(pending: dict) -> dict:
        def safe_count(value):
            try:
                return max(0, int(value))
            except (TypeError, ValueError, OverflowError):
                return 0
        raw_errors = pending.get("errors", {})
        errors = Counter()
        if isinstance(raw_errors, dict):
            for key, value in raw_errors.items():
                count = safe_count(value)
                if count:
                    errors[str(key)] = count
        raw_failed = pending.get("failed", [])
        failed = []
        if isinstance(raw_failed, list):
            for item in raw_failed:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    failed.append((str(item[0])[:128], str(item[1])[:32]))
        return {
            "ok": safe_count(pending.get("ok", 0)),
            "fail": safe_count(pending.get("fail", 0)),
            "skipped": safe_count(pending.get("skipped", 0)),
            "errors": errors,
            "failed": failed[-50:],
        }

    def _save_pending_progress(self, pending: dict, result: dict, completed: set[int]) -> None:
        pending["completed_ids"] = sorted(completed)
        pending["ok"] = result["ok"]
        pending["fail"] = result["fail"]
        pending["errors"] = dict(result["errors"])
        pending["failed"] = result["failed"][-50:]
        pending["skipped"] = int(result.get("skipped", pending.get("skipped", 0)))
        self.state["pending_wave"] = pending
        if not self._save_state():
            # Stop on the first checkpoint failure. Continuing would create a large
            # duplicate window after a VPS crash because later successes would not persist.
            raise OSError("Не удалось сохранить checkpoint очереди рассылки")

    async def _restore_pending_chats(self, pending: dict) -> list:
        """Resolve only the still-pending chats; removed/excluded chats are checkpointed as skipped."""
        groups = await self._scan_dialogs()
        by_id = {int(entity.id): entity for entity in groups}
        completed = self._int_ids(pending.get("completed_ids", []))
        excluded = self._int_ids(self.cfg.get("broadcast_exclude", []))
        ready = []
        result = self._pending_result(pending)
        skipped = result["skipped"]
        raw_chat_ids = pending.get("chat_ids", [])
        if not isinstance(raw_chat_ids, list):
            raw_chat_ids = []
        for raw_id in raw_chat_ids:
            try:
                cid = int(raw_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if cid in completed:
                continue
            if cid in excluded or cid in self.banned:
                completed.add(cid)
                skipped += 1
                continue
            entity = by_id.get(cid)
            if entity is None:
                completed.add(cid)
                result["fail"] += 1
                result["errors"]["other"] += 1
                result["failed"].append((str(cid), "other"))
                continue
            if self.skip.blocked_until(cid) is not None or self._slow_until.get(cid, 0) > time.time():
                completed.add(cid)
                skipped += 1
                continue
            ready.append(entity)
        pending["skipped"] = skipped
        result["skipped"] = skipped
        self._save_pending_progress(pending, result, completed)
        return ready

    async def send_wave(self, msg, chats, pending: dict | None = None) -> dict:
        r = self._pending_result(pending or {})
        completed = self._int_ids((pending or {}).get("completed_ids", []))
        self._apply_delays()
        self._abort = self._fatal = False
        self._fatal_reason = ""
        self._flood_wait = 0
        self._consec_spam = 0
        total_remaining = len(chats)
        excluded_source = None
        excluded_signature = None
        excluded = set()
        for i, chat in enumerate(chats, 1):
            chat_id = int(chat.id)
            if chat_id in completed:
                continue
            if self.stop_event.is_set() or self._abort:
                break
            if self.state.get("end") and time.time() >= float(self.state["end"]):
                blog.info("Достигнут конец периода рассылки; завершаю текущую волну без новых отправок")
                break
            # Respect live changes from the bot panel: if the owner excludes a
            # destination during a wave, do not send to it later in that same wave.
            raw_excluded = self.cfg.get("broadcast_exclude", [])
            if not isinstance(raw_excluded, (list, tuple, set)):
                raw_excluded = []
            signature = tuple(sorted(self._int_ids(raw_excluded)))
            if signature != excluded_signature:
                excluded_source = raw_excluded
                excluded_signature = signature
                excluded = set(signature)
            if (chat_id in self.banned or chat_id in excluded
                    or self.skip.blocked_until(chat_id) is not None
                    or self._slow_until.get(chat_id, 0) > time.time()):
                r["skipped"] += 1
                completed.add(chat_id)
                if pending is not None:
                    self._save_pending_progress(pending, r, completed)
                continue
            self.progress = (i, total_remaining)
            name = getattr(chat, "title", str(chat.id))
            ok, reason = await self.forward(msg, chat)
            if reason == "stop":
                break
            if reason in {"flood", "account_restricted"}:
                # Account-wide conditions must not mark the current chat complete.
                # Keep it and all following destinations in the durable checkpoint.
                break
            if reason == "source_restricted":
                # The source post itself cannot be forwarded; close as partial so
                # the owner can select another post rather than retrying this source.
                break
            if ok:
                r["ok"] += 1
            else:
                r["fail"] += 1
                r["errors"][reason] += 1
                r["failed"].append((name, reason))
            # Persist after each confirmed attempt. On restart, only remaining chats
            # will be retried; a crash between Telegram's ack and this write can still
            # duplicate at most the single in-flight chat (Telegram has no idempotency key).
            if pending is not None:
                completed.add(int(chat.id))
                self._save_pending_progress(pending, r, completed)
            if not ok and reason in FAST_FAIL:
                await asyncio.sleep(self.FAIL_DELAY)
                continue
            if await self._sleep(random.uniform(self.DELAY_MIN, self.DELAY_MAX)):
                break
            if random.random() < self.LONG_PAUSE_CHANCE:
                if await self._sleep(random.randint(self.LONG_PAUSE_MIN, self.LONG_PAUSE_MAX)):
                    break
        self.progress = (0, 0)
        blog.info("Чёрный список: %s", self.skip.report())
        return r

    def _record_wave(self, r, seconds, partial=False, partial_reason=""):
        st = self.state
        st["wave"] += 1
        st["run_ok"] += r["ok"]
        st["run_fail"] += r["fail"]
        st["life_ok"] += r["ok"]
        st["life_fail"] += r["fail"]
        st["life_waves"] += 1
        self.total_sent += r["ok"]
        st["history"].append({"n": st["wave"], "t": time.time(), "ok": r["ok"], "fail": r["fail"],
                              "skipped": int(r.get("skipped", 0)),
                              "sec": round(seconds), "err": dict(r["errors"]),
                              "partial": bool(partial), "partial_reason": str(partial_reason or "")[:40]})
        st["history"] = st["history"][-self.HISTORY_KEEP:]
        st["pending_wave"] = None
        st["last_error"] = ""
        if not self._save_state():
            raise OSError("Не удалось сохранить итог завершённой волны")

    def wave_report(self, r, seconds) -> str:
        st = self.state
        latest = st["history"][-1] if st.get("history") else {}
        partial_label = " · ⚠️ НЕПОЛНАЯ ВОЛНА" if latest.get("partial") else ""
        text = (f"📊 Волна #{st['wave']}: ✅ {r['ok']} / ❌ {r['fail']} за {fmt_dur(seconds)}{partial_label}\n"
                f"За этот запуск: ✅ {st['run_ok']} / ❌ {st['run_fail']}")
        skipped = int(r.get("skipped", 0))
        if skipped:
            text += f"\nПропущено чатов: {skipped}"
        if r["errors"]:
            text += "\nОшибки: " + ", ".join(f"{REASON_LABELS.get(k, k)} - {v}" for k, v in r["errors"].items())
            names = [n for n, _ in r["failed"][:5]]
            text += "\nЧаты: " + ", ".join(names) + (" и др." if len(r["failed"]) > 5 else "")
        return text

    # ----- главный цикл -----
    async def _job(self):
        st = self.state
        resumed, self._resumed = self._resumed, False
        reason = "done"
        try:
            # A pending wave owns the exact Saved Messages post ID that was
            # checkpointed when the wave began. Do not let a later UI selection
            # (or a deleted current selection) replace/block that recovery post.
            pending_at_start = st.get("pending_wave")
            if isinstance(pending_at_start, dict):
                try:
                    recovery_post_id = int(pending_at_start.get("post_id") or 0)
                except (TypeError, ValueError, OverflowError):
                    recovery_post_id = 0
                msg = (await self.client.get_messages("me", ids=recovery_post_id)
                       if recovery_post_id > 0 else None)
                if msg is None:
                    await self.alert("pending-post", "⚠️ Пост незавершённой волны больше недоступен в «Избранном». "
                                     "Очередь не будет заменена другим постом: выбери новый пост и создай новую кампанию.", 0)
            else:
                msg = await self.get_post()
            if msg is None:
                reason = "nopost"
            elif not await self.check_spam_block():
                reason = "spam"
            else:
                if resumed:
                    await self.say("▶️ Рассылка продолжена после перезапуска сервиса.")
                    if st["next"] > time.time() and await self._wait_next_wave():
                        reason = "stopped"
                else:
                    await self.say(f"🚀 Рассылка запущена: до {fmt_ts(self.cfg, st['end'], '%d.%m %H:%M')}, "
                                   f"интервал {st['interval']} мин.")
                while reason == "done" and time.time() < st["end"] and not self.stop_event.is_set():
                    if await self._wait_account_flood():
                        # Manual Stop closes a partial wave and records work already
                        # completed; service shutdown (suspending=True) preserves the
                        # checkpoint intact for automatic resume after restart.
                        pending_on_stop = st.get("pending_wave")
                        if not self.suspending and isinstance(pending_on_stop, dict):
                            partial_result = self._pending_result(pending_on_stop)
                            partial_seconds = max(0.0, time.time() - float(pending_on_stop.get("started") or time.time()))
                            self._record_wave(partial_result, partial_seconds, partial=True,
                                              partial_reason="Остановлена пользователем во время FloodWait")
                            if self.cfg.get("broadcast_notify_waves", True):
                                await self.notifier.send(self.wave_report(partial_result, partial_seconds))
                        reason = "stopped"
                        break
                    pending = st.get("pending_wave")
                    if pending:
                        pending_post_id = int(pending.get("post_id") or 0)
                        msg = await self.client.get_messages("me", ids=pending_post_id) if pending_post_id else None
                        if not msg:
                            await self.alert("pending-post", "⚠️ Не удалось восстановить пост незавершённой волны. "
                                             "Выбери новый пост и запусти новую рассылку.", 0)
                            reason = "nopost"
                            break
                        chats = await self._restore_pending_chats(pending)
                        t0 = float(pending.get("started") or time.time())
                        blog.info("Восстанавливаю незавершённую волну: осталось чатов %s из %s",
                                  len(chats), len(pending.get("chat_ids", [])))
                    else:
                        chats = await self.load_dialogs()
                        if not chats:
                            await self.alert("nochats", "❌ Нет чатов для рассылки (все исключены или нет групп).", 0)
                            reason = "nochats"
                            break
                        msg = await self.get_post()
                        if msg is None:
                            reason = "nopost"
                            break
                        random.shuffle(chats)
                        st["next"] = 0.0
                        pending = {
                            "post_id": int(msg.id),
                            "chat_ids": [int(chat.id) for chat in chats],
                            "completed_ids": [],
                            "started": time.time(),
                            "ok": 0,
                            "fail": 0,
                            "errors": {},
                            "failed": [],
                            "skipped": 0,
                        }
                        st["pending_wave"] = pending
                        if not self._save_state():
                            raise OSError("Не удалось сохранить начальный checkpoint очереди; отправка не начата")
                        t0 = float(pending["started"])
                    r = await self.send_wave(msg, chats, pending=pending)
                    dur = max(0.0, time.time() - t0)
                    if self.suspending:
                        # Leave pending_wave intact; system restart will continue remaining chats.
                        reason = "stopped"
                        break
                    if self._flood_wait:
                        # A long account-level FloodWait is not a completed wave.
                        # Preserve the checkpoint and resume the same remaining queue
                        # after Telegram's deadline, as long as the campaign has time.
                        wait_until = max(float(st.get("flood_until") or 0.0),
                                         time.time() + float(self._flood_wait) + self.FLOOD_PAD)
                        st["next"] = wait_until
                        if not self._save_state():
                            raise OSError("Не удалось сохранить расписание FloodWait")
                        if wait_until >= float(st.get("end") or 0.0):
                            self._record_wave(r, dur, partial=True, partial_reason="FloodWait до конца кампании")
                            if self.cfg.get("broadcast_notify_waves", True):
                                await self.notifier.send(self.wave_report(r, dur))
                            await self.alert("flood-campaign-ended", "⏹ Время кампании закончилось раньше, чем истёк FloodWait. "
                                             "Незавершённая волна отмечена как частичная; новых отправок не будет.", 0)
                            reason = "flood_expired"
                            break
                        await self.say(f"⏳ Очередь сохранена. Продолжу с оставшихся чатов после паузы Telegram: {fmt_dur(wait_until - time.time())}.")
                        if await self._wait_next_wave():
                            if not self.suspending and isinstance(pending, dict):
                                self._record_wave(r, dur, partial=True,
                                                  partial_reason="Остановлена пользователем во время FloodWait")
                                if self.cfg.get("broadcast_notify_waves", True):
                                    await self.notifier.send(self.wave_report(r, dur))
                            reason = "stopped"
                            break
                        # The owner may shorten campaign duration while the Telegram
                        # FloodWait is running. If the end time is reached, close the
                        # wave as partial and keep truthful counters in history.
                        if time.time() >= float(st.get("end") or 0.0):
                            dur = max(0.0, time.time() - t0)
                            self._record_wave(r, dur, partial=True, partial_reason="FloodWait до конца кампании")
                            if self.cfg.get("broadcast_notify_waves", True):
                                await self.notifier.send(self.wave_report(r, dur))
                            await self.alert("flood-campaign-ended", "⏹ Время кампании закончилось во время FloodWait. "
                                             "Незавершённая волна отмечена как частичная; новых отправок не будет.", 0)
                            reason = "flood_expired"
                            break
                        continue
                    pending_chat_ids = self._int_ids(pending.get("chat_ids", [])) if isinstance(pending, dict) else set()
                    pending_completed = self._int_ids(pending.get("completed_ids", [])) if isinstance(pending, dict) else set()
                    has_remaining = bool(pending_chat_ids - pending_completed)
                    await self.save_subs(force=True)
                    if self._fatal:
                        if self._fatal_reason == "source_restricted":
                            # The selected source cannot be forwarded. Close this wave as
                            # partial so the owner can choose a different Saved Messages post.
                            self._record_wave(r, dur, partial=True, partial_reason="Исходное сообщение запрещено к пересылке")
                            if self.cfg.get("broadcast_notify_waves", True):
                                await self.notifier.send(self.wave_report(r, dur))
                            await self.alert("source-forward-stop", "🛑 Telegram запретил пересылать выбранный пост "
                                             "(возможно, исходное сообщение защищено от пересылки). Выбери другой пост.", 0)
                            reason = "source_restricted"
                        else:
                            # Account-level restrictions do not invalidate the remaining
                            # destinations. Preserve the exact post + unfinished chat IDs.
                            st["active"] = bool(float(st.get("end") or 0) > time.time())
                            st["next"] = time.time() + 30
                            st["last_error"] = "account_restricted: Telegram PeerFlood; manual review required"
                            if not self._save_state():
                                raise OSError("Не удалось сохранить очередь после ограничения аккаунта")
                            await self.alert("account-restricted", "🛑 Telegram вернул PeerFlood: это ограничение "
                                             "аккаунта, а не отдельной группы. Незавершённая очередь сохранена. "
                                             "Проверь @SpamBot и возобновляй только после снятия ограничения.", 0)
                            reason = "account_restricted"
                        break
                    partial = has_remaining
                    partial_reason = "Остановка/конец кампании" if has_remaining else ""
                    self._record_wave(r, dur, partial=partial, partial_reason=partial_reason)
                    if self.cfg.get("broadcast_notify_waves", True):
                        await self.notifier.send(self.wave_report(r, dur))
                    if self.stop_event.is_set():
                        reason = "stopped"
                        break
                    flood_remaining = max(0.0, float(st.get("flood_until") or 0.0) - time.time())
                    wait = max(st["interval"] * 60, flood_remaining)
                    if time.time() + wait >= st["end"]:
                        break
                    st["next"] = time.time() + wait
                    if not self._save_state():
                        raise OSError("Не удалось сохранить время следующей волны")
                    if await self._wait_next_wave():
                        reason = "stopped"
                        break
                if reason == "done" and self.stop_event.is_set():
                    reason = "stopped"
            if not self.suspending:
                head = {"done": "🏁 Рассылка завершена", "stopped": "⏹ Рассылка остановлена",
                        "flood_expired": "⏹ Кампания завершилась во время ограничения Telegram"}.get(
                    reason, "⏹ Рассылка остановлена из-за ошибки (см. выше)")
                await self.say(f"{head}.\nЗа запуск: ✅ {st['run_ok']} / ❌ {st['run_fail']}, волн: {st['wave']}.")
        except asyncio.CancelledError:
            self.suspending = True
            raise
        except Exception as e:  # noqa: BLE001
            reason = "error"
            blog.exception("Сбой рассылки")
            # Keep the campaign and its per-chat checkpoint recoverable while its
            # configured end time is still in the future. A manual Start or service
            # restart will resume it instead of silently discarding the queue.
            if float(st.get("end") or 0) > time.time():
                st["active"] = True
                st["next"] = max(float(st.get("next") or 0), time.time() + 30)
                st["last_error"] = f"{type(e).__name__}: {e}"[:240]
            try:
                await self.alert("job-crash", f"❌ Сбой рассылки: {type(e).__name__}: {e}. "
                                 "Состояние очереди сохранено; после устранения причины нажми «Запустить» для продолжения.", 0)
            except Exception:
                blog.exception("Не удалось отправить уведомление о сбое рассылки")
        finally:
            await self.save_subs(force=True)
            self.running = False
            # Stop link-action execution as soon as a campaign ends. Queue rows
            # remain durable and are resumed only by the next explicit campaign.
            self._autosub_wakeup.set()
            worker = self._autosub_worker_task
            if worker and not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            self._autosub_worker_task = None
            if not self.suspending:
                terminal_detail = self.terminal_reason_message(reason)
                if terminal_detail:
                    st["last_error"] = terminal_detail[:240]
                elif reason in {"done", "stopped"}:
                    st["last_error"] = ""
                recoverable_error = reason == "error" and st.get("active") is True and float(st.get("end") or 0) > time.time()
                recoverable_account_pause = (reason in {"account_restricted", "spam"}
                                             and isinstance(st.get("pending_wave"), dict)
                                             and float(st.get("end") or 0) > time.time())
                if recoverable_error or recoverable_account_pause:
                    st["active"] = True
                    if float(st.get("next") or 0) <= time.time():
                        st["next"] = time.time() + 30
                    # Keep pending_wave and active marker for explicit resume after review.
                else:
                    st["active"] = False
                    st["next"] = 0.0
                    if self.stop_event.is_set() or reason not in {"done", "stopped"}:
                        st["pending_wave"] = None
            self._save_state()

    # ----- управление -----
    def _launch(self):
        self.running = True
        self.suspending = False
        self.stop_event.clear()
        self.task = asyncio.create_task(self._job(), name=f"broadcast:{self.ddir.name}")
        if self.cfg.get("broadcast_auto_subscribe", True):
            self._ensure_autosub_worker()

    def start(self, interval_min, hours) -> bool:
        if self.running:
            return False
        if self.state.get("active") and float(self.state.get("end") or 0) > time.time():
            # If a previous task stopped on an unexpected exception, Start means
            # resume the persisted campaign. To create a fresh campaign, stop first.
            return self.resume()
        now = time.time()
        # Preserve post_id: the selected message belongs to this account's Saved Messages
        # and must remain selected across stop/start and VPS restarts.
        self.state.update(active=True, end=now + float(hours) * 3600, interval=max(1, int(interval_min)),
                          next=0.0, wave=0, started=now, run_ok=0, run_fail=0, pending_wave=None,
                          last_error="")
        self.schedule_changed.clear()
        if not self._save_state():
            self.state["active"] = False
            return False
        self._launch()
        return True

    def resume(self) -> bool:
        """После перезапуска сервиса: если рассылка шла и время ещё не вышло - продолжаем."""
        st = self.state
        if self.running or not st.get("active"):
            return False
        if st["end"] <= time.time():
            st["active"] = False
            self._save_state()
            return False
        self._resumed = True
        self._launch()
        return True

    def stop(self):
        self.suspending = False
        self.stop_event.set()
        self._autosub_wakeup.set()
        worker = self._autosub_worker_task
        if worker and not worker.done():
            worker.cancel()

    def suspend(self):
        """Остановить кампанию при выключении сервиса, сохранив checkpoint и очередь."""
        self.suspending = True
        self.stop_event.set()
        self._autosub_wakeup.set()
        worker = self._autosub_worker_task
        if worker and not worker.done():
            worker.cancel()

    def set_interval(self, minutes):
        old_interval = max(1, int(self.state.get("interval", 60)))
        new_interval = max(1, int(minutes))
        self.state["interval"] = new_interval
        if self.running and self.state.get("next"):
            history = self.state.get("history") or []
            last = history[-1] if history and isinstance(history[-1], dict) else {}
            try:
                base_time = float(last.get("t", 0)) or (float(self.state["next"]) - old_interval * 60)
            except (TypeError, ValueError, OverflowError):
                base_time = float(self.state["next"]) - old_interval * 60
            self.state["next"] = max(time.time(), base_time + new_interval * 60)
            self.schedule_changed.set()
        self._save_state()

    def set_duration(self, hours):
        """Apply a new duration from the moment of changing it and wake the scheduler."""
        duration = max(0.1, float(hours))
        if self.running:
            self.state["end"] = time.time() + duration * 3600
            self.schedule_changed.set()
            self._save_state()

    def short(self) -> str:
        if not self.running:
            return "⏹ выключена"
        return "💤 пауза между волнами" if self.state["next"] else "📤 идёт"

    @staticmethod
    def terminal_reason_message(reason: str) -> str:
        """Explain why a campaign task stopped before/while starting; diagnostics only."""
        return {
            "nopost": "Не найден доступный пост в «Избранном» этого Telegram-аккаунта. Выбери существующее сообщение.",
            "nochats": "После проверки в выбранном аккаунте не осталось доступных групп. Обнови и сохрани список получателей.",
            "spam": "Telegram сообщил об ограничении аккаунта. Проверь уведомления Telegram и статус аккаунта; автоматический обход ограничения не выполняется.",
            "account_restricted": "Telegram ограничил действия этого аккаунта. Проверь уведомления Telegram и дождись снятия ограничения.",
            "source_restricted": "Telegram запретил использовать выбранное исходное сообщение. Выбери другое сообщение из «Избранного».",
            "flood_expired": "Ограничение Telegram действует дольше оставшегося времени кампании. Проверь статус аккаунта перед новым запуском.",
        }.get(str(reason or ""), "")

    def summary(self) -> str:
        st = self.state
        if not self.running:
            if st.get("active") and float(st.get("end") or 0) > time.time():
                s = "⚠️ ожидает продолжения после сбоя/перезапуска"
                if st.get("last_error"):
                    s += f"\nПоследняя ошибка: {str(st['last_error'])[:160]}"
            else:
                s = "⏹ не запущена"
            if st["life_waves"]:
                s += (f"\nПоследний запуск: ✅ {st['run_ok']} / ❌ {st['run_fail']}, волн: {st['wave']}")
            s += f"\nЗа всё время: ✅ {st['life_ok']} / ❌ {st['life_fail']}, волн: {st['life_waves']}"
            return s
        lines = []
        if st["next"]:
            lines.append(f"💤 Пауза до {fmt_ts(self.cfg, st['next'], '%H:%M')} (волн сделано: {st['wave']})")
        else:
            i, n = self.progress
            lines.append(f"📤 Идёт волна #{st['wave'] + 1}" + (f", чат {i} из {n}" if n else ""))
        lines.append(f"Осталось: {fmt_dur(st['end'] - time.time())} · интервал {st['interval']} мин")
        lines.append(f"За запуск: ✅ {st['run_ok']} / ❌ {st['run_fail']}")
        lines.append(f"За всё время: ✅ {st['life_ok']} / ❌ {st['life_fail']}")
        return "\n".join(lines)


# =============================================================================
#  Диагностика (используется и ботом, и консольной проверкой)
# =============================================================================
async def check_rights(client, target) -> tuple[bool, str]:
    try:
        me = await client.get_me()
        p = await client.get_permissions(target, me)
        rights = getattr(getattr(p, "participant", None), "admin_rights", None)
        ok = bool(getattr(p, "is_creator", False)
                  or (getattr(p, "is_admin", False) and getattr(rights, "manage_topics", False)))
        return ok, ("Права на создание тем есть" if ok else
                    "Нет права «Управление темами» - сделайте себя админом с этим правом")
    except Exception as e:  # noqa: BLE001
        return False, f"Не удалось проверить права: {e}"


async def diagnose(client, arch, bc, cfg, ddir) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    out.append((bool(client.is_connected()), "Соединение с Telegram"))
    try:
        authorized = await client.is_user_authorized()
    except Exception as e:  # noqa: BLE001
        authorized = False
        out.append((False, f"Не удалось проверить сессию: {e}"))
    out.append((authorized, "Сессия аккаунта действует" if authorized else
                "Сессия слетела - открой в боте раздел «Аккаунты» и пройди авторизацию заново"))
    if authorized:
        err = await arch.setup_target()
        if err:
            out.append((False, err))
        else:
            out.append((True, f"Группа архива: {getattr(arch.target, 'title', '?')}, темы включены"))
            out.append(await check_rights(client, arch.target))
        spam_ok, post = await asyncio.gather(bc.check_spam_block(alert=False), bc.preview())
        out.append((spam_ok, "Спам-блок не обнаружен"))
        out.append((bool(post), "Пост для рассылки есть в Избранном" if post else
                    "В Избранном нет поста для рассылки"))
    try:
        du = shutil.disk_usage(ddir)
        pct = du.free / du.total * 100
        out.append((pct >= cfg.get("low_disk_percent", 10),
                    f"Свободно на диске: {human(du.free)} ({pct:.0f}%)"))
    except OSError:
        pass
    return out


async def check_health(client, ddir, cfg, notifier) -> list[str]:
    """Один проход проверки здоровья. Шлёт предупреждения и возвращает список проблем."""
    problems = []
    try:
        if not client.is_connected():
            problems.append("disconnected")
            await notifier.alert("disconnected", "⚠️ Нет соединения с Telegram, пробую переподключиться.", 3600)
        elif not await client.is_user_authorized():
            problems.append("auth")
            await notifier.alert("auth", "❌ Сессия аккаунта слетела. Открой в боте раздел «Аккаунты» и пройди авторизацию заново.", 21600)
        du = shutil.disk_usage(ddir)
        pct = du.free / du.total * 100
        if pct < cfg.get("low_disk_percent", 10):
            problems.append("disk")
            await notifier.alert("disk", f"💾 Мало места: свободно {human(du.free)} ({pct:.0f}%). "
                                 "Открой «📊 Статус» → «🧹 Удалить копии» или выключи хранение копий в настройках.",
                                 21600)
    except Exception:  # noqa: BLE001
        log.exception("Сбой проверки здоровья")
    return problems


# =============================================================================
#  Управление Telegram-аккаунтами из веб-панели
# =============================================================================
class AccountManager:
    """Owns all Telegram account sessions under the independent web control plane.

    The web host owns no Telegram session. Every profile, including default,
    is started as a separately managed task with its own session lock.
    """
    def __init__(self, owner_id: int):
        self.owner_id = int(owner_id)
        self.tasks: dict[str, asyncio.Task] = {}
        self.statuses: dict[str, str] = {}
        self.configs: dict[str, dict] = {}
        self.broadcasters: dict[str, Broadcaster] = {}
        self.archivers: dict[str, Archiver] = {}
        self.locks: dict[str, object] = {}
        self.auth_flows: dict[str, dict] = {}
        self.auth_timeout_tasks: dict[str, asyncio.Task] = {}
        self._logging_profiles: set[str] = set()
        self.notify_fn = None  # optional notifier hook; web control does not require a Telegram bot

    def set_notifier_send_fn(self, send_fn) -> None:
        self.notify_fn = send_fn
        for broadcaster in self.broadcasters.values():
            broadcaster.notifier.send_fn = send_fn

    @staticmethod
    def names() -> list[str]:
        return [name for name, _ in account_profiles() if name != "default"]

    @staticmethod
    def _profile_config_path(name: str) -> Path:
        if name == "default":
            return DEFAULT_CONFIG_PATH
        return profile_config_path(name)

    @staticmethod
    def _session_path(cfg: dict, ddir: Path) -> Path:
        return ddir / (str(cfg.get("session_name") or "circles") + ".session")

    def has_session(self, name: str) -> bool:
        try:
            cfg = read_profile_config(self._profile_config_path(name))
            return self._session_path(cfg, data_dir(cfg)).exists()
        except (OSError, ValueError, FileNotFoundError):
            return False

    def is_running(self, name: str) -> bool:
        task = self.tasks.get(name)
        return bool(task and not task.done())

    def status(self, name: str) -> str:
        if self.is_running(name):
            return "Работает · основной аккаунт" if name == "default" else "Работает"
        if name in self.auth_flows:
            flow = self.auth_flows[name]
            if time.monotonic() > flow.get("expires", 0):
                try:
                    asyncio.get_running_loop().create_task(self._discard_auth(name))
                except RuntimeError:
                    pass
                return "Срок авторизации истёк"
            stage = flow.get("stage")
            return {"code": "Ожидает код Telegram", "password": "Ожидает пароль 2FA"}.get(stage, "Авторизация")
        state = self.statuses.get(name)
        if state:
            return state
        return "Сессия сохранена · не запущен" if self.has_session(name) else "Не авторизован"

    def _setup_profile_logging(self, name: str, ddir: Path) -> None:
        if name in self._logging_profiles:
            return
        # Existing handlers belong to the primary account. Route each profile's
        # records to its own files using the same ContextVar used by run_all_accounts.
        for logger in (log, blog):
            for handler in logger.handlers:
                if isinstance(handler, ErrorRing):
                    continue
                if not any(isinstance(f, ProfileLogFilter) and f.profile_name == "default"
                           for f in handler.filters):
                    handler.addFilter(ProfileLogFilter("default"))
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        for logger, filename, formatter in (
            (log, "circles.log", fmt),
            (blog, "broadcast.log", logging.Formatter("%(asctime)s %(message)s")),
        ):
            handler = RotatingFileHandler(ddir / filename, maxBytes=5 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(formatter)
            handler.addFilter(ProfileLogFilter(name))
            logger.addHandler(handler)
        self._logging_profiles.add(name)

    def add_profile(self, name: str) -> Path:
        return add_account_profile(name)

    async def start_existing(self) -> list[str]:
        started = []
        for name in self.names():
            if self.has_session(name):
                ok, message = await self.start_profile(name)
                if ok:
                    started.append(name)
                else:
                    log.warning("Профиль %s не запущен: %s", name, message)
        return started

    async def start_profile(self, name: str) -> tuple[bool, str]:
        try:
            path = self._profile_config_path(name)
        except ValueError as e:
            return False, str(e)
        if not path.is_file():
            return False, "Профиль не найден. Сначала добавь аккаунт."
        if self.is_running(name):
            return True, "Аккаунт уже работает."
        if name in self.auth_flows:
            return False, "Сначала заверши авторизацию в веб-панели."
        cfg = read_profile_config(path)
        # Starting a profile must not silently change the owner's broadcast policy.
        # The current saved allowlist/exclusions and auto-subscribe choice are respected.
        problems = [p for p in cfg_problems(cfg) if p != "не задан ID группы-форума"]
        if problems:
            self.statuses[name] = "Требуется настройка: " + "; ".join(problems)
            return False, "Не хватает настроек: " + "; ".join(problems)
        ddir = data_dir(cfg)
        if not self._session_path(cfg, ddir).exists():
            return False, "Сначала авторизуй аккаунт через кнопку «🔐 Авторизовать»."
        self._setup_profile_logging(name, ddir)
        self.statuses[name] = "Запускается"
        task = asyncio.create_task(self._run_profile(name, cfg, ddir), name=f"account:{name}")
        self.tasks[name] = task
        return True, "Аккаунт запускается. Через несколько секунд обнови карточку."

    async def _run_profile(self, name: str, cfg: dict, ddir: Path) -> None:
        profile_token = ACTIVE_PROFILE.set(name)
        lock = None
        self.configs[name] = cfg
        try:
            lock = acquire_lock(ddir)
            if lock is None:
                self.statuses[name] = "Уже запущен в другом процессе"
                return
            self.locks[name] = lock
            self.statuses[name] = "Работает"
            def register_broadcaster(instance):
                self.broadcasters[name] = instance
                if self.notify_fn:
                    instance.notifier.send_fn = self.notify_fn

            def register_archiver(instance):
                self.archivers[name] = instance
                if self.notify_fn:
                    instance.notifier.send_fn = self.notify_fn

            code = await run_service(cfg, ddir, allow_broadcast=True,
                                     handle_signals=False, enable_account_manager=False,
                                     allow_broadcast_autostart=False,
                                     on_broadcaster_ready=register_broadcaster,
                                     on_archiver_ready=register_archiver)
            if code == 2:
                self.statuses[name] = "Требуется повторная авторизация"
            elif code:
                self.statuses[name] = f"Остановлен с ошибкой ({code})"
            else:
                self.statuses[name] = "Остановлен"
        except asyncio.CancelledError:
            self.statuses[name] = "Остановлен"
            raise
        except Exception as e:  # noqa: BLE001
            self.statuses[name] = f"Ошибка запуска: {type(e).__name__}"
            log.exception("Ошибка дополнительного аккаунта %s", name)
        finally:
            self.configs.pop(name, None)
            self.broadcasters.pop(name, None)
            self.archivers.pop(name, None)
            self.locks.pop(name, None)
            if lock not in (None, True):
                try:
                    lock.close()
                except Exception:
                    pass
            ACTIVE_PROFILE.reset(profile_token)

    async def stop_profile(self, name: str) -> tuple[bool, str]:
        task = self.tasks.get(name)
        if not task or task.done():
            return False, "Аккаунт уже остановлен."
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            self.statuses[name] = "Остановка не завершилась вовремя"
            return False, "Не удалось подтвердить остановку за 15 секунд. Не меняю настройки, чтобы не повредить сессию."
        if not task.done():
            self.statuses[name] = "Остановка не завершилась"
            return False, "Аккаунт ещё завершает работу. Попробуй обновить статус позже."
        self.statuses[name] = "Остановлен"
        return True, "Аккаунт остановлен."

    async def start_all(self) -> list[tuple[str, bool, str]]:
        results = []
        for name in self.names():
            if self.has_session(name):
                ok, message = await self.start_profile(name)
                results.append((name, ok, message))
        return results

    async def stop_all(self) -> None:
        names = [name for name in self.tasks if self.is_running(name)]
        await asyncio.gather(*(self.stop_profile(name) for name in names), return_exceptions=True)
        # Also close any unfinished interactive login flows.
        for name in list(self.auth_flows):
            await self._discard_auth(name)

    async def _expire_auth_flow(self, name: str, expires: float) -> None:
        try:
            await asyncio.sleep(max(0.0, expires - time.monotonic()))
            flow = self.auth_flows.get(name)
            if flow and flow.get("expires") == expires:
                await self._discard_auth(name)
                self.statuses[name] = "Срок авторизации истёк"
                log.info("Временная авторизация профиля %s закрыта по таймауту", name)
        except asyncio.CancelledError:
            raise
        finally:
            current = self.auth_timeout_tasks.get(name)
            if current is asyncio.current_task():
                self.auth_timeout_tasks.pop(name, None)

    async def begin_login(self, name: str, phone: str) -> tuple[str, str]:
        phone = phone.strip()
        if not re.fullmatch(r"\+?[1-9]\d{6,14}", phone):
            return "error", "Неверный формат номера. Отправь номер с кодом страны, например +79991234567."
        if not phone.startswith("+"):
            phone = "+" + phone
        if self.is_running(name):
            return "error", "Сначала останови этот профиль, затем начинай авторизацию."
        await self._discard_auth(name)
        try:
            cfg = read_profile_config(self._profile_config_path(name))
            ddir = data_dir(cfg)
            problems = [p for p in cfg_problems(cfg) if p != "не задан ID группы-форума"]
            if problems:
                return "error", "Сначала исправь настройки основного профиля: " + "; ".join(problems)
            client = make_client(cfg, ddir)
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                cfg["account_label"] = display_name(me)
                cfg["owner_id"] = me.id
                save_cfg(cfg)
                await client.disconnect()
                ok, message = await self.start_profile(name)
                tail = "Архиватор запускается." if ok else message
                return "done", f"Этот аккаунт уже авторизован: {display_name(me)}. {tail}"
            sent = await client.send_code_request(phone.strip())
            expires = time.monotonic() + 600
            self.auth_flows[name] = {
                "client": client, "phone": phone,
                "phone_code_hash": getattr(sent, "phone_code_hash", None),
                "stage": "code", "expires": expires,
            }
            timer = asyncio.create_task(self._expire_auth_flow(name, expires), name=f"auth-timeout:{name}")
            self.auth_timeout_tasks[name] = timer
            return "code", "Код отправлен в Telegram. Отправь сюда код из Telegram. Он будет удалён из чата после обработки."
        except Exception as e:  # noqa: BLE001
            try:
                if "client" in locals() and client.is_connected():
                    await client.disconnect()
            except Exception:
                pass
            log.warning("Не удалось отправить код для профиля %s: %s", name, type(e).__name__)
            return "error", f"Не удалось отправить код ({type(e).__name__}). Проверь номер и попробуй снова позже."

    async def submit_code(self, name: str, code: str) -> tuple[str, str]:
        flow = self.auth_flows.get(name)
        if not flow or time.monotonic() > flow.get("expires", 0):
            await self._discard_auth(name)
            return "expired", "Срок действия авторизации истёк. Нажми «Авторизовать» и начни заново."
        try:
            await flow["client"].sign_in(phone=flow["phone"], code=code.strip(),
                                         phone_code_hash=flow.get("phone_code_hash"))
        except errors.SessionPasswordNeededError:
            flow["stage"] = "password"
            return "password", "Для этого аккаунта включена двухэтапная защита. Отправь пароль 2FA. Сообщение будет удалено."
        except errors.PhoneCodeInvalidError:
            return "invalid", "Код не подошёл. Проверь код и отправь его ещё раз."
        except errors.PhoneCodeExpiredError:
            await self._discard_auth(name)
            return "expired", "Код истёк. Нажми «Авторизовать» и запроси новый."
        except Exception as e:  # noqa: BLE001
            log.warning("Ошибка ввода кода авторизации профиля %s: %s", name, type(e).__name__)
            return "error", f"Не удалось войти ({type(e).__name__}). Проверь код или запроси новый."
        return await self._finish_login(name)

    async def submit_password(self, name: str, password: str) -> tuple[str, str]:
        flow = self.auth_flows.get(name)
        if not flow or flow.get("stage") != "password" or time.monotonic() > flow.get("expires", 0):
            await self._discard_auth(name)
            return "expired", "Срок действия авторизации истёк. Начни вход заново."
        try:
            await flow["client"].sign_in(password=password)
        except errors.PasswordHashInvalidError:
            return "invalid", "Пароль 2FA неверный. Попробуй ещё раз."
        except Exception as e:  # noqa: BLE001
            log.warning("Ошибка 2FA профиля %s: %s", name, type(e).__name__)
            return "error", f"Не удалось завершить вход ({type(e).__name__})."
        return await self._finish_login(name)

    async def _finish_login(self, name: str) -> tuple[str, str]:
        flow = self.auth_flows.get(name)
        if not flow:
            return "expired", "Сеанс авторизации не найден. Начни вход заново."
        client = flow["client"]
        try:
            me = await client.get_me()
            cfg = read_profile_config(self._profile_config_path(name))
            cfg["account_label"] = display_name(me)
            cfg["owner_id"] = int(me.id)
            save_cfg(cfg)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            self.auth_flows.pop(name, None)
            timer = self.auth_timeout_tasks.pop(name, None)
            if timer and timer is not asyncio.current_task():
                timer.cancel()
        ok, message = await self.start_profile(name)
        suffix = " Архиватор запускается." if ok else f" Но запуск не выполнен: {message}"
        return "done", f"✅ Аккаунт авторизован: {display_name(me)}.{suffix}"

    async def _discard_auth(self, name: str) -> None:
        timer = self.auth_timeout_tasks.pop(name, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        flow = self.auth_flows.pop(name, None)
        if flow:
            try:
                await flow["client"].disconnect()
            except Exception:
                pass

    def get_broadcaster(self, name: str):
        return self.broadcasters.get(name)

    def start_broadcast(self, name: str) -> tuple[bool, str]:
        bc = self.get_broadcaster(name)
        cfg = self.configs.get(name)
        if not bc or not cfg:
            return False, "Сначала запусти этот Telegram-аккаунт."
        if bc.running:
            return False, "Рассылка уже работает."
        if not bc.start(cfg.get("broadcast_interval_min", 60), cfg.get("broadcast_hours", 12)):
            return False, "Не удалось запустить рассылку."
        return True, "Рассылка этого аккаунта запускается."

    def stop_broadcast(self, name: str) -> tuple[bool, str]:
        bc = self.get_broadcaster(name)
        if not bc or not bc.running:
            return False, "Рассылка не запущена."
        bc.stop()
        return True, "Останавливаю рассылку этого аккаунта."

    def set_broadcast_number(self, name: str, key: str, value) -> None:
        if key not in {"broadcast_interval_min", "broadcast_hours", "broadcast_delay_min", "broadcast_delay_max"}:
            raise ValueError("Недопустимая настройка рассылки")
        cfg = self.configs.get(name) or read_profile_config(profile_config_path(name))
        if key == "broadcast_delay_min" and float(value) > float(cfg.get("broadcast_delay_max", 1.2)):
            raise ValueError("Минимальная пауза не может быть больше максимальной")
        if key == "broadcast_delay_max" and float(value) < float(cfg.get("broadcast_delay_min", 0.6)):
            raise ValueError("Максимальная пауза не может быть меньше минимальной")
        cfg[key] = value
        save_cfg(cfg)
        if name in self.configs:
            self.configs[name][key] = value
        bc = self.broadcasters.get(name)
        if bc and key == "broadcast_interval_min" and bc.running:
            bc.set_interval(value)
        elif bc and key == "broadcast_hours" and bc.running:
            bc.set_duration(value)
        elif bc and key in {"broadcast_delay_min", "broadcast_delay_max"}:
            bc._apply_delays()
        elif key in {"broadcast_interval_min", "broadcast_hours"}:
            # A stopped profile can still have an interrupted broadcast marked active
            # on disk. Keep only schedule settings aligned; delay settings must not
            # change the campaign end timestamp.
            state_path = data_dir(cfg) / "broadcast_state.json"
            state = Broadcaster._read(state_path, Broadcaster._new_state())
            if state.get("active"):
                if key == "broadcast_interval_min":
                    old_interval = max(1, int(state.get("interval", 60)))
                    new_interval = max(1, int(value))
                    state["interval"] = new_interval
                    if state.get("next"):
                        history = state.get("history") or []
                        last = history[-1] if history and isinstance(history[-1], dict) else {}
                        try:
                            base_time = float(last.get("t", 0)) or (float(state["next"]) - old_interval * 60)
                        except (TypeError, ValueError, OverflowError):
                            base_time = float(state["next"]) - old_interval * 60
                        state["next"] = max(time.time(), base_time + new_interval * 60)
                else:
                    state["end"] = time.time() + max(0.1, float(value)) * 3600
                Broadcaster._write(state_path, state)

    def set_paused(self, name: str) -> bool:
        cfg = (self.configs.get(name) or read_profile_config(profile_config_path(name)))
        cfg["paused"] = not bool(cfg.get("paused"))
        save_cfg(cfg)
        if name in self.configs:
            self.configs[name]["paused"] = cfg["paused"]
        return bool(cfg["paused"])

    def set_target(self, name: str, chat_id: int) -> None:
        cfg = read_profile_config(profile_config_path(name))
        cfg["target_chat_id"] = int(chat_id)
        save_cfg(cfg)
        if name in self.configs:
            self.configs[name]["target_chat_id"] = int(chat_id)


# =============================================================================
#  Бот-панель управления (кнопки в Telegram)
# =============================================================================
class ControlBot:
    PAGE = 8
    TOGGLES = [("save_outgoing", "Сохранять и мои медиа"),
               ("info_message", "Подпись с датой под медиа"),
               ("keep_local", "Хранить копии на диске VPS"),
               ("broadcast_notify_waves", "Отчёт после каждой волны рассылки"),
               ("broadcast_autostart", "Рассылка стартует вместе с сервисом"),
               ("broadcast_auto_subscribe", "Фоновая автоподписка по ссылкам ботов"),
               ("broadcast_spambot", "Писать @SpamBot при спам-блоке")]
    # ключ: (название, тип, минимум, максимум)
    NUMS = {"broadcast_interval_min": ("Интервал между волнами рассылки, минут", int, 1, 1440),
            "broadcast_hours": ("Длительность рассылки, часов", float, 0.1, 720),
            "tz_offset": ("Часовой пояс для времени в боте (UTC+N), например 3", int, -12, 14),
            "broadcast_delay_min": ("Мин. пауза между чатами (сек)", float, 0.0, 60),
            "broadcast_delay_max": ("Макс. пауза между чатами (сек)", float, 0.0, 120),
            "low_disk_percent": ("Предупреждать, если свободно меньше N% диска", int, 1, 50),
            "target_chat_id": ("ID группы-форума для архива (-100...)", int, None, None)}
    COMMANDS = {"/start": "main", "/menu": "main", "/status": "status", "/catalog": "cat:default:0", "/broadcast": "bc",
                "/accounts": "acc:list", "/log": "log:b", "/errors": "log:e", "/help": "main",
                "/version": "about", "/backup": "backup", "/health": "maint", "/media": "media:default"}

    def __init__(self, cfg, ddir, db, arch, bot, owner_id, restart_cb, bc, notifier=None, client=None,
                 allow_broadcast=True, account_manager=None):
        self.cfg, self.ddir, self.db, self.arch, self.bot = cfg, ddir, db, arch, bot
        self.owner_id, self.restart_cb, self.bc = owner_id, restart_cb, bc
        self.allow_broadcast = bool(allow_broadcast)
        self.notifier, self.client = notifier, client
        self.account_manager = account_manager
        self.started = time.time()
        self.bf_task = None
        self.bf_count = 0
        self.profile_bf_tasks = {}
        self.profile_bf_counts = {}
        self.pending = None   # ключ настройки, значение которой бот ждёт текстом
        self.pending_action = None  # пошаговый ввод имени/телефона/кода/2FA из панели
        self.catalog_states = {}
        self.catalog_cleanup_plans = {}
        global _UI_CUSTOM_EMOJI
        custom_emoji = cfg.get("ui_custom_emoji", {})
        _UI_CUSTOM_EMOJI = custom_emoji if isinstance(custom_emoji, dict) else {}
        bot.parse_mode = None  # без разметки: имена со спецсимволами ничего не ломают

    # ----- экраны -----
    def main_view(self):
        s = self.db.stats()
        paused = bool(self.cfg.get("paused"))
        account_label = self.cfg.get("account_label") or "Основной аккаунт"
        archive_state = "⏸ Пауза" if paused else "🟢 Активна"
        selected_labels = [ARCHIVE_MEDIA_LABELS[k] for k in ("round_video", "photo", "video")
                           if k in configured_media_types(self.cfg)]
        text = (
            "✨ CIRCLES CONTROL · ПАНЕЛЬ\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 < {account_label} >\n"
            f"🧩 Версия {APP_VERSION}\n\n"
            "🎞 АРХИВ МЕДИА\n"
            f"├ Статус: {archive_state}\n"
            f"├ Людей / тем: {s['topics']}\n"
            f"├ Сохранено: {s['sent']} из {s['total']}\n"
            f"└ Типы: {', '.join(selected_labels) or 'не выбраны'}"
        )
        if s["unsent"]:
            text += f"\n⚠️ Ожидают повтора: {s['unsent']}"
        if self.arch.target is None:
            text += "\n⚠️ Группа архива не настроена — открой диагностику."
        if self.allow_broadcast:
            text += "\n\n📢 РАССЫЛКА\n" + self.bc.short()
        else:
            text += "\n\n📢 Рассылка для этого режима выключена."
        if RECENT_ERRORS:
            text += f"\n\n🟠 Предупреждений: {len(RECENT_ERRORS)} · см. Логи → Ошибки"
        text += "\n━━━━━━━━━━━━━━━━━━\nВыбери раздел ниже."

        B = _ui_button
        rows = []
        if self.allow_broadcast:
            rows.append([B("📢 Рассылка", b"bc"), B("📊 Обзор", b"status")])
        else:
            rows.append([B("📊 Обзор", b"status"), B("🩺 Диагностика", b"diag")])
        rows.append([B("⏸ Пауза архива" if not paused else "▶️ Продолжить архив", b"pause")])
        rows.append([B("🎞 Каталог медиа", b"cat:default:0"), B("🧩 Типы медиа", b"media:default")])
        rows.append([B("📥 Старые медиа", b"bf")])
        rows.append([B("⚙️ Настройки", b"set"), B("📜 Журнал", b"log:b" if self.allow_broadcast else b"log:c")])
        if self.account_manager:
            rows.append([B("👤 Управление аккаунтами", b"acc:list")])
        rows.append([B("🧰 Обслуживание", b"maint"), B("🔄 Перезапуск", b"restart")])
        rows.append([B("ℹ️ О программе", b"about")])
        return text, rows

    def bc_view(self):
        B = _ui_button
        c = self.cfg
        selected_post = int(self.bc.state.get("post_id") or 0)
        excluded_count = len(c.get("broadcast_exclude", []))
        text = ("📢 Рассылка основного аккаунта\n\n" + self.bc.summary() +
                f"\n\nНастройки: интервал {c['broadcast_interval_min']} мин, "
                f"длительность {c['broadcast_hours']} ч" +
                f"\nВыбранный пост из личного Избранного: {'ID ' + str(selected_post) if selected_post else 'последнее сообщение'}" +
                f"\nРежим получателей: все группы, кроме исключённых ({excluded_count})" +
                f"\nАвтоподписка по ссылкам ботов: {'включена' if c.get('broadcast_auto_subscribe', True) else 'выключена'}" +
                "\nРаботает независимо от рассылки: обрабатывает ссылки в тексте и кнопках группового сообщения от бота, если бот упомянул аккаунт или ответил на его сообщение. Личные диалоги намеренно не запускают автоматические вступления.")
        rows = [[B("🔄 Обновить", b"bc"), B("⏹ Остановить", b"bc:stop")] if self.bc.running
                else [B("▶️ Запустить", b"bc:go")],
                [B("📝 Пост", b"post"), B("💬 Чаты", b"ch:v:0"), B("📈 История", b"hist")],
                [B(f"🤖 Очередь автоподписки · {sum(self.bc.autosub_queue.counts().get(k, 0) for k in ('queued','retry','waiting_approval','processing'))}", b"abqueue:default")],
                [B("⏱ Интервал", b"num:broadcast_interval_min"), B("⌛ Длительность", b"num:broadcast_hours")],
                [B(f"🤖 Автоподписка: {'✅' if c.get('broadcast_auto_subscribe', True) else '❌'}", b"tg:broadcast_auto_subscribe")],
                [B("⚙️ Настройки автоподписки", b"abautoset:default")],
                [B("◀️ Назад", b"main")]]
        return text, rows

    async def post_view(self):
        B = _ui_button
        p = await self.bc.preview()
        if not p:
            return ("📝 Пост для рассылки\n\nВ личном «Избранном» основного аккаунта пока нет сообщений. "
                    "Отправь туда нужный пост, затем выбери его из последних 10.",
                    [[B("🗂 Открыть последние 10", b"posts")],
                     [B("📌 Взять последнее из Избранного", b"post:pin")], [B("◀️ Назад", b"bc")]])
        body = p["text"].strip()
        if len(body) > 700:
            body = body[:700] + "…"
        head = "📌 Закреплён этот пост:" if p["pinned"] else "ℹ️ Пост не закреплён, при запуске возьму последний из Избранного:"
        text = f"📝 Пост для рассылки\n\n{head}\n\n{body or '(без текста)'}"
        if p["media"]:
            text += "\n[+ медиа]"
        text += ("\n\nПост выбирается из «Избранного» основного Telegram-аккаунта. "
                 "Выбранное сообщение сохраняется после остановки и перезапуска.")
        return text, [[B("🗂 Выбрать одно из последних 10", b"posts")],
                      [B("📌 Взять последнее из Избранного", b"post:pin")], [B("◀️ Назад", b"bc")]]

    async def saved_posts_view(self):
        B = _ui_button
        posts = await self.bc.saved_posts(limit=10)
        selected_id = int(self.bc.state.get("post_id") or 0)
        lines = ["🗂 Последние 10 сообщений из «Избранного» основного аккаунта", "",
                 "Выбери сообщение, которое будет использовать основная рассылка.", ""]
        if selected_id:
            selected_msg = await self.client.get_messages("me", ids=selected_id)
            if selected_msg:
                body = (getattr(selected_msg, "message", None) or getattr(selected_msg, "text", None) or "").strip()
                lines.extend(["👁 ПРЕДПРОСМОТР ВЫБРАННОГО ПОСТА", body[:900] or "(без текста)"])
                if getattr(selected_msg, "media", None):
                    lines.append("📎 К посту прикреплено медиа")
                lines.append("")
        buttons = []
        if not posts:
            lines.append("Основное Избранное пустое.")
        for msg in posts:
            body = (getattr(msg, "message", None) or getattr(msg, "text", None) or "").replace("\n", " ").strip()
            if not body:
                body = "📎 медиа" if getattr(msg, "media", None) else "сообщение без текста"
            snippet = body[:50] + ("…" if len(body) > 50 else "")
            date = getattr(msg, "date", None)
            date_label = date.strftime("%d.%m %H:%M") if date else "без даты"
            selected = int(msg.id) == selected_id
            lines.append(f"{'✅' if selected else '▫️'} ID {msg.id} · {date_label} · {snippet}")
            buttons.append([B(f"{'✅' if selected else '▫️'} {date_label} · {snippet}"[:60], f"pick:{int(msg.id)}".encode())])
        buttons += [[B("🔄 Обновить", b"posts")], [B("◀️ К рассылке", b"bc")]]
        return "\n".join(lines), buttons

    async def chats_view(self, page=0):
        B = _ui_button
        chats = await self.bc.chat_list()
        pages = max(1, (len(chats) + self.PAGE - 1) // self.PAGE)
        page = min(max(page, 0), pages - 1)
        st = [self.bc.chat_status(cid) for cid, _ in chats]
        text = (f"💬 Чаты рассылки\n\nВсего групп: {len(chats)} · включено {st.count('ok')} · "
                f"исключено {st.count('ex')} · ранее недоступно {st.count('ban')}\n"
                "✅ - включён, 🚫 - исключён мной, ⛔ - Telegram ранее запретил отправку. "
                "Все группы выбираются автоматически; нажми на чат, чтобы исключить или вернуть его.")
        if not chats:
            text += "\n\nГрупп не найдено."
        btns = []
        for cid, title in chats[page * self.PAGE:(page + 1) * self.PAGE]:
            icon = {"ok": "✅", "ex": "🚫", "ban": "⛔"}[self.bc.chat_status(cid)]
            btns.append([B(f"{icon} {title}"[:60], f"ch:{cid}:{page}".encode())])
        nav = []
        if page > 0:
            nav.append(B("◀️", f"ch:v:{page - 1}".encode()))
        nav.append(B(f"{page + 1}/{pages}", b"noop"))
        if page < pages - 1:
            nav.append(B("▶️", f"ch:v:{page + 1}".encode()))
        btns += [nav,
                 [B("✅ Все группы + повторить недоступные", f"ch:all:{page}".encode(), style="success")],
                 [B("🔄 Обновить список", f"ch:r:{page}".encode()), B("◀️ Назад", b"bc")]]
        return text, btns

    def hist_view(self):
        st = self.bc.state
        rows = st["history"][-8:][::-1]
        lines = []
        for h in rows:
            err = ""
            if h.get("err"):
                err = " · " + ", ".join(f"{REASON_LABELS.get(k, k)} {v}" for k, v in h["err"].items())
            skipped = int(h.get("skipped", 0))
            skip_text = f" ⏭{skipped}" if skipped else ""
            partial_text = " · ⚠️ неполная" if h.get("partial") else ""
            lines.append(f"#{h['n']} {fmt_ts(self.cfg, h['t'])} ✅{h['ok']} ❌{h['fail']}{skip_text} · {fmt_dur(h['sec'])}{partial_text}{err}")
        text = ("📈 История рассылки\n\n" + ("\n".join(lines) if lines else "Волн пока не было.") +
                f"\n\nЗа всё время: ✅ {st['life_ok']} / ❌ {st['life_fail']}, волн: {st['life_waves']}")
        return text, [[_ui_button("🔄 Обновить", b"hist"), _ui_button("◀️ Назад", b"bc")]]

    _size_cache = (0.0, 0)

    async def _videos_size(self, vids, ttl=60):
        """Размер папки с медиа считаем не чаще раза в минуту (rglob по тысячам файлов долгий)."""
        ts, val = self._size_cache
        if time.monotonic() - ts < ttl:
            return val
        val = await asyncio.to_thread(lambda: dir_size(vids) if vids.exists() else 0)
        self._size_cache = (time.monotonic(), val)
        return val

    async def status_view(self):
        vids = self.ddir / "videos"
        s, size = await asyncio.gather(asyncio.to_thread(self.db.stats), self._videos_size(vids))
        du = shutil.disk_usage(self.ddir)
        up = int(time.time() - self.started)
        media_counts = s.get("by_type", {})
        media_lines = "\n".join(
            f"├ {label}: {media_counts.get(key, 0)}"
            for key, label in (("round_video", "Кружки"), ("photo", "Фото"), ("video", "Видео")))
        text = ("📊 Статус\n\n"
                f"Работаю: {up // 86400} д {up % 86400 // 3600} ч {up % 3600 // 60} мин\n\n"
                "🎞 Архив медиа\n"
                f"Состояние: {'⏸ на паузе' if self.cfg.get('paused') else '▶️ архивируются'}\n"
                f"Тем (людей): {s['topics']}\n"
                f"Медиафайлов: {s['total']} (в архиве {s['sent']}, ждут повтора {s['unsent']})\n"
                f"{media_lines}\n"
                f"Последнее медиа: {fmt_ts(self.cfg, s['last'])}\n"
                f"В игнор-листе: {len(self.cfg['ignore_users'])}\n\n"
                + (("📢 Рассылка\n" + self.bc.summary() + "\n\n") if self.allow_broadcast else "")
                + "💾 Диск\n"
                f"Медиафайлы: {human(size)} · свободно {human(du.free)} из {human(du.total)}\n"
                f"Предупреждений в памяти: {len(RECENT_ERRORS)}")
        B = _ui_button
        return text, [[B("🔄 Обновить", b"status"), B("🧹 Удалить копии", b"clean")], [B("◀️ Назад", b"main")]]

    def settings_view(self):
        B = _ui_button
        toggles = self.TOGGLES if self.allow_broadcast else [
            x for x in self.TOGGLES
            if not x[0].startswith("broadcast_") or x[0] == "broadcast_auto_subscribe"
        ]
        nums = self.NUMS if self.allow_broadcast else {k: v for k, v in self.NUMS.items() if not k.startswith("broadcast_")}
        rows = [[B(f"{'✅' if self.cfg.get(k) else '❌'} {t}", f"tg:{k}".encode())] for k, t in toggles]
        for k, (t, *_r) in nums.items():
            rows.append([B(f"✏️ {t.split(',')[0].split(' (')[0]}: {self.cfg.get(k)}"[:60], f"num:{k}".encode())])
        # Auto-subscription runs with the user session even when the broadcast
        # campaign UI is disabled, so its controls must remain reachable.
        rows.append([B("🤖 Настройки автоподписки", b"abautoset:default")])
        rows.append([B("◀️ Назад", b"main")])
        return ("⚙️ Настройки\n\nПереключатели применяются сразу. Числа - нажми и отправь новое значение сообщением."), rows

    def media_view(self, profile="default"):
        """Per-profile media-type selection shared by live collection and historical backfill."""
        B = _ui_button
        try:
            cfg = self._catalog_profile(profile)
        except Exception as exc:
            return (f"Не удалось открыть настройки медиа: {type(exc).__name__}",
                    [[B("◀️ Назад", b"acc:list")]])
        selected = configured_media_types(cfg)
        labels = [
            ("round_video", "🫧 Кружки"),
            ("photo", "🖼 Фотографии"),
            ("video", "🎬 Обычные видео"),
        ]
        counts = {}
        owned_db = None
        try:
            media_db, owned_db, _ = self._catalog_db(profile)
            if media_db is not None:
                counts = media_db.media_type_status()
        except Exception as exc:
            log.warning("Не удалось получить статистику типов медиа профиля %s: %s", profile, exc)
        finally:
            if owned_db:
                owned_db.close()
        lines = [
            f"🎞 ТИПЫ МЕДИА · {cfg.get('account_label') or profile}",
            "━━━━━━━━━━━━━━━━━━",
            "Выбор действует для новых личных сообщений и команды «Старые медиа». Фото/видео, отправленные как файл, тоже распознаются.",
            "Историческая выгрузка сейчас сканирует личные диалоги 1:1; группы и каналы не входят в этот проход.",
            "",
            f"Активно: {len(selected)} из {len(labels)} типов",
        ]
        for key, label in labels:
            item = counts.get(key, {})
            lines.append(
                f"{'✅' if key in selected else '▫️'} {label.split(' ', 1)[1]} · "
                f"всего {item.get('total', 0)} · в архиве {item.get('sent', 0)} · "
                f"ожидают {item.get('pending', 0)}"
            )
        if not selected:
            lines.extend(["", "⚠️ Ничего не архивируется. Включи хотя бы один тип медиа."])
        if cfg.get("keep_local", True):
            lines.extend(["", "💾 Локальные копии включены: файлы занимают место на VPS."])
        else:
            lines.extend(["", "💾 Локальные копии выключены: отправленные в архив файлы удаляются с VPS."])
        buttons = [[B(f"{'✅' if key in selected else '▫️'} {label}",
                      f"mt:{profile}:{key}".encode(),
                      style="success" if key in selected else "primary")]
                   for key, label in labels]
        buttons.append([B("✅ Выбрать все три типа", f"mta:{profile}".encode(), style="success"),
                        B("🫧 Только кружки", f"mtc:{profile}".encode())])
        buttons.append([B("📥 Выгрузить старые медиа", (b"bf" if profile == "default"
                                                           else f"pbf:{profile}".encode()),
                          style="success")])
        back = b"main" if profile == "default" else f"acc:detail:{profile}".encode()
        buttons.append([B("◀️ Назад", back)])
        return "\n".join(lines), buttons

    def accounts_view(self):
        B = _ui_button
        manager = self.account_manager
        if not manager:
            return "👤 Управление аккаунтами недоступно.", [[B("◀️ В меню", b"main")]]
        entries = account_profiles()
        lines = [
            "👤 МОИ TELEGRAM-АККАУНТЫ",
            "━━━━━━━━━━━━━━━━━━",
            "Каждый профиль имеет собственную сессию, архив, пост из Избранного и настройки рассылки.",
            "",
        ]
        buttons = []
        for name, path in entries:
            if name == "default":
                title = self.cfg.get("account_label") or "Основной аккаунт"
            else:
                try:
                    pcfg = read_profile_config(path)
                    title = pcfg.get("account_label") or name
                except Exception:
                    title = name
            state = manager.status(name)
            if "Работает" in state or "активна" in state.lower():
                marker = "🟢"
            elif "Не авторизован" in state or "не запущен" in state.lower():
                marker = "🔐"
            elif "код" in state.lower() or "2fa" in state.lower() or "пароль" in state.lower():
                marker = "🟡"
            else:
                marker = "⚪"
            lines.append(f"{marker} {title} · {state}")
            buttons.append([B(f"{marker} {title[:24]} · {name}"[:55], f"acc:detail:{name}".encode())])
        if len(entries) <= 1:
            lines.extend(["", "Аккаунтов пока немного. Добавь второй профиль, чтобы управлять им отдельно."])
        buttons += [
            [B("➕ Добавить аккаунт", b"acc:add", style="success"), B("▶️ Запустить все", b"acc:startall", style="success")],
            [B("⏹ Остановить дополнительные", b"acc:stopall", style="danger")],
            [B("🔄 Обновить список", b"acc:list"), B("◀️ В меню", b"main")],
        ]
        return "\n".join(lines), buttons

    def account_detail_view(self, name: str):
        B = _ui_button
        if name == "default":
            return (
                "👤 ОСНОВНОЙ АККАУНТ\n━━━━━━━━━━━━━━━━━━\n"
                "Этот аккаунт держит панель управления. У него собственная архивация и отдельная рассылка из личного Избранного.\n\n"
                "Выбери, что настроить:",
                [[B("🎞 Каталог медиа", b"cat:default:0", style="primary")],
                 [B("🧩 Типы медиа", b"media:default")],
                 [B("📥 Старые медиа", b"bf")],
                 [B("📢 Настройки рассылки", b"bc", style="primary")],
                 [B("⚙️ Настройки архива", b"set")],
                 [B("◀️ Все аккаунты", b"acc:list")]]
            )
        try:
            path = profile_config_path(name)
            if not path.is_file():
                return ("Профиль не найден.", [[B("◀️ Все аккаунты", b"acc:list")]])
            cfg = read_profile_config(path)
            ddir = data_dir(cfg)
        except Exception as e:
            return (f"Не удалось открыть профиль: {e}", [[B("◀️ Все аккаунты", b"acc:list")]])
        manager = self.account_manager
        running = manager.is_running(name)
        label = cfg.get("account_label") or name
        session_exists = manager._session_path(cfg, ddir).exists()
        state = manager.status(name)
        bc = manager.get_broadcaster(name)
        if running:
            indicator = "🟢"
        elif session_exists:
            indicator = "⚪"
        else:
            indicator = "🔐"
        broadcast_state = bc.short() if bc else "доступна после запуска аккаунта"
        text = (
            f"👤 АККАУНТ · {label}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{indicator} Состояние: {state}\n"
            f"🔐 Сессия: {'сохранена' if session_exists else 'не создана'}\n"
            f"🎞 Архивация: {'⏸ на паузе' if cfg.get('paused') else '🟢 включена'}\n"
            f"🧩 Типы медиа: {', '.join(ARCHIVE_MEDIA_LABELS[k] for k in ('round_video', 'photo', 'video') if k in configured_media_types(cfg)) or 'не выбраны'}\n"
            f"🎯 Группа архива: {cfg.get('target_chat_id') or 'не задана'}\n"
            f"📢 Рассылка: {broadcast_state}\n\n"
            "Профиль независим: пост, интервалы, получатели, журнал и медиа хранятся отдельно."
        )
        actions = []
        if running:
            actions.append(B("⏹ Остановить аккаунт", f"acc:stop:{name}".encode(), style="danger"))
        elif session_exists:
            actions.append(B("▶️ Запустить аккаунт", f"acc:start:{name}".encode(), style="success"))
        actions.append(B("🔐 Авторизовать / переподключить", f"acc:login:{name}".encode()))
        actions.append(B("⏸ Пауза / продолжить архив", f"acc:pause:{name}".encode()))
        actions.append(B("🎞 Каталог медиа", f"cat:{name}:0".encode(), style="primary"))
        actions.append(B("🧩 Типы медиа", f"media:{name}".encode()))
        actions.append(B("📥 Выгрузить старые медиа", f"pbf:{name}".encode(), style="success"))
        actions.append(B("🎯 Изменить группу архива", f"acc:target:{name}".encode()))
        actions.append(B("📢 Независимая рассылка", f"ab:{name}".encode(), style="primary"))
        actions.append(B("📎 Журнал аккаунта", f"acc:log:{name}".encode()))
        actions.append(B("◀️ Все аккаунты", b"acc:list"))
        return text, [[x] for x in actions]

    def autosub_settings_view(self, name: str):
        B = _ui_button
        if name == "default":
            cfg, bc = self.cfg, self.bc
        else:
            cfg = (self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))) if self.account_manager else read_profile_config(profile_config_path(name))
            bc = self.account_manager.get_broadcaster(name) if self.account_manager else None
        enabled = bool(cfg.get("broadcast_auto_subscribe", True))
        try:
            gap = min(3600.0, max(2.0, float(cfg.get("broadcast_auto_subscribe_gap_seconds", 90))))
            daily = max(1, int(cfg.get("broadcast_auto_subscribe_daily_limit", 15)))
            max_per_message = min(100, max(1, int(cfg.get("broadcast_auto_subscribe_max_per_message", 24))))
            batch_size = min(50, max(1, int(cfg.get("broadcast_auto_subscribe_batch_size", 5))))
            batch_pause = min(1440, max(5, int(cfg.get("broadcast_auto_subscribe_batch_pause_minutes", 30))))
        except (TypeError, ValueError):
            gap, daily, max_per_message, batch_size, batch_pause = 90.0, 15, 24, 5, 30
        pause_until, pause_reason = bc.autosub_queue.pause_info() if bc else (None, "")
        batch_count = bc.autosub_queue.batch_action_count() if bc else 0
        if pause_until is not None and pause_until < 0:
            if pause_reason == AUTOSUB_OWNER_PAUSE_REASON:
                queue_state = "⏸ ручная пауза владельца"
            else:
                queue_state = f"🛑 ограничение аккаунта: {pause_reason or 'требуется проверка'}"
        elif pause_until is not None and pause_until > time.time():
            queue_state = f"⏳ пауза до {fmt_ts(cfg, pause_until)} · {pause_reason or 'ожидание'}"
        else:
            queue_state = "▶️ работает / ожидает задания" if bc else "доступна после запуска аккаунта"
        body = (f"⚙️ НАСТРОЙКИ АВТОПОДПИСКИ · {name}\n━━━━━━━━━━━━━━━━━━\n"
                f"Состояние: {'🟢 включена' if enabled else '⚪ выключена'}\n"
                f"Интервал между действиями: {gap:g} сек\n"
                f"Лимит вступлений: {daily} за скользящие 24 часа\n"
                f"Ссылок из одного сообщения: не более {max_per_message}\n"
                f"Пакет: {batch_size} действий (/start, вступления, новые заявки), затем отдых {batch_pause} мин\n"
                f"В текущем пакете: {batch_count}/{batch_size}\n"
                f"Очередь: {queue_state}\n\n"
                "Параметры независимы для профиля и применяются без перезапуска. Уже начавшаяся плановая пауза завершается по сохранённому сроку; ограничения Telegram не сокращаются настройками.")
        rows = [
            [B(f"Автоподписка {'✅ ВКЛ' if enabled else '❌ ВЫКЛ'}", f"absub:{name}".encode())],
            [B(f"⏱ Интервал · {gap:g} сек", f"abautosubnum:{name}:gap".encode()),
             B(f"📊 Лимит · {daily}/24 ч", f"abautosubnum:{name}:daily".encode())],
            [B(f"🔗 Ссылок/сообщение · {max_per_message}", f"abautosubnum:{name}:max".encode()),
             B(f"📦 Действий/пакет · {batch_size}", f"abautosubnum:{name}:batch".encode())],
            [B(f"☕ Отдых · {batch_pause} мин", f"abautosubnum:{name}:rest".encode())],
        ]
        if bc:
            if pause_until is not None and pause_until < 0 and pause_reason != AUTOSUB_OWNER_PAUSE_REASON:
                rows.append([B("⚠️ Возобновить после проверки", f"abqresume:{name}".encode(), style="success")])
            elif pause_until is not None and pause_until < 0:
                rows.append([B("▶️ Продолжить очередь", f"abqmanual:{name}".encode(), style="success")])
            elif pause_until is not None and pause_until > time.time():
                rows.append([B("⏳ Действует обязательная пауза", f"abqueue:{name}".encode())])
            else:
                rows.append([B("⏸ Приостановить очередь", f"abqmanual:{name}".encode(), style="danger")])
            rows.append([B("📋 Очередь и история", f"abqueue:{name}".encode())])
        rows.append([B("◀️ Назад", (b"bc" if name == "default" else f"ab:{name}".encode()))])
        return body, rows

    def autosub_queue_view(self, name: str):
        B = _ui_button
        bc = self.bc if name == "default" else (self.account_manager.get_broadcaster(name) if self.account_manager else None)
        if not bc:
            return "Аккаунт не запущен. Очередь будет доступна после его запуска.", [[B("◀️ Назад", f"ab:{name}".encode())]]
        counts = bc.autosub_queue.counts()
        labels = {
            "queued": "ожидают обработки", "retry": "ожидают повторной попытки",
            "waiting_approval": "заявки ожидают одобрения", "processing": "сейчас выполняются",
            "completed": "успешно завершены", "already_done": "уже выполнены",
            "failed_permanent": "недействительные ссылки", "unsupported": "неподдерживаемые цели",
            "needs_review": "требуют ручной проверки", "account_paused": "остановлены ограничением аккаунта",
        }
        lines = [f"🤖 ОЧЕРЕДЬ АВТОПОДПИСКИ · {name}", "━━━━━━━━━━━━━━━━━━"]
        cfg = self.cfg if name == "default" else (self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name)))
        try:
            gap_seconds = min(3600.0, max(2.0, float(cfg.get("broadcast_auto_subscribe_gap_seconds", 90))))
            daily_limit = max(1, int(cfg.get("broadcast_auto_subscribe_daily_limit", 15)))
            batch_size = min(50, max(1, int(cfg.get("broadcast_auto_subscribe_batch_size", 5))))
            batch_pause = min(1440, max(5, int(cfg.get("broadcast_auto_subscribe_batch_pause_minutes", 30))))
        except (TypeError, ValueError):
            gap_seconds, daily_limit, batch_size, batch_pause = 90.0, 15, 5, 30
        rate_wait, rate_reason = bc.autosub_queue.rate_limit_wait(
            gap_seconds=gap_seconds, daily_limit=daily_limit)
        lines.append(f"Темп: пауза {int(gap_seconds)} с · лимит вступлений {daily_limit}/24 ч")
        lines.append(f"Пакет: {bc.autosub_queue.batch_action_count()}/{batch_size} действий · отдых {batch_pause} мин")
        if rate_wait > 0:
            lines.append(f"Следующее действие не раньше чем через: {fmt_dur(rate_wait)} ({rate_reason})")
        for key, label in labels.items():
            count = int(counts.get(key, 0))
            if count or key in {"queued", "retry", "waiting_approval", "processing", "needs_review"}:
                lines.append(f"• {label}: {count}")
        pause_until, pause_reason = bc.autosub_queue.pause_info()
        if pause_until is not None:
            if pause_until < 0:
                if pause_reason == AUTOSUB_OWNER_PAUSE_REASON:
                    lines.extend(["", "⏸ Очередь приостановлена владельцем."])
                else:
                    lines.extend(["", f"🛑 Ограничение аккаунта Telegram: {pause_reason or 'требуется проверка'}"])
            elif pause_until > time.time():
                lines.extend(["", f"⏳ Пауза до: {fmt_ts(cfg, pause_until)} · {pause_reason}"])
        lines.extend(["", "Последние действия:"])
        recent = bc.autosub_queue.list_recent(10)
        if not recent:
            lines.append("Пока нет действий.")
        for item in recent:
            # Never display payload_json, invite hashes or referral tokens in the panel.
            status = str(item.get("status") or "")
            reason = str(item.get("result") or item.get("last_error") or "")[:60]
            lines.append(f"#{item['id']} · {status}" + (f" · {reason}" if reason else ""))
        buttons = []
        if pause_until is not None and pause_until < 0 and pause_reason == AUTOSUB_OWNER_PAUSE_REASON:
            buttons.append([B("▶️ Продолжить очередь", f"abqmanual:{name}".encode(), style="success")])
        elif pause_until is not None and pause_until < 0:
            buttons.append([B("▶️ Возобновить после проверки аккаунта", f"abqresume:{name}".encode(), style="success")])
        elif pause_until is None or pause_until <= time.time():
            buttons.append([B("⏸ Приостановить очередь", f"abqmanual:{name}".encode(), style="danger")])
        buttons.append([B("🔄 Обновить", f"abqueue:{name}".encode()), B("◀️ Назад", f"ab:{name}".encode())])
        return "\n".join(lines), buttons

    def account_broadcast_view(self, name: str):
        B = _ui_button
        if not self.account_manager:
            return ("Менеджер аккаунтов недоступен.", [[B("◀️ Назад", b"acc:list")]])
        bc = self.account_manager.get_broadcaster(name)
        cfg = self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))
        state_path = data_dir(cfg) / "broadcast_state.json"
        try:
            disk_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except Exception:
            disk_state = {}
        post_id = (bc.state.get("post_id") if bc else disk_state.get("post_id", 0)) or 0
        broadcast_status = bc.summary() if bc else "Аккаунт остановлен — запусти его для управления рассылкой."
        interval = int(cfg.get("broadcast_interval_min", 60))
        hours = float(cfg.get("broadcast_hours", 12))
        delay_min = float(cfg.get("broadcast_delay_min", 0.6))
        delay_max = float(cfg.get("broadcast_delay_max", 1.2))
        excluded = len(cfg.get("broadcast_exclude", []))
        auto_sub_started = len(bc.started_bots) if bc else 0
        auto_sub_pending = len(bc.pending_invites) if bc else 0
        queue_counts = bc.autosub_queue.counts() if bc else {}
        queue_active = sum(queue_counts.get(k, 0) for k in ("queued", "retry", "waiting_approval", "processing"))
        queue_review = sum(queue_counts.get(k, 0) for k in ("needs_review", "account_paused"))
        pause_until, pause_reason = bc.autosub_queue.pause_info() if bc else (None, "")
        text = (
            f"📢 РАССЫЛКА · {cfg.get('account_label') or name}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{broadcast_status}\n\n"
            "⚙️ ТЕКУЩИЕ НАСТРОЙКИ\n"
            f"├ Интервал между волнами: {interval} мин\n"
            f"├ Время работы кампании: {hours:g} ч\n"
            f"├ Пауза между чатами: {delay_min:g}–{delay_max:g} сек\n"
            f"├ Исключено чатов: {excluded}\n"
            f"├ Автозапуск: {'🟢 да' if cfg.get('broadcast_autostart') else '⚪ нет'}\n"
            f"├ Автоподписка: {'🟢 включена' if cfg.get('broadcast_auto_subscribe', True) else '⚪ выключена'}\n"
            f"├ Запусков ботов / referral-токенов: {auto_sub_started}\n"
            f"├ Заявок в закрытые чаты на одобрении: {auto_sub_pending}\n"
            f"├ Действий автоподписки в очереди: {queue_active}\n"
            f"├ Требуют проверки: {queue_review}\n"
            f"└ Отчёты по волнам: {'🟢 включены' if cfg.get('broadcast_notify_waves', True) else '⚪ выключены'}\n\n"
            f"📝 Пост из личного Избранного: {'ID ' + str(post_id) if post_id else 'не выбран — будет использован последний'}\n"
            "Автоподписка слушает сообщения ботов с упоминанием аккаунта независимо от состояния рекламной кампании. "
            "Публичные чаты, приватные invite-ссылки и /start referral-параметры попадают в отдельную постоянную очередь. "
            "Секретные invite-хэши хранятся в закрытой локальной БД профиля и не включаются в резервные копии."
        )
        if bc and bc.running:
            run_button = B("⏹ Остановить кампанию", f"abstop:{name}".encode(), style="danger")
        elif bc:
            run_button = B("▶️ Запустить кампанию", f"abgo:{name}".encode(), style="success")
        else:
            run_button = B("▶️ Запустить аккаунт", f"acc:start:{name}".encode(), style="success")
        rows = [
            [run_button],
            [B("📝 Выбрать пост из Избранного", f"abposts:{name}".encode(), style="primary")],
            [B("💬 Настроить получателей", f"abch:{name}:0".encode())],
            [B(f"⏱ Интервал · {interval} мин", f"abnum:{name}:i".encode()),
             B(f"⌛ Длительность · {hours:g} ч", f"abnum:{name}:h".encode())],
            [B(f"⏳ Пауза мин. · {delay_min:g} с", f"abnum:{name}:dmin".encode()),
             B(f"⏳ Пауза макс. · {delay_max:g} с", f"abnum:{name}:dmax".encode())],
            [B(f"🔁 Автозапуск · {'✅' if cfg.get('broadcast_autostart') else '❌'}", f"abauto:{name}".encode()),
             B(f"🤖 Автоподписка · {'✅' if cfg.get('broadcast_auto_subscribe', True) else '❌'}", f"absub:{name}".encode())],
            [B(f"📊 Отчёты · {'✅' if cfg.get('broadcast_notify_waves', True) else '❌'}", f"abnotify:{name}".encode()),
             B("📈 История", f"abhist:{name}".encode())],
            [B(f"🤖 Очередь автоподписки · {queue_active}", f"abqueue:{name}".encode())],
            [B("⚙️ Настройки автоподписки", f"abautoset:{name}".encode())],
            [B("◀️ Карточка аккаунта", f"acc:detail:{name}".encode())],
        ]
        return text, rows

    async def account_saved_posts_view(self, name: str):
        B = _ui_button
        bc = self.account_manager.get_broadcaster(name) if self.account_manager else None
        if not bc:
            return ("Сначала запусти этот аккаунт, чтобы получить сообщения из его собственного «Избранного».",
                    [[B("▶️ Запустить аккаунт", f"acc:start:{name}".encode())],
                     [B("◀️ Назад", f"ab:{name}".encode())]])
        posts = await bc.saved_posts(limit=10)
        selected_id = int(bc.state.get("post_id") or 0)
        lines = [f"🗂 Последние 10 сообщений из «Избранного» · {name}", "",
                 "Нажми на сообщение, которое этот аккаунт будет использовать в своей рассылке.", ""]
        if selected_id:
            selected_msg = await bc.client.get_messages("me", ids=selected_id)
            if selected_msg:
                selected_body = (getattr(selected_msg, "message", None) or
                                 getattr(selected_msg, "text", None) or "").strip()
                lines.extend(["👁 ПРЕДПРОСМОТР ВЫБРАННОГО ПОСТА", selected_body[:900] or "(без текста)"])
                if getattr(selected_msg, "media", None):
                    lines.append("📎 К посту прикреплено медиа")
                lines.append("")
            else:
                lines.extend(["⚠️ Ранее выбранный пост больше не найден в Избранном.", ""])
        buttons = []
        if not posts:
            lines.append("«Избранное» этого аккаунта пустое.")
        for msg in posts:
            body = (getattr(msg, "message", None) or getattr(msg, "text", None) or "").replace("\n", " ").strip()
            media = bool(getattr(msg, "media", None))
            kind = "📎 медиа" if media else "текст"
            if not body:
                body = kind
            snippet = body[:52] + ("…" if len(body) > 52 else "")
            date = getattr(msg, "date", None)
            date_label = date.strftime("%d.%m %H:%M") if date else "без даты"
            selected = int(msg.id) == selected_id
            lines.append(f"{'✅' if selected else '▫️'} ID {msg.id} · {date_label} · {snippet}")
            button_label = f"{'✅' if selected else '▫️'} {date_label} · {snippet}"[:60]
            buttons.append([B(button_label, f"abp:{name}:{int(msg.id)}".encode())])
        buttons += [[B("🔄 Обновить список", f"abposts:{name}".encode())],
                    [B("◀️ К рассылке", f"ab:{name}".encode())]]
        return "\n".join(lines), buttons

    async def account_broadcast_chats_view(self, name: str, page: int = 0):
        B = _ui_button
        bc = self.account_manager.get_broadcaster(name) if self.account_manager else None
        if not bc:
            return self.account_broadcast_view(name)
        chats = await bc.chat_list()
        pages = max(1, (len(chats) + self.PAGE - 1) // self.PAGE)
        page = min(max(page, 0), pages - 1)
        cfg = self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))
        excluded = {int(x) for x in cfg.get("broadcast_exclude", [])}
        blocked = sum(1 for cid, _ in chats if bc.chat_status(cid) == "ban")
        included = sum(1 for cid, _ in chats if cid not in excluded and bc.chat_status(cid) != "ban")
        text = (
            f"💬 ПОЛУЧАТЕЛИ · {cfg.get('account_label') or name}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Всего групп: {len(chats)}\n"
            f"🟢 Включено: {included}\n"
            f"🚫 Исключено вручную: {len(excluded)}\n"
            f"⚠️ Ранее недоступно: {blocked}\n\n"
            "Все группы включены автоматически — роль администратора не требуется. "
            "Если Telegram не разрешает публикацию, конкретная отправка будет пропущена. "
            "Нажми на группу, чтобы исключить её или вернуть."
        )
        buttons = []
        for cid, title in chats[page * self.PAGE:(page + 1) * self.PAGE]:
            status = bc.chat_status(cid)
            marker = {"ok": "🟢", "ex": "🚫", "ban": "⚠️"}.get(status, "⚪")
            buttons.append([B(f"{marker} {title}"[:60], f"abt:{name}:{cid}:{page}".encode())])
        nav = []
        if page > 0:
            nav.append(B("◀️", f"abch:{name}:{page-1}".encode()))
        nav.append(B(f"{page+1}/{pages}", b"noop"))
        if page < pages - 1:
            nav.append(B("▶️", f"abch:{name}:{page+1}".encode()))
        buttons += [nav,
                    [B("✅ Все группы + повторить недоступные", f"abadm:{name}:{page}".encode(), style="success")],
                    [B("🔄 Обновить список", f"abch:{name}:{page}:refresh".encode()),
                     B("◀️ Назад", f"ab:{name}".encode())]]
        return text, buttons

    def account_broadcast_history_view(self, name: str):
        B = _ui_button
        if not self.account_manager:
            return ("История недоступна.", [[B("◀️ Назад", b"acc:list")]])
        bc = self.account_manager.get_broadcaster(name)
        cfg = self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))
        if bc:
            st = bc.state
        else:
            state_path = data_dir(cfg) / "broadcast_state.json"
            try:
                st = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            except Exception:
                st = {}
        rows = st.get("history", [])[-8:][::-1]
        lines = [f"#{h.get('n')} {fmt_ts(cfg, h.get('t', 0))} ✅{h.get('ok', 0)} ❌{h.get('fail', 0)}"
                 + (f" ⏭{h.get('skipped', 0)}" if h.get('skipped') else "") for h in rows]
        text = "📈 История рассылки · " + name + "\n\n" + ("\n".join(lines) if lines else "Волн пока нет.")
        if not bc:
            text += "\n\nАккаунт сейчас остановлен; показана сохранённая история."
        return text, [[B("🔄 Обновить", f"abhist:{name}".encode())], [B("◀️ Назад", f"ab:{name}".encode())]]

    async def handle_account_text(self, text: str):
        action = self.pending_action or {}
        kind = action.get("type")
        name = action.get("profile", "")
        manager = self.account_manager
        if kind == "broadcast_num":
            cancel = [[_ui_button("❌ Отмена", f"ab:{name}".encode())]]
        elif kind == "autosub_num":
            cancel = [[_ui_button("❌ Отмена", f"abautoset:{name}".encode())]]
        elif kind in {"catalog_search", "catalog_threshold"}:
            profile = action.get("profile", "default")
            cancel = [[_ui_button("❌ Отмена", f"catcleanup:{profile}".encode())]]
        else:
            cancel = [[_ui_button("❌ Отмена", b"acc:list")]]
        if kind == "catalog_search":
            profile = action.get("profile", "default")
            query = text.strip()[:80]
            self._catalog_state(profile)["query"] = query
            self._catalog_state(profile)["page"] = 0
            self.pending_action = None
            return self.catalog_view(profile, 0)
        if kind == "catalog_threshold":
            profile = action.get("profile", "default")
            key = action.get("key")
            try:
                value = int(text.strip())
                if key == "min" and not 0 <= value <= 100000:
                    raise ValueError
                if key == "days" and not 1 <= value <= 3650:
                    raise ValueError
            except ValueError:
                hint = "0–100000 медиафайлов" if key == "min" else "1–3650 дней"
                return f"Нужно целое число в диапазоне {hint}.", cancel
            profile_cfg = self._catalog_profile(profile)
            config_key = "circles_catalog_min_count" if key == "min" else "circles_catalog_stale_days"
            profile_cfg[config_key] = value
            save_cfg(profile_cfg)
            if profile == "default":
                self.cfg[config_key] = value
            elif manager and profile in manager.configs:
                manager.configs[profile][config_key] = value
            self.pending_action = None
            return self.catalog_cleanup_view(profile)
        if not manager and kind != "autosub_num":
            self.pending_action = None
            return "Управление аккаунтами сейчас недоступно.", cancel

        if kind == "name":
            try:
                manager.add_profile(text.strip())
            except Exception as e:
                return f"Не удалось создать профиль: {e}\nОтправь другое короткое имя (латиница, цифры, . _ -).", cancel
            name = text.strip()
            self.pending_action = {"type": "phone", "profile": name}
            return (f"✅ Профиль «{name}» создан.\n\nТеперь отправь номер телефона второго Telegram-аккаунта с кодом страны, например +79991234567.\n\nКод и пароль 2FA не сохраняются в файлах.", cancel)

        if kind == "phone":
            state, message = await manager.begin_login(name, text.strip())
            if state == "code":
                self.pending_action = {"type": "code", "profile": name}
            elif state == "done":
                self.pending_action = None
            return message, (cancel if state != "done" else self.account_detail_view(name)[1])

        if kind == "code":
            state, message = await manager.submit_code(name, text.strip())
            if state == "password":
                self.pending_action = {"type": "password", "profile": name}
            elif state == "done" or state == "expired":
                self.pending_action = None
            return message, (cancel if state != "done" else self.account_detail_view(name)[1])

        if kind == "password":
            state, message = await manager.submit_password(name, text)
            if state == "done" or state == "expired":
                self.pending_action = None
            return message, (cancel if state != "done" else self.account_detail_view(name)[1])

        if kind == "autosub_num":
            key = action.get("key")
            try:
                raw = text.strip().replace(",", ".")
                ranges = {
                    "broadcast_auto_subscribe_gap_seconds": (2, 3600, float),
                    "broadcast_auto_subscribe_daily_limit": (1, None, int),
                    "broadcast_auto_subscribe_max_per_message": (1, 100, int),
                    "broadcast_auto_subscribe_batch_size": (1, 50, int),
                    "broadcast_auto_subscribe_batch_pause_minutes": (5, 1440, int),
                }
                if key not in ranges:
                    raise ValueError
                lower, upper, converter = ranges[key]
                value = converter(raw)
                if value < lower or (upper is not None and value > upper):
                    raise ValueError
            except ValueError:
                return ("Некорректное значение. Диапазоны: интервал 2–3600 сек; лимит вступлений от 1 без верхнего ограничения; "
                        "ссылок/сообщение 1–100; действий/пакет 1–50; отдых 5–1440 мин.", cancel)
            cfg = self.cfg if name == "default" else ((manager.configs.get(name) or read_profile_config(profile_config_path(name))) if manager else read_profile_config(profile_config_path(name)))
            cfg[key] = value
            save_cfg(cfg)
            if name == "default":
                self.cfg[key] = value
            elif manager and name in manager.configs:
                manager.configs[name][key] = value
            self.pending_action = None
            bc = self.bc if name == "default" else (manager.get_broadcaster(name) if manager else None)
            if bc:
                bc._autosub_wakeup.set()
            return (f"✅ Сохранено: {value}. Изменение действует для следующих действий очереди без перезапуска.",
                    self.autosub_settings_view(name)[1])

        if kind == "broadcast_num":
            key = action.get("key")
            try:
                if key == "broadcast_interval_min":
                    value = int(text.strip())
                    if not 1 <= value <= 1440:
                        raise ValueError
                elif key == "broadcast_hours":
                    value = float(text.strip().replace(",", "."))
                    if not 0.1 <= value <= 720:
                        raise ValueError
                elif key in {"broadcast_delay_min", "broadcast_delay_max"}:
                    value = float(text.strip().replace(",", "."))
                    upper = 60 if key == "broadcast_delay_min" else 120
                    if not 0 <= value <= upper:
                        raise ValueError
                    current_cfg = manager.configs.get(name) or read_profile_config(profile_config_path(name))
                    other = float(current_cfg.get("broadcast_delay_max" if key == "broadcast_delay_min" else "broadcast_delay_min", 1.2 if key == "broadcast_delay_min" else 0.6))
                    if (key == "broadcast_delay_min" and value > other) or (key == "broadcast_delay_max" and value < other):
                        raise ValueError
                else:
                    raise ValueError
            except ValueError:
                return ("Некорректное значение. Интервал: 1–1440 мин; длительность: 0.1–720 ч; "
                        "минимальная пауза: 0–60 сек и не больше максимальной; "
                        "максимальная пауза: 0–120 сек и не меньше минимальной.", cancel)
            manager.set_broadcast_number(name, key, value)
            self.pending_action = None
            note = ({"broadcast_interval_min": " Интервал обновлён; текущая пауза пересчитана, если аккаунт уже ожидал следующую волну.",
                     "broadcast_hours": " Длительность обновлена с момента изменения; текущая отправка завершится перед остановкой.",
                     "broadcast_delay_min": " Минимальная пауза будет применена в следующих отправках.",
                     "broadcast_delay_max": " Максимальная пауза будет применена в следующих отправках."}[key])
            return f"✅ Настройка сохранена: {value}.{note}", self.account_broadcast_view(name)[1]

        if kind == "target":
            try:
                chat_id = int(text.strip())
            except ValueError:
                return "Нужно отправить числовой ID группы, например -1001234567890.", cancel
            was_running = manager.is_running(name)
            if was_running:
                stopped, stop_note = await manager.stop_profile(name)
                if not stopped:
                    return stop_note, cancel
            manager.set_target(name, chat_id)
            message = f"✅ ID группы архива сохранён: {chat_id}."
            if was_running:
                ok, note = await manager.start_profile(name)
                message += "\n" + note
            self.pending_action = None
            return message, self.account_detail_view(name)[1]

        self.pending_action = None
        return "Неизвестный шаг. Открой список аккаунтов и начни заново.", cancel

    CATALOG_PAGE = 6

    def _catalog_state(self, profile):
        return self.catalog_states.setdefault(profile, {
            "filter": "all", "sort": "recent", "page": 0, "query": ""})

    def _catalog_profile(self, profile):
        if profile == "default":
            return self.cfg
        if not self.account_manager or profile not in self.account_manager.names():
            raise ValueError("Профиль не найден")
        return self.account_manager.configs.get(profile) or read_profile_config(profile_config_path(profile))

    def _catalog_db(self, profile):
        """Return (DB, owned temporary DB, profile config); stopped profiles are browsable read-only."""
        cfg = self._catalog_profile(profile)
        if profile == "default":
            return self.db, None, cfg
        if self.account_manager:
            running_archiver = self.account_manager.archivers.get(profile)
            if running_archiver:
                return running_archiver.db, None, cfg
        db_path = data_dir(cfg) / "circles.db"
        if not db_path.exists():
            return None, None, cfg
        db = DB(db_path, readonly=True)
        return db, db, cfg

    @staticmethod
    def _catalog_dt(value):
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def _catalog_age_days(self, row):
        activity = self._catalog_dt(row[6]) or self._catalog_dt(row[8])
        if not activity:
            return None
        return max(0, int((datetime.now(timezone.utc) - activity).total_seconds() // 86400))

    def _catalog_rows(self, profile):
        db, owned, cfg = self._catalog_db(profile)
        if db is None:
            return [], cfg
        try:
            rows = db.catalog_rows()
        finally:
            if owned:
                owned.close()
        return rows, cfg

    def _catalog_filter(self, rows, state, cfg):
        query = str(state.get("query", "")).strip().casefold()
        selected = state.get("filter", "all")
        min_count = max(0, int(cfg.get("circles_catalog_min_count", 5)))
        stale_days = max(1, int(cfg.get("circles_catalog_stale_days", 30)))
        now = datetime.now(timezone.utc)
        out = []
        for row in rows:
            uid, title, topic_id, count, sent, pending, last, first, created, duration = row
            if query and query not in str(title or "").casefold() and query not in str(uid):
                continue
            age = self._catalog_age_days(row)
            if selected == "small" and int(count) >= min_count:
                continue
            if selected == "stale" and (age is None or age < stale_days):
                continue
            if selected == "ignored" and uid not in (cfg.get("ignore_users") or []):
                continue
            if selected == "pending" and int(pending or 0) <= 0:
                continue
            out.append(row)
        sort_mode = state.get("sort", "recent")
        if sort_mode == "few":
            out.sort(key=lambda r: (int(r[3]), self._catalog_dt(r[6]) or datetime.min.replace(tzinfo=timezone.utc)))
        elif sort_mode == "oldest":
            out.sort(key=lambda r: self._catalog_dt(r[6]) or self._catalog_dt(r[8]) or datetime.min.replace(tzinfo=timezone.utc))
        elif sort_mode == "name":
            out.sort(key=lambda r: str(r[1] or "").casefold())
        else:
            out.sort(key=lambda r: self._catalog_dt(r[6]) or self._catalog_dt(r[8]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out

    def catalog_view(self, profile="default", page=0):
        B = _ui_button
        try:
            rows, cfg = self._catalog_rows(profile)
        except Exception as exc:
            return (f"Не удалось открыть каталог: {type(exc).__name__}", [[B("◀️ Назад", f"acc:detail:{profile}".encode())]])
        state = self._catalog_state(profile)
        state["page"] = max(0, int(page))
        filtered = self._catalog_filter(rows, state, cfg)
        pages = max(1, (len(filtered) + self.CATALOG_PAGE - 1) // self.CATALOG_PAGE)
        state["page"] = min(state["page"], pages - 1)
        page = state["page"]
        min_count = max(0, int(cfg.get("circles_catalog_min_count", 5)))
        stale_days = max(1, int(cfg.get("circles_catalog_stale_days", 30)))
        few_count = sum(1 for r in rows if int(r[3]) < min_count)
        stale_count = sum(1 for r in rows if (self._catalog_age_days(r) or 0) >= stale_days)
        ignored_count = sum(1 for r in rows if r[0] in (cfg.get("ignore_users") or []))
        label = cfg.get("account_label") or profile
        sort_label = {"recent": "сначала новые", "oldest": "сначала старые", "few": "меньше медиа", "name": "по имени"}.get(state["sort"], "сначала новые")
        filt_label = {"all": "Все", "small": f"Меньше {min_count}", "stale": f"Старше {stale_days} дн.", "ignored": "В игноре", "pending": "Не доставлены"}.get(state["filter"], "Все")
        text = (
            f"🎞 КАТАЛОГ МЕДИА · {label}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👥 Тем: {len(rows)} · 🔎 Найдено: {len(filtered)}\n"
            f"🫧 Меньше {min_count} файлов: {few_count} · 🕒 Неактивны {stale_days}+ дн.: {stale_count} · 🚫 Игнор: {ignored_count}\n"
            f"Фильтр: {filt_label} · Сортировка: {sort_label}\n"
        )
        if state.get("query"):
            text += f"Поиск: «{state['query'][:60]}»\n"
        if not filtered:
            text += "\nНичего не найдено. Измени фильтр или поисковый запрос."
        else:
            text += "\nНажми на тему, чтобы открыть карточку и действия.\n"
        buttons = [
            [B("🔎 Поиск по имени или ID", f"catq:{profile}".encode()),
             B("🧹 Очистка", f"catcleanup:{profile}".encode(), style="danger")],
            [B("📚 Все", f"catf:{profile}:all:0".encode()),
             B(f"🫧 <{min_count}", f"catf:{profile}:small:0".encode()),
             B(f"🕒 {stale_days}+д", f"catf:{profile}:stale:0".encode())],
            [B("🟠 Не доставлены", f"catf:{profile}:pending:0".encode()),
             B("🚫 Игнор", f"catf:{profile}:ignored:0".encode()),
             B("↕️ Сортировка", f"cats:{profile}:next:0".encode())],
        ]
        for row in filtered[page * self.CATALOG_PAGE:(page + 1) * self.CATALOG_PAGE]:
            uid, title, topic_id, count, sent, pending, last, _first, _created, _duration = row
            ignored = uid in (cfg.get("ignore_users") or [])
            age = self._catalog_age_days(row)
            last_label = f"{age}д" if age is not None else "нет даты"
            mark = "🚫" if ignored else ("🟠" if pending else "✅")
            label_text = f"{mark} {str(title or uid)[:30]} · {count} медиа · {last_label}"
            buttons.append([B(label_text[:60], f"catitem:{profile}:{uid}:{page}".encode())])
        nav = []
        if page > 0:
            nav.append(B("◀️", f"cat:{profile}:{page - 1}".encode()))
        nav.append(B(f"{page + 1}/{pages}", b"noop"))
        if page < pages - 1:
            nav.append(B("▶️", f"cat:{profile}:{page + 1}".encode()))
        buttons.append(nav)
        if state.get("query"):
            buttons.append([B("✖️ Сбросить поиск", f"catclear:{profile}".encode())])
        back = b"main" if profile == "default" else f"acc:detail:{profile}".encode()
        buttons.append([B("◀️ Назад", back)])
        return text, buttons

    def catalog_detail_view(self, profile, user_id, page=0):
        B = _ui_button
        try:
            db, owned, cfg = self._catalog_db(profile)
            row = db.catalog_detail(user_id) if db else None
        except Exception as exc:
            row, owned = None, None
            error = type(exc).__name__
        else:
            error = None
        finally:
            if 'owned' in locals() and owned:
                owned.close()
        if error:
            return (f"Ошибка чтения карточки: {error}", [[B("◀️ Каталог", f"cat:{profile}:{page}".encode())]])
        if not row:
            return ("Тема уже удалена из каталога или отсутствует.", [[B("◀️ Каталог", f"cat:{profile}:{page}".encode())]])
        uid, title, topic_id, count, sent, pending, last, first, created, duration = row
        ignored = uid in (cfg.get("ignore_users") or [])
        age = self._catalog_age_days(row)
        age_text = f"{age} дней назад" if age is not None else "неизвестно"
        total_duration = int(duration or 0)
        text = (
            f"👤 КАРТОЧКА ТЕМЫ\n━━━━━━━━━━━━━━━━━━\n"
            f"Имя: {title or uid}\nID пользователя: {uid}\nID темы: {topic_id}\n\n"
            f"🎞 Всего медиафайлов: {count}\n✅ Отправлено в архив: {sent}\n"
            f"🟠 Не доставлено: {pending}\n"
            f"🕒 Последнее медиа: {fmt_ts(cfg, last) if last else 'нет данных'}\n"
            f"📅 Тема создана: {created or 'неизвестно'}\n"
            f"⌛ Общая длительность: {total_duration // 60} мин {total_duration % 60} сек\n"
            f"⏳ Последняя активность: {age_text}\n"
            f"Игнор-лист: {'да' if ignored else 'нет'}\n\n"
            "⚠️ Удаление темы удалит всю историю сообщений этой темы в архивной группе. "
            "Нужны права manage_topics. Локальные медиафайлы на VPS останутся."
        )
        return text, [
            [B("🎞 Последние медиа", f"catvid:{profile}:{uid}:{page}".encode())],
            [B("🚫 Убрать из архива" if not ignored else "✅ Возобновить сохранение", f"catig:{profile}:{uid}:{page}".encode())],
            [B("🗑 Удалить тему и её сообщения", f"catone:{profile}:{uid}:{page}".encode(), style="danger")],
            [B("◀️ К каталогу", f"cat:{profile}:{page}".encode())],
        ]

    def catalog_circles_view(self, profile, user_id, page=0):
        B = _ui_button
        owned = None
        try:
            db, owned, cfg = self._catalog_db(profile)
            topic = db.catalog_detail(user_id) if db else None
            items = db.topic_circles(user_id, 12) if db and topic else []
        except Exception as exc:
            if owned:
                owned.close()
            return (f"Не удалось открыть медиа: {type(exc).__name__}",
                    [[B("◀️ К теме", f"catitem:{profile}:{user_id}:{page}".encode())]])
        finally:
            if owned:
                owned.close()
        if not topic:
            return ("Тема отсутствует в каталоге.", [[B("◀️ Каталог", f"cat:{profile}:{page}".encode())]])
        lines = [f"🎞 ПОСЛЕДНИЕ МЕДИА · {str(topic[1] or user_id)[:36]}",
                 f"Всего: {topic[3]} · Отправлено: {topic[4]} · Не доставлено: {topic[5]}", ""]
        if not items:
            lines.append("Записей о медиа пока нет.")
        else:
            for msg_id, date_utc, duration, outgoing, sent, attempts, file_path, media_type, mime_type, file_size in items:
                timestamp = fmt_ts(cfg, date_utc) if date_utc else "дата неизвестна"
                direction = "исходящий" if outgoing else "входящий"
                status = "✅ в архиве" if sent else f"🟠 не доставлен · попыток {attempts}"
                local = " · 📁 копия на VPS" if file_path else ""
                kind = ARCHIVE_MEDIA_LABELS.get(media_type, "Медиа")
                size = f" · {human(int(file_size or 0))}" if file_size else ""
                length = f" · {fmt_dur(duration or 0)}" if duration else ""
                lines.append(f"• {kind} · {timestamp}{length}{size} · {direction} · {status}{local} · ID {msg_id}")
        return "\n".join(lines), [
            [B("🔄 Обновить", f"catvid:{profile}:{user_id}:{page}".encode())],
            [B("◀️ К карточке темы", f"catitem:{profile}:{user_id}:{page}".encode())],
        ]

    def catalog_cleanup_view(self, profile):
        B = _ui_button
        try:
            rows, cfg = self._catalog_rows(profile)
        except Exception as exc:
            return (f"Не удалось открыть очистку: {type(exc).__name__}", [[B("◀️ Каталог", f"cat:{profile}:0".encode())]])
        min_count = max(0, int(cfg.get("circles_catalog_min_count", 5)))
        stale_days = max(1, int(cfg.get("circles_catalog_stale_days", 30)))
        few_raw = [r for r in rows if int(r[3]) < min_count]
        stale_raw = [r for r in rows if (self._catalog_age_days(r) is not None and self._catalog_age_days(r) >= stale_days)]
        few = [r for r in rows if self._catalog_candidate(r, "small", cfg)]
        stale = [r for r in rows if self._catalog_candidate(r, "stale", cfg)]
        protected_pending = sum(1 for r in rows if int(r[5] or 0) > 0 and
                                (int(r[3] or 0) < min_count or
                                 (self._catalog_age_days(r) is not None and self._catalog_age_days(r) >= stale_days)))
        protected_general = sum(1 for r in rows if int(r[2] or 0) <= 1 and
                                (int(r[3] or 0) < min_count or
                                 (self._catalog_age_days(r) is not None and self._catalog_age_days(r) >= stale_days)))
        text = (
            "🧹 ОЧИСТКА КАТАЛОГА\n━━━━━━━━━━━━━━━━━━\n"
            "Удаление удаляет всю тему и её сообщения из Telegram-группы архива. "
            "Для этого у аккаунта должны быть права управления темами. Локальные медиафайлы на VPS не удаляются.\n\n"
            f"🫧 Меньше {min_count} медиафайлов: {len(few)} кандидатов из {len(few_raw)}\n"
            f"🕒 Без новых медиа {stale_days}+ дней: {len(stale)} кандидатов из {len(stale_raw)}\n"
            f"🛡 Защищено: {protected_pending} тем с недоставленными файлами, {protected_general} системных тем\n\n"
            "Сначала будет показан список кандидатов. Удаление выполняется только после второго подтверждения."
        )
        return text, [
            [B(f"🫧 Показать темы с < {min_count}", f"catplan:{profile}:small".encode())],
            [B(f"🕒 Показать темы без обновлений {stale_days}+ дней", f"catplan:{profile}:stale".encode())],
            [B("🧹 Любое из двух условий", f"catplan:{profile}:either".encode())],
            [B("⚙️ Порог медиа", f"catthr:{profile}:min".encode()), B("⚙️ Дней без обновления", f"catthr:{profile}:days".encode())],
            [B("🧾 История очистки", f"catlog:{profile}".encode())],
            [B("◀️ Каталог", f"cat:{profile}:0".encode())],
        ]

    def catalog_cleanup_history_view(self, profile):
        B = _ui_button
        try:
            db, owned, cfg = self._catalog_db(profile)
            history = db.topic_cleanup_history(15) if db else []
        except Exception as exc:
            history, owned = [], None
            error = type(exc).__name__
        else:
            error = None
        finally:
            if 'owned' in locals() and owned:
                owned.close()
        if error:
            text = f"Не удалось прочитать журнал очистки: {error}"
        elif not history:
            text = "🧾 ЖУРНАЛ ОЧИСТКИ\n\nЗаписей пока нет."
        else:
            lines = ["🧾 ЖУРНАЛ ОЧИСТКИ", ""]
            icons = {"deleted": "✅", "already_missing": "ℹ️", "failed": "❌"}
            for uid, topic_id, title, count, criterion, result, err, created in history:
                line = f"{icons.get(result, '•')} {str(title or uid)[:28]} · {count} медиа · {created}"
                if err:
                    line += f" · {err}"
                lines.append(line[:150])
            text = "\n".join(lines)
        return text, [[B("🔄 Обновить", f"catlog:{profile}".encode())],
                      [B("◀️ Очистка", f"catcleanup:{profile}".encode())]]

    def _catalog_candidate(self, row, mode, cfg):
        uid, title, topic_id, count, sent, pending, last, first, created, duration = row
        # topic_id=1 is Telegram's protected General topic; never include it in cleanup.
        if int(pending or 0) > 0 or int(topic_id or 0) <= 1:
            return False
        min_count = max(0, int(cfg.get("circles_catalog_min_count", 5)))
        stale_days = max(1, int(cfg.get("circles_catalog_stale_days", 30)))
        age = self._catalog_age_days(row)
        small = int(count or 0) < min_count
        stale = age is not None and age >= stale_days
        if mode == "small":
            return small
        if mode == "stale":
            return stale
        if mode == "either":
            return small or stale
        if mode == "one":
            return True
        return False

    def catalog_plan_view(self, profile):
        B = _ui_button
        plan = self.catalog_cleanup_plans.get(profile)
        if not plan:
            return ("План очистки не найден. Сформируй его заново.", [[B("◀️ Очистка", f"catcleanup:{profile}".encode())]])
        items = plan["items"]
        protected = int(plan.get("protected", 0))
        lines = ["🔎 ПРЕДПРОСМОТР УДАЛЕНИЯ", "", f"Кандидатов: {len(items)}", f"Защищено по правилам безопасности: {protected}", ""]
        for item in items[:12]:
            lines.append(f"• {str(item['title'])[:36]} — {item['count']} медиа, ID {item['user_id']}")
        if len(items) > 12:
            lines.append(f"… и ещё {len(items) - 12}")
        lines.extend(["", "⚠️ Подтверждение удалит каждую выбранную тему вместе со всеми сообщениями в архивной Telegram-группе. Локальные файлы останутся на VPS."])
        if not items:
            lines.append("\nПодходящих тем нет. Ничего не удалено.")
            return "\n".join(lines), [[B("◀️ К очистке", f"catcleanup:{profile}".encode())]]
        return "\n".join(lines), [
            [B(f"🗑 Удалить {len(items)} тем", f"catdel:{profile}:go".encode(), style="danger")],
            [B("❌ Отмена", f"catcleanup:{profile}".encode())],
        ]

    async def catalog_delete_plan(self, profile):
        plan = self.catalog_cleanup_plans.get(profile)
        if not plan:
            return "План истёк — сформируй предварительный список заново."
        cfg = self._catalog_profile(profile)
        if profile == "default":
            arch = self.arch
        else:
            arch = self.account_manager.archivers.get(profile) if self.account_manager else None
        if arch is None:
            return "Для удаления Telegram-тем профиль должен быть запущен. Запусти аккаунт и повтори действие."
        if arch.target is None:
            return "Группа архива не подключена. Проверь ID целевой группы в настройках профиля."
        db = arch.db
        removed = failed = skipped = 0
        items = list(plan["items"])
        criterion = plan.get("mode", "unknown")
        remaining = []
        last_error = None
        for index, item in enumerate(items):
            try:
                # The same lock is used by Archiver.process, so no in-flight circle
                # can be written to a topic between the freshness recheck and deletion.
                async with arch.lock:
                    current = db.catalog_detail(item["user_id"])
                    if not current or int(current[2]) != int(item["topic_id"]):
                        skipped += 1
                        continue
                    if not self._catalog_candidate(current, criterion, cfg):
                        skipped += 1
                        continue
                    uid, title, topic_id, count, sent, pending, last, first, created, duration = current
                    try:
                        await arch.delete_archive_topic(topic_id)
                    except Exception as exc:
                        error_name = type(exc).__name__
                        error_text = str(exc)
                        marker = (error_name + " " + error_text).upper()
                        if "TOPIC_ID_INVALID" in marker or "TOPICIDINVALID" in marker:
                            db.record_topic_cleanup(uid, topic_id, title, count, last, criterion, "already_missing")
                            db.drop_topic(uid)
                            removed += 1
                        else:
                            db.record_topic_cleanup(uid, topic_id, title, count, last, criterion,
                                                    "failed", f"{error_name}: {error_text}")
                            failed += 1
                            if isinstance(exc, errors.FloodWaitError):
                                remaining = items[index:]
                                last_error = (f"Telegram попросил подождать {exc.seconds} сек. "
                                              "Очистка остановлена без потери оставшейся очереди.")
                                break
                            compact_marker = re.sub(r"[^A-Z0-9]", "", marker)
                            if any(code in compact_marker for code in (
                                "CHATWRITEFORBIDDEN", "CHATADMINREQUIRED", "RIGHTFORBIDDEN",
                                "MANAGETOPICSREQUIRED")):
                                remaining = items[index:]
                                last_error = ("У аккаунта нет права manage_topics в группе архива. "
                                              "Очистка остановлена; выдай право управления темами и повтори.")
                                break
                    else:
                        db.record_topic_cleanup(uid, topic_id, title, count, last, criterion, "deleted")
                        db.drop_topic(uid)
                        removed += 1
                await asyncio.sleep(0.25)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                last_error = f"Внутренняя ошибка: {type(exc).__name__}: {exc}"
                remaining = items[index:]
                break
        if remaining:
            self.catalog_cleanup_plans[profile]["items"] = remaining
        else:
            self.catalog_cleanup_plans.pop(profile, None)
        result = (f"Очистка: удалено {removed} · ошибок {failed} · "
                  f"пропущено после перепроверки {skipped}. Локальные медиафайлы не тронуты.")
        if last_error:
            result += "\n" + last_error + f" Осталось в очереди: {len(remaining)}."
        return result

    def people_view(self, page=0, rows=None):
        # Backward-compatible entry point; the new catalogue replaces the old people list.
        return self.catalog_view("default", page)

    @staticmethod
    def _tail(p: Path, n: int) -> list[str]:
        """Последние n строк файла, читая только хвост (лог бывает до 5 МБ)."""
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 24 * 1024))
                data = f.read()
            return data.decode("utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def log_view(self, kind="b"):
        B = _ui_button
        if kind == "e":
            lines = [f"{fmt_ts(self.cfg, t, '%d.%m %H:%M:%S')} {lvl[:4]} {msg}"
                     for t, lvl, _n, msg in list(RECENT_ERRORS)[-15:]]
            body = "\n".join(lines)[-3500:] or "Предупреждений и ошибок нет 👍"
            title = "⚠️ Последние ошибки"
        else:
            name = "broadcast.log" if kind == "b" else "circles.log"
            p = self.ddir / name
            lines = self._tail(p, 25)
            body = "\n".join(lines)[-3500:] or "Лог пока пуст."
            title = "📜 Лог рассылки" if kind == "b" else "📜 Лог архива"
        tabs = [B("📢 Рассылка", b"log:b"), B("🎞 Архив", b"log:c"), B("⚠️ Ошибки", b"log:e")]
        acts = [B("🔄 Обновить", f"log:{kind}".encode())]
        if kind in ("b", "c"):
            acts.append(B("📎 Файлом", f"logf:{kind}".encode()))
        return f"{title}\n\n{body}", [tabs, acts, [B("◀️ Назад", b"main")]]

    async def maintenance_view(self):
        """Compact operations dashboard across all configured profiles."""
        profiles = account_profiles()
        running = 0
        active_broadcasts = 0
        lines = []
        for name, path in profiles:
            try:
                cfg = self.cfg if name == "default" else read_profile_config(path)
                ddir = data_dir(cfg)
                if name == "default":
                    is_running = bool(self.client and self.client.is_connected())
                    label = cfg.get("account_label") or "Основной"
                    bc = self.bc if self.allow_broadcast else None
                else:
                    is_running = bool(self.account_manager and self.account_manager.is_running(name))
                    label = cfg.get("account_label") or name
                    bc = self.account_manager.get_broadcaster(name) if self.account_manager else None
                if is_running:
                    running += 1
                if bc and bc.running:
                    active_broadcasts += 1
                    broadcast_label = "📢 рассылка работает"
                else:
                    state_path = ddir / "broadcast_state.json"
                    try:
                        saved_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                    except Exception:
                        saved_state = {}
                    broadcast_label = "📢 ожидает восстановления" if saved_state.get("active") else "📢 остановлена"
                marker = "🟢" if is_running else "⚪"
                lines.append(f"{marker} {label[:22]} — {'онлайн' if is_running else 'не запущен'} · {broadcast_label}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"🟠 {name}: профиль недоступен ({type(exc).__name__})")

        du = shutil.disk_usage(BASE)
        free_pct = (du.free / du.total * 100) if du.total else 0
        rss = _process_rss_mb()
        try:
            import importlib.metadata as metadata
            telethon_version = metadata.version("Telethon")
        except Exception:
            telethon_version = "не определена"
        db_check = await asyncio.to_thread(self.db.quick_check)
        db_ok = db_check.casefold() == "ok"
        backup_files = sorted((BASE / "backups").glob("circles_backup_*.zip"),
                              key=lambda item: item.stat().st_mtime, reverse=True) if (BASE / "backups").exists() else []
        last_backup = datetime.fromtimestamp(backup_files[0].stat().st_mtime).strftime("%d.%m %H:%M") if backup_files else "ещё не создавалась"

        text = (
            "🧰 ЦЕНТР ОБСЛУЖИВАНИЯ\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👥 Профилей: {len(profiles)} · 🟢 работают: {running}\n"
            f"📢 Активных рассылок: {active_broadcasts}\n"
            f"💾 Свободно: {human(du.free)} из {human(du.total)} ({free_pct:.1f}%)\n"
            f"🧠 Память процесса: {str(rss) + ' МБ' if rss is not None else 'н/д'}\n"
            f"🐍 Python: {platform.python_version()} · Telethon: {telethon_version}\n"
            f"🗄 База архива: {'✅ целостна' if db_ok else '⚠️ ' + db_check}\n"
            f"📦 Последняя копия: {last_backup}\n\n"
            "СТАТУС ПРОФИЛЕЙ\n" + ("\n".join(lines) if lines else "Нет настроенных профилей") +
            "\n\nРезервная копия не включает Telegram-сессии, токены, API HASH, логи и локальные медиафайлы."
        )
        B = _ui_button
        rows = [
            [B("🩺 Диагностика", b"diag"), B("📦 Создать копию", b"backup")],
            [B("👤 Все аккаунты", b"acc:list"), B("⚠️ Ошибки", b"log:e")],
            [B("🔄 Обновить", b"maint"), B("◀️ В меню", b"main")],
        ]
        return text, rows

    async def diag_view(self):
        res = await diagnose(self.client, self.arch, self.bc, self.cfg, self.ddir)
        try:
            db_check = await asyncio.to_thread(self.db.quick_check)
            res.append((db_check.casefold() == "ok", f"Целостность SQLite circles.db: {db_check}"))
        except Exception as exc:  # noqa: BLE001
            res.append((False, f"Проверка circles.db: {type(exc).__name__}"))
        lines = [f"{'✅' if ok else '❌'} {t}" for ok, t in res]
        bad = sum(1 for ok, _ in res if not ok)
        head = "🩺 Диагностика\n\n" + "\n".join(lines)
        head += "\n\n" + ("Всё в порядке 👍" if not bad else f"Проблем: {bad}")
        return head, [[_ui_button("🔄 Проверить снова", b"diag"), _ui_button("◀️ К обслуживанию", b"maint")]]

    def _archiver_for_profile(self, profile="default"):
        if profile == "default":
            return self.arch
        if self.account_manager:
            return self.account_manager.archivers.get(profile)
        return None

    def backfill_running(self, profile="default"):
        task = self.bf_task if profile == "default" else self.profile_bf_tasks.get(profile)
        return bool(task and not task.done())

    def backfill_view(self, profile="default"):
        B = _ui_button
        arch = self._archiver_for_profile(profile)
        back = b"main" if profile == "default" else f"acc:detail:{profile}".encode()
        media_back = f"media:{profile}".encode()
        if arch is None:
            return (f"📥 Старые медиа · {profile}\n\nЧтобы выгрузить историю этого профиля, сначала запусти аккаунт.",
                    [[B("▶️ Запустить аккаунт", f"acc:start:{profile}".encode(), style="success")],
                     [B("◀️ Назад", back)]])
        selected = [ARCHIVE_MEDIA_LABELS[k] for k in ("round_video", "photo", "video")
                    if k in arch.selected_media_types()]
        if self.backfill_running(profile):
            text = (f"📥 ВЫГРУЗКА ИСТОРИИ · {profile}\n"
                    f"Текущий чат: {arch.backfill_current or '—'}\n"
                    f"Просмотрено вложений: {arch.backfill_scanned}\n"
                    f"Успешно добавлено в архив: {arch.backfill_archived}\n"
                    f"Типы: {', '.join(selected) or 'не выбраны'}\n\n"
                    "Прогресс сохраняется отдельно для каждого типа медиа и чата.")
            stop_data = b"bf:stop" if profile == "default" else f"pbf:{profile}:stop".encode()
            return (text, [[B("🔄 Обновить", b"bf" if profile == "default" else f"pbf:{profile}".encode()),
                           B("⏹ Остановить", stop_data, style="danger")],
                    [B("🧩 Типы медиа", media_back)], [B("◀️ Назад", back)]])
        try:
            summary = arch.db.backfill_summary()
        except Exception:
            summary = {}
        summary_lines = []
        for kind in ("round_video", "photo", "video"):
            item = summary.get(kind, {})
            if item:
                summary_lines.append(
                    f"├ {ARCHIVE_MEDIA_LABELS[kind]}: просмотрено {item.get('scanned', 0)}, "
                    f"добавлено {item.get('archived', 0)}")
        text = (f"📥 СТАРЫЕ МЕДИА · {profile}\n━━━━━━━━━━━━━━━━━━\n"
                f"Будут выгружены типы: {', '.join(selected) or 'ничего не выбрано'}\n\n"
                "Будет просмотрена история личных диалогов 1:1 (группы и каналы пока не сканируются). "
                "Уже добавленные файлы пропускаются. Прогресс хранится отдельно по каждому чату и типу медиа — остановку можно продолжить позже.\n\n"
                + ("ПОСЛЕДНИЙ ПРОГРЕСС\n" + "\n".join(summary_lines) if summary_lines else "История ещё не сканировалась."))
        start_data = b"bf:go" if profile == "default" else f"pbf:{profile}:go".encode()
        return (text, [[B("▶️ Запустить / продолжить", start_data, style="success")],
                      [B("🧩 Выбрать типы медиа", media_back)],
                      [B("↻ Пересканировать историю", f"bfrst:{profile}".encode())],
                      [B("◀️ Назад", back)]])

    def start_backfill(self, profile="default"):
        arch = self._archiver_for_profile(profile)
        if arch is None:
            return False
        if self.backfill_running(profile):
            return False
        self.profile_bf_counts[profile] = 0
        arch.stop_flag = False  # reset before creating the task so an immediate Stop is not lost

        async def job():
            try:
                n = await arch.backfill(None, progress=lambda line: self._progress(line, profile))
                txt = (f"⏹ Выгрузка остановлена · {profile}. Добавлено: {n}; просмотрено: {arch.backfill_scanned}."
                       if arch.stop_flag else
                       f"✅ Выгрузка завершена · {profile}. Добавлено: {n}; просмотрено: {arch.backfill_scanned}.")
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка выгрузки истории профиля %s", profile)
                txt = f"❌ Ошибка выгрузки · {profile}: {type(exc).__name__}. Подробности в журнале."
            try:
                await self.bot.send_message(self.owner_id, txt)
            except Exception:  # noqa: BLE001
                pass

        task = asyncio.create_task(job(), name=f"media-backfill:{profile}")
        if profile == "default":
            self.bf_task = task
            self.bf_count = 0
        else:
            self.profile_bf_tasks[profile] = task
        return True

    def _progress(self, _line, profile="default"):
        self.profile_bf_counts[profile] = int(self.profile_bf_counts.get(profile, 0)) + 1
        if profile == "default":
            self.bf_count = self.profile_bf_counts[profile]

    # ----- ввод чисел текстом -----
    async def handle_text(self, text: str):
        """Ответ на сообщение владельца, когда бот ждёт значение настройки. Возвращает (текст, кнопки)."""
        key = self.pending
        title, kind, lo, hi = self.NUMS[key]
        cancel = [[_ui_button("❌ Отмена", b"set")]]
        try:
            v = kind(text.strip().replace(",", "."))
        except ValueError:
            return f"⚠️ Нужно число.\n{title}\nПопробуй ещё раз или нажми «Отмена».", cancel
        if lo is not None and not (lo <= v <= hi):
            return f"⚠️ Допустимо от {lo} до {hi}. Попробуй ещё раз.", cancel
        if key == "broadcast_delay_min" and float(v) > float(self.cfg.get("broadcast_delay_max", 1.2)):
            return "⚠️ Минимальная пауза не может быть больше максимальной.", cancel
        if key == "broadcast_delay_max" and float(v) < float(self.cfg.get("broadcast_delay_min", 0.6)):
            return "⚠️ Максимальная пауза не может быть меньше минимальной.", cancel
        self.cfg[key] = v
        save_cfg(self.cfg)
        self.pending = None
        note = ""
        if key == "broadcast_interval_min" and self.bc.running:
            self.bc.set_interval(v)
            note = "\nТекущая пауза пересчитана, если аккаунт уже ожидал следующую волну."
        elif key == "broadcast_hours" and self.bc.running:
            self.bc.set_duration(v)
            note = "\nНовая длительность отсчитывается с момента изменения; текущая отправка завершится перед остановкой."
        elif key in {"broadcast_delay_min", "broadcast_delay_max"}:
            self.bc._apply_delays()
            note = "\nНовые паузы применятся к следующим отправкам."
        elif key == "target_chat_id":
            note = "\nНажми «🩺 Диагностика» - проверю, что группа найдена."
        view = self.settings_view()
        return f"✅ Сохранено: {v}.{note}\n\n" + view[0], view[1]

    # ----- обработка нажатий -----
    async def dispatch(self, data: str):
        """Возвращает (текст, кнопки, всплывающая подсказка) или None."""
        cmd, _, arg = data.partition(":")
        if not self.allow_broadcast and (cmd in {"bc", "post", "ch", "hist"} or
                                         (cmd == "num" and arg.startswith("broadcast_"))):
            view = self.main_view()
            return view[0], view[1], "Рассылка выключена в режиме нескольких аккаунтов"
        if not self.allow_broadcast and cmd == "log" and arg == "b":
            arg = "c"
        if cmd != "num":
            self.pending = None
        if cmd != "acc":
            self.pending_action = None
        toast = ""
        if cmd == "noop":
            return None
        if cmd == "maint":
            view = await self.maintenance_view()
        elif cmd == "backup":
            if arg != "go":
                view = (
                    "📦 РЕЗЕРВНАЯ КОПИЯ\n\n"
                    "Соберу настройки профилей, SQLite-базы архива и состояние рассылок. "
                    "Базы SQLite копируются штатным online-backup API, поэтому снимок согласован с работающим процессом.\n\n"
                    "🔒 В архив НЕ попадут .session, bot token, API HASH, .env, локальные медиафайлы и журналы. "
                    "После восстановления потребуется повторно авторизовать Telegram-аккаунты.\n\n"
                    "Старые архивы автоматически ограничиваются пятью последними копиями.",
                    [[_ui_button("✅ Создать и отправить", b"backup:go", style="success")],
                     [_ui_button("◀️ Назад", b"maint")]],
                )
            else:
                try:
                    backup_path, backup_warnings, profiles_done = await asyncio.to_thread(create_safe_backup, 5)
                    size = human(backup_path.stat().st_size)
                    caption = f"📦 CIRCLES CONTROL · безопасная копия\nПрофилей: {profiles_done} · Размер: {size}\nСессии и секреты не включены."
                    await self.bot.send_file(self.owner_id, str(backup_path), caption=caption)
                    toast = "Резервная копия отправлена в этот чат" + (f" · предупреждений: {len(backup_warnings)}" if backup_warnings else "")
                except Exception as exc:  # noqa: BLE001
                    log.exception("Не удалось создать/отправить резервную копию")
                    toast = f"Не удалось отправить копию: {type(exc).__name__}"
                view = await self.maintenance_view()
        elif cmd == "pause":
            self.cfg["paused"] = not self.cfg.get("paused")
            save_cfg(self.cfg)
            toast = "Архивация на паузе" if self.cfg["paused"] else "Архивация продолжается"
            view = self.main_view()
        elif cmd == "status":
            view = await self.status_view()
        elif cmd == "media":
            profile = arg or "default"
            view = self.media_view(profile)
        elif cmd == "mt":
            profile, _, media_type = arg.partition(":")
            try:
                if media_type not in ARCHIVE_MEDIA_LABELS:
                    raise ValueError("Неизвестный тип медиа")
                profile_cfg = self._catalog_profile(profile or "default")
                selected = configured_media_types(profile_cfg)
                if media_type in selected:
                    selected.remove(media_type)
                    toast = f"Выключено: {ARCHIVE_MEDIA_LABELS[media_type]}"
                else:
                    selected.add(media_type)
                    toast = f"Включено: {ARCHIVE_MEDIA_LABELS[media_type]}"
                profile_cfg["archive_media_types"] = [k for k in ("round_video", "photo", "video") if k in selected]
                save_cfg(profile_cfg)
                if profile == "default":
                    self.cfg["archive_media_types"] = profile_cfg["archive_media_types"]
                elif self.account_manager and profile in self.account_manager.configs:
                    self.account_manager.configs[profile]["archive_media_types"] = profile_cfg["archive_media_types"]
                active_archiver = self._archiver_for_profile(profile or "default")
                if active_archiver and self.backfill_running(profile or "default"):
                    active_archiver.stop_flag = True
                    toast += ". Текущая дозагрузка остановится безопасно; запусти её снова для нового набора типов"
                view = self.media_view(profile or "default")
            except Exception as exc:
                toast = f"Не удалось сохранить: {type(exc).__name__}"
                view = self.media_view(profile or "default")
        elif cmd in {"mta", "mtc"}:
            profile = arg or "default"
            try:
                profile_cfg = self._catalog_profile(profile)
                profile_cfg["archive_media_types"] = (
                    ["round_video", "photo", "video"] if cmd == "mta" else ["round_video"]
                )
                save_cfg(profile_cfg)
                if profile == "default":
                    self.cfg["archive_media_types"] = list(profile_cfg["archive_media_types"])
                elif self.account_manager and profile in self.account_manager.configs:
                    self.account_manager.configs[profile]["archive_media_types"] = list(profile_cfg["archive_media_types"])
                active_archiver = self._archiver_for_profile(profile)
                if active_archiver and self.backfill_running(profile):
                    active_archiver.stop_flag = True
                    toast = "Выбор сохранён. Текущая дозагрузка остановится безопасно; запусти её снова."
                else:
                    toast = "Выбор типов медиа сохранён"
                view = self.media_view(profile)
            except Exception as exc:
                toast = f"Не удалось сохранить: {type(exc).__name__}"
                view = self.media_view(profile)
        elif cmd == "bfrst":
            profile = arg or "default"
            arch = self._archiver_for_profile(profile)
            if arch is None:
                toast = "Сначала запусти этот аккаунт"
                view = self.backfill_view(profile)
            elif self.backfill_running(profile):
                toast = "Нельзя сбросить курсор во время выгрузки. Сначала останови её."
                view = self.backfill_view(profile)
            else:
                selected = [k for k in ("round_video", "photo", "video") if k in arch.selected_media_types()]
                labels = ", ".join(ARCHIVE_MEDIA_LABELS[k] for k in selected) or "нет выбранных типов"
                text = (
                    f"↻ ПЕРЕСКАНИРОВАТЬ ИСТОРИЮ · {profile}\n\n"
                    f"Будут сброшены курсоры типов: {labels}.\n"
                    "Сами записи архива и отправленные в Telegram медиа не удаляются. "
                    "Уже успешно сохранённые сообщения будут распознаны по базе и не отправятся повторно.\n\n"
                    "Полная повторная проверка может занять время и вызвать дополнительную загрузку тех файлов, "
                    "которые ранее не удалось сохранить. Продолжить?"
                )
                cancel_data = b"bf" if profile == "default" else f"pbf:{profile}".encode()
                view = (text, [[_ui_button("✅ Сбросить курсоры", f"bfrstgo:{profile}".encode(), style="success")],
                               [_ui_button("❌ Отмена", cancel_data)]])
        elif cmd == "bfrstgo":
            profile = arg or "default"
            arch = self._archiver_for_profile(profile)
            if arch is None:
                toast = "Сначала запусти этот аккаунт"
            elif self.backfill_running(profile):
                toast = "Выгрузка уже запущена — курсоры не изменены"
            else:
                selected = [k for k in ("round_video", "photo", "video") if k in arch.selected_media_types()]
                count = arch.db.reset_backfill_state(selected)
                toast = f"Курсоры сброшены для {len(selected)} типов. Существующий архив сохранён."
            view = self.backfill_view(profile)
        elif cmd == "pbf":
            profile, _, action = arg.partition(":")
            arch = self._archiver_for_profile(profile)
            if arch is None:
                toast = "Сначала запусти этот аккаунт"
            elif arch.target is None:
                toast = "Сначала настрой группу архива и проверь права записи"
            elif action == "stop":
                arch.stop_flag = True
                toast = "Останавливаю выгрузку после текущего файла"
            elif action in {"", "go"}:
                if self.backfill_running(profile):
                    toast = "Выгрузка уже идёт"
                elif self.start_backfill(profile):
                    toast = "Выгрузка старых медиа запущена"
                else:
                    toast = "Не удалось запустить выгрузку"
            view = self.backfill_view(profile)
        elif cmd == "about":
            try:
                style_supported = "style" in inspect.signature(Button.inline).parameters
            except (TypeError, ValueError):
                style_supported = False
            view = (f"✨ CIRCLES CONTROL\nВерсия: {APP_VERSION}\n\n"
                    "👤 Независимые Telegram-профили\n"
                "🎞 Архивирование кружков, фото и видео по выбору владельца\n"
                    "📢 Отдельная рассылка и пост из Избранного каждого аккаунта\n"
                    "🤖 Автоподписка по ссылкам ботов\n\n"
                    f"Цветные кнопки: {'🟢 доступны' if style_supported else '⚪ обнови Telethon до 1.45+'}\n"
                    f"Custom Emoji ID настроено: {sum(1 for v in _UI_CUSTOM_EMOJI.values() if str(v).isdigit() and int(v) > 0)}\n"
                    "Стандартные эмодзи включены. Для Premium Custom Emoji нужны реальные Telegram ID и право бота на их использование.",
                    [[_ui_button("◀️ В меню", b"main")]])
        elif cmd == "set":
            view = self.settings_view()
        elif cmd == "tg" and arg in dict(self.TOGGLES):
            self.cfg[arg] = not self.cfg.get(arg)
            if arg == "broadcast_auto_subscribe":
                self.cfg["broadcast_auto_subscribe_user_set"] = True
            save_cfg(self.cfg)
            if arg == "broadcast_auto_subscribe":
                if self.cfg[arg]:
                    self.bc._ensure_autosub_worker()
                    pause_until, pause_reason = self.bc.autosub_queue.pause_info()
                    toast = (f"Автоподписка включена; очередь остаётся на паузе: {pause_reason or 'ожидание Telegram'}"
                             if pause_until is not None and (pause_until < 0 or pause_until > time.time())
                             else "Автоподписка включена; очередь запущена")
                else:
                    toast = "Автоподписка выключена; очередь сохранена"
                self.bc._autosub_wakeup.set()
            else:
                toast = "Сохранено"
            view = self.settings_view()
        elif cmd == "num" and arg in self.NUMS:
            self.pending = arg
            return (f"✏️ {self.NUMS[arg][0]}\n\nСейчас: {self.cfg.get(arg)}\nОтправь новое значение числом.",
                    [[_ui_button("❌ Отмена", b"set")]], "")
        elif cmd == "acc" and self.account_manager:
            # Any explicit account-menu button replaces a pending text prompt;
            # login/target actions below will install the next expected input.
            self.pending_action = None
            parts = arg.split(":", 2)
            action = parts[0] if parts else "list"
            name = parts[1] if len(parts) > 1 else ""
            if action in ("list", "refresh"):
                view = self.accounts_view()
            elif action == "add":
                self.pending_action = {"type": "name"}
                return ("➕ Добавление Telegram-аккаунта\n\nОтправь короткое имя профиля латиницей, например work или second.\nПосле этого бот попросит номер, код и при необходимости пароль 2FA.",
                        [[_ui_button("❌ Отмена", b"acc:list")]], "")
            elif action == "detail":
                view = self.account_detail_view(name)
            elif action == "login":
                if name == "default":
                    toast = "Главный аккаунт уже авторизован"
                    view = self.account_detail_view(name)
                else:
                    self.pending_action = {"type": "phone", "profile": name}
                    return (f"🔐 Авторизация профиля «{name}»\n\nОтправь номер телефона с кодом страны.\nПример: +79991234567\n\nКод подтверждения и пароль 2FA будут удалены из чата после обработки.",
                            [[_ui_button("❌ Отмена", f"acc:detail:{name}".encode())]], "")
            elif action == "start":
                ok, message = await self.account_manager.start_profile(name)
                toast = message
                view = self.account_detail_view(name)
            elif action == "stop":
                ok, message = await self.account_manager.stop_profile(name)
                toast = message
                view = self.account_detail_view(name)
            elif action == "startall":
                results = await self.account_manager.start_all()
                started = sum(1 for _n, ok, _m in results if ok)
                toast = f"Запускаю аккаунты: {started}"
                view = self.accounts_view()
            elif action == "stopall":
                await self.account_manager.stop_all()
                toast = "Все дополнительные аккаунты остановлены"
                view = self.accounts_view()
            elif action == "pause":
                paused = self.account_manager.set_paused(name)
                toast = "Архивация поставлена на паузу" if paused else "Архивация продолжена"
                view = self.account_detail_view(name)
            elif action == "target":
                self.pending_action = {"type": "target", "profile": name}
                return (f"🎯 Группа архива для профиля «{name}»\n\nОтправь числовой ID супергруппы (обычно начинается с -100). Если аккаунт работает, он будет перезапущен с новым значением.",
                        [[_ui_button("❌ Отмена", f"acc:detail:{name}".encode())]], "")
            elif action == "log":
                cfg = read_profile_config(profile_config_path(name))
                p = data_dir(cfg) / "circles.log"
                if p.exists() and p.stat().st_size > 0:
                    await self.bot.send_file(self.owner_id, str(p))
                    toast = "Лог отправлен"
                else:
                    toast = "Лог пока пуст"
                view = self.account_detail_view(name)
            else:
                view = self.accounts_view()
        elif cmd == "ab":
            view = self.account_broadcast_view(arg)
        elif cmd in {"abgo", "abstop"}:
            name = arg
            ok, message = (self.account_manager.start_broadcast(name) if cmd == "abgo"
                           else self.account_manager.stop_broadcast(name))
            toast = message
            view = self.account_broadcast_view(name)
        elif cmd == "abpin":
            name = arg
            bc = self.account_manager.get_broadcaster(name)
            if not bc:
                toast = "Сначала запусти аккаунт"
            else:
                p = await bc.repin()
                toast = "Последнее сообщение из Избранного этого аккаунта выбрано" if p else "Избранное пусто"
            view = self.account_broadcast_view(name)
        elif cmd == "abposts":
            view = await self.account_saved_posts_view(arg)
        elif cmd == "abp":
            name, _, message_id = arg.partition(":")
            bc = self.account_manager.get_broadcaster(name)
            if not bc:
                toast = "Сначала запусти аккаунт"
                view = self.account_broadcast_view(name)
            else:
                try:
                    chosen = await bc.select_post(int(message_id))
                except (TypeError, ValueError):
                    chosen = None
                toast = (f"Пост ID {message_id} выбран для аккаунта {name}" if chosen
                         else "Не удалось найти это сообщение в Избранном данного аккаунта")
                view = await self.account_saved_posts_view(name)
        elif cmd == "abadm":
            name, _, page_s = arg.partition(":")
            bc = self.account_manager.get_broadcaster(name)
            if not bc:
                toast = "Сначала запусти аккаунт"
            else:
                selected, checked = await bc.select_all_groups(retry_unavailable=True)
                toast = f"Включены группы: {selected} из {checked}; прежние недоступные проверены повторно. Telegram проверит право публикации при отправке."
            view = await self.account_broadcast_chats_view(name, int(page_s or 0))
        elif cmd == "abautoset":
            view = self.autosub_settings_view(arg)
        elif cmd == "abautosubnum":
            name, _, short_key = arg.partition(":")
            key = {
                "gap": "broadcast_auto_subscribe_gap_seconds",
                "daily": "broadcast_auto_subscribe_daily_limit",
                "max": "broadcast_auto_subscribe_max_per_message",
                "batch": "broadcast_auto_subscribe_batch_size",
                "rest": "broadcast_auto_subscribe_batch_pause_minutes",
            }.get(short_key)
            if not key:
                return "Неизвестная настройка автоподписки.", [[_ui_button("◀️ Назад", f"abautoset:{name}".encode())]], ""
            self.pending_action = {"type": "autosub_num", "profile": name, "key": key}
            cfg = self.cfg if name == "default" else (self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name)))
            defaults = {"gap": 90, "daily": 15, "max": 24, "batch": 5, "rest": 30}
            current = cfg.get(key, defaults[short_key])
            labels = {
                "gap": "интервал между успешными действиями в секундах (2–3600)",
                "daily": "лимит вступлений за скользящие 24 часа (любое целое число от 1)",
                "max": "максимум ссылок из одного сообщения (1–100)",
                "batch": "число успешных действий до отдыха (1–50)",
                "rest": "длительность отдыха между пакетами в минутах (5–1440)",
            }
            label = labels[short_key]
            return (f"✏️ Отправь новое значение: {label}.\nСейчас: {current}.",
                    [[_ui_button("❌ Отмена", f"abautoset:{name}".encode())]], "")
        elif cmd == "abqmanual":
            name = arg
            bc = self.bc if name == "default" else (self.account_manager.get_broadcaster(name) if self.account_manager else None)
            if not bc:
                toast = "Сначала запусти аккаунт, чтобы управлять его очередью"
            else:
                pause_until, pause_reason = bc.autosub_queue.pause_info()
                if pause_until is not None and pause_until < 0:
                    if pause_reason == AUTOSUB_OWNER_PAUSE_REASON:
                        bc.resume_autosub_manual_pause()
                        toast = "Ручная пауза снята; ограничения Telegram не изменены"
                    else:
                        toast = "Очередь остановлена ограничением аккаунта. Для продолжения сначала проверь аккаунт и используй «Возобновить после проверки»"
                elif pause_until is not None and pause_until > time.time():
                    toast = f"Уже действует пауза Telegram/плановый отдых: {pause_reason or fmt_ts(self.cfg, pause_until)}"
                else:
                    bc.autosub_queue.set_pause(-1, AUTOSUB_OWNER_PAUSE_REASON)
                    bc._autosub_wakeup.set()
                    toast = "Очередь приостановлена; новые задания будут сохраняться"
            view = self.autosub_settings_view(name)
        elif cmd == "abqueue":
            view = self.autosub_queue_view(arg)
        elif cmd == "abqresume":
            name = arg
            bc = self.bc if name == "default" else (self.account_manager.get_broadcaster(name) if self.account_manager else None)
            if bc:
                resumed = bc.resume_autosub_after_review()
                toast = ("Очередь возобновлена после ручной проверки." if resumed else
                         "Серверная пауза Telegram ещё действует; срок ожидания не сокращён.")
            else:
                toast = "Аккаунт не запущен"
            view = self.autosub_queue_view(name)
        elif cmd == "absub":
            name = arg
            if name == "default":
                cfg = self.cfg
                account_bc = self.bc
            else:
                cfg = (self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))) if self.account_manager else read_profile_config(profile_config_path(name))
                account_bc = self.account_manager.get_broadcaster(name) if self.account_manager else None
            cfg["broadcast_auto_subscribe"] = not bool(cfg.get("broadcast_auto_subscribe", True))
            cfg["broadcast_auto_subscribe_user_set"] = True
            save_cfg(cfg)
            if name == "default":
                self.cfg["broadcast_auto_subscribe"] = cfg["broadcast_auto_subscribe"]
                self.cfg["broadcast_auto_subscribe_user_set"] = True
            elif self.account_manager and name in self.account_manager.configs:
                self.account_manager.configs[name]["broadcast_auto_subscribe"] = cfg["broadcast_auto_subscribe"]
                self.account_manager.configs[name]["broadcast_auto_subscribe_user_set"] = True
            if account_bc:
                if cfg["broadcast_auto_subscribe"]:
                    account_bc._ensure_autosub_worker()
                account_bc._autosub_wakeup.set()
            if not cfg["broadcast_auto_subscribe"]:
                toast = "Автоподписка выключена; очередь сохранена"
            else:
                pause_until, pause_reason = account_bc.autosub_queue.pause_info() if account_bc else (None, "")
                toast = (f"Автоподписка включена; очередь остаётся на паузе: {pause_reason or 'ожидание Telegram'}"
                         if pause_until is not None and (pause_until < 0 or pause_until > time.time())
                         else "Автоподписка включена; очередь запущена")
            view = self.autosub_settings_view(name)
        elif cmd == "abnotify":
            name = arg
            cfg = self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))
            cfg["broadcast_notify_waves"] = not bool(cfg.get("broadcast_notify_waves", True))
            save_cfg(cfg)
            if name in self.account_manager.configs:
                self.account_manager.configs[name]["broadcast_notify_waves"] = cfg["broadcast_notify_waves"]
            toast = "Отчёты после каждой волны включены" if cfg["broadcast_notify_waves"] else "Отчёты после каждой волны выключены"
            view = self.account_broadcast_view(name)
        elif cmd == "abch":
            name, _, tail = arg.partition(":")
            page_s, _, refresh = tail.partition(":")
            bc = self.account_manager.get_broadcaster(name)
            if bc and refresh == "refresh":
                await bc.chat_list(refresh=True)
            view = await self.account_broadcast_chats_view(name, int(page_s or 0))
        elif cmd == "abt":
            name, _, tail = arg.partition(":")
            cid_s, _, page_s = tail.partition(":")
            bc = self.account_manager.get_broadcaster(name)
            if bc:
                status = await bc.toggle_chat(int(cid_s))
                toast = {"ok": "Чат включён в рассылку", "ex": "Чат исключён", "ban": "Telegram ранее запретил отправку в этот чат; проверь права и нажми ещё раз для повтора"}.get(status, "Статус чата обновлён")
            view = await self.account_broadcast_chats_view(name, int(page_s or 0))
        elif cmd == "abnum":
            name, _, short_key = arg.partition(":")
            key = {"i": "broadcast_interval_min", "h": "broadcast_hours",
                   "dmin": "broadcast_delay_min", "dmax": "broadcast_delay_max"}.get(short_key)
            if not key:
                return "Неизвестная настройка рассылки.", [[_ui_button("◀️ Назад", f"ab:{name}".encode())]], ""
            self.pending_action = {"type": "broadcast_num", "profile": name, "key": key}
            current = (self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))).get(key)
            label = {"broadcast_interval_min": "интервал между волнами в минутах",
                     "broadcast_hours": "длительность в часах",
                     "broadcast_delay_min": "минимальная пауза между чатами в секундах",
                     "broadcast_delay_max": "максимальная пауза между чатами в секундах"}[key]
            return (f"✏️ Отправь новое значение ({label}). Сейчас: {current}.",                    [[_ui_button("❌ Отмена", f"ab:{name}".encode())]], "")
        elif cmd == "abauto":
            name = arg
            cfg = self.account_manager.configs.get(name) or read_profile_config(profile_config_path(name))
            cfg["broadcast_autostart"] = not bool(cfg.get("broadcast_autostart"))
            save_cfg(cfg)
            if name in self.account_manager.configs:
                self.account_manager.configs[name]["broadcast_autostart"] = cfg["broadcast_autostart"]
            toast = "Автозапуск включён" if cfg["broadcast_autostart"] else "Автозапуск выключен"
            view = self.account_broadcast_view(name)
        elif cmd == "abhist":
            view = self.account_broadcast_history_view(arg)
        elif cmd == "cat":
            profile, _, page_s = arg.partition(":")
            view = self.catalog_view(profile or "default", int(page_s or 0))
        elif cmd == "catf":
            profile, _, tail = arg.partition(":")
            filt, _, page_s = tail.partition(":")
            state = self._catalog_state(profile or "default")
            state["filter"] = filt if filt in {"all", "small", "stale", "ignored", "pending"} else "all"
            state["page"] = 0
            view = self.catalog_view(profile or "default", int(page_s or 0))
        elif cmd == "cats":
            profile, _, tail = arg.partition(":")
            sort_mode, _, page_s = tail.partition(":")
            state = self._catalog_state(profile or "default")
            if sort_mode == "next":
                order = ["recent", "oldest", "few", "name"]
                current = state.get("sort", "recent")
                sort_mode = order[(order.index(current) + 1) % len(order)]
            state["sort"] = sort_mode if sort_mode in {"recent", "oldest", "few", "name"} else "recent"
            view = self.catalog_view(profile or "default", int(page_s or 0))
        elif cmd == "catq":
            profile = arg or "default"
            self.pending_action = {"type": "catalog_search", "profile": profile}
            return ("🔎 Поиск по каталогу\n\nОтправь имя собеседника или числовой ID. Можно использовать часть имени.",
                    [[_ui_button("❌ Отмена", f"cat:{profile}:0".encode())]], "")
        elif cmd == "catclear":
            profile = arg or "default"
            self._catalog_state(profile)["query"] = ""
            self._catalog_state(profile)["page"] = 0
            view = self.catalog_view(profile, 0)
        elif cmd == "catitem":
            profile, _, tail = arg.partition(":")
            uid_s, _, page_s = tail.partition(":")
            view = self.catalog_detail_view(profile, int(uid_s), int(page_s or 0))
        elif cmd == "catvid":
            profile, _, tail = arg.partition(":")
            uid_s, _, page_s = tail.partition(":")
            view = self.catalog_circles_view(profile, int(uid_s), int(page_s or 0))
        elif cmd == "catig":
            profile, _, tail = arg.partition(":")
            uid_s, _, page_s = tail.partition(":")
            profile_cfg = self._catalog_profile(profile)
            uid = int(uid_s)
            ignore = profile_cfg.setdefault("ignore_users", [])
            if uid in ignore:
                ignore.remove(uid)
                toast = "Сохранение медиа от этого пользователя возобновлено"
            else:
                ignore.append(uid)
                toast = "Пользователь добавлен в игнор-лист"
            save_cfg(profile_cfg)
            if profile == "default":
                self.cfg["ignore_users"] = ignore
            elif self.account_manager and profile in self.account_manager.configs:
                self.account_manager.configs[profile]["ignore_users"] = ignore
            view = self.catalog_detail_view(profile, uid, int(page_s or 0))
        elif cmd == "catcleanup":
            self.catalog_cleanup_plans.pop(arg or "default", None)
            view = self.catalog_cleanup_view(arg or "default")
        elif cmd == "catlog":
            view = self.catalog_cleanup_history_view(arg or "default")
        elif cmd == "catthr":
            profile, _, key = arg.partition(":")
            profile_cfg = self._catalog_profile(profile)
            setting = "circles_catalog_min_count" if key == "min" else "circles_catalog_stale_days"
            current = profile_cfg.get(setting, 5 if key == "min" else 30)
            self.pending_action = {"type": "catalog_threshold", "profile": profile, "key": key}
            label = "минимальное количество медиафайлов" if key == "min" else "число дней без обновлений"
            return (f"⚙️ Настройка очистки · {label}\n\nСейчас: {current}. Отправь новое целое число.",
                    [[_ui_button("❌ Отмена", f"catcleanup:{profile}".encode())]], "")
        elif cmd == "catplan":
            profile, _, mode = arg.partition(":")
            rows, cfg = self._catalog_rows(profile)
            all_candidates = [r for r in rows if self._catalog_candidate(r, mode, cfg)]
            min_count = max(0, int(cfg.get("circles_catalog_min_count", 5)))
            stale_days = max(1, int(cfg.get("circles_catalog_stale_days", 30)))
            def matches_cleanup_rule(r):
                small = int(r[3] or 0) < min_count
                age = self._catalog_age_days(r)
                stale = age is not None and age >= stale_days
                return small if mode == "small" else stale if mode == "stale" else (small or stale)
            protected = sum(1 for r in rows if matches_cleanup_rule(r) and
                            (int(r[5] or 0) > 0 or int(r[2] or 0) <= 1))
            self.catalog_cleanup_plans[profile] = {
                "mode": mode, "protected": protected,
                "items": [{"user_id": int(r[0]), "title": str(r[1] or r[0]), "topic_id": int(r[2]),
                           "count": int(r[3]), "last": r[6]} for r in all_candidates[:100]]
            }
            view = self.catalog_plan_view(profile)
            if len(all_candidates) > 100:
                view = (view[0] + "\n\nПоказаны первые 100 кандидатов; очистку можно повторить для следующей партии.", view[1])
        elif cmd == "catone":
            profile, _, tail = arg.partition(":")
            uid_s, _, page_s = tail.partition(":")
            rows, cfg = self._catalog_rows(profile)
            row = next((r for r in rows if int(r[0]) == int(uid_s)), None)
            if not row:
                toast = "Тема больше не найдена"
                view = self.catalog_view(profile, int(page_s or 0))
            elif int(row[5] or 0) > 0:
                toast = "Нельзя удалить тему: есть недоставленные медиа. Сначала разберись с повторами."
                view = self.catalog_detail_view(profile, int(uid_s), int(page_s or 0))
            elif int(row[2] or 0) <= 1:
                toast = "Системная тема General защищена от удаления."
                view = self.catalog_detail_view(profile, int(uid_s), int(page_s or 0))
            else:
                self.catalog_cleanup_plans[profile] = {"mode": "one", "protected": 0,
                    "items": [{"user_id": int(row[0]), "title": str(row[1] or row[0]),
                               "topic_id": int(row[2]), "count": int(row[3]), "last": row[6]}]}
                view = self.catalog_plan_view(profile)
        elif cmd == "catdel":
            profile, _, action = arg.partition(":")
            if action == "go":
                toast = await self.catalog_delete_plan(profile)
                if profile in self.catalog_cleanup_plans and self.catalog_cleanup_plans[profile].get("items"):
                    view = self.catalog_plan_view(profile)
                else:
                    view = self.catalog_view(profile, 0)
            else:
                view = self.catalog_plan_view(profile)
        elif cmd == "ppl":
            view = self.people_view(int(arg or 0), await asyncio.to_thread(self.db.people))
        elif cmd == "ig":
            uid_s, _, page = arg.partition(":")
            uid = int(uid_s)
            if uid in self.cfg["ignore_users"]:
                self.cfg["ignore_users"].remove(uid)
                toast = "Медиа снова сохраняются"
            else:
                self.cfg["ignore_users"].append(uid)
                toast = "Добавлен в игнор"
            save_cfg(self.cfg)
            view = self.people_view(int(page or 0), await asyncio.to_thread(self.db.people))
        elif cmd == "bf":
            if arg == "go" and not self.backfill_running("default"):
                if self.arch.target is None:
                    toast = "Группа архива не настроена - см. Диагностику"
                else:
                    toast = "Выгрузка запущена" if self.start_backfill("default") else "Не удалось запустить выгрузку"
            elif arg == "stop":
                self.arch.stop_flag = True
                toast = "Останавливаю..."
            view = self.backfill_view("default")
        elif cmd == "bc":
            if arg == "go":
                if self.bc.running:
                    toast = "Уже идёт"
                else:
                    self.bc.start(self.cfg["broadcast_interval_min"], self.cfg["broadcast_hours"])
                    toast = "Рассылка запускается по всем включённым группам..."
            elif arg == "stop":
                if self.bc.running:
                    self.bc.stop()
                    toast = "Останавливаю..."
                else:
                    toast = "Рассылка не запущена"
            view = self.bc_view()
        elif cmd == "post":
            if arg == "pin":
                p = await self.bc.repin()
                toast = "Последнее сообщение из Избранного основного аккаунта выбрано" if p else "Избранное пусто"
            view = await self.post_view()
        elif cmd == "posts":
            view = await self.saved_posts_view()
        elif cmd == "pick":
            try:
                chosen = await self.bc.select_post(int(arg))
            except (TypeError, ValueError):
                chosen = None
            toast = (f"Выбран пост ID {arg} из Избранного основного аккаунта" if chosen
                     else "Не удалось найти это сообщение в Избранном основного аккаунта")
            view = await self.saved_posts_view()
        elif cmd == "ch":
            a, _, page = arg.partition(":")
            if a == "r":
                await self.bc.chat_list(refresh=True)
                toast = "Список обновлён"
            elif a == "all":
                selected, checked = await self.bc.select_all_groups(retry_unavailable=True)
                toast = f"Включены группы: {selected} из {checked}; прежние недоступные проверены повторно. Telegram проверит право публикации при отправке."
            elif a not in ("v", ""):
                st = await self.bc.toggle_chat(int(a))
                toast = {"ok": "Чат включён в рассылку", "ex": "Чат исключён", "ban": "Нет прав"}[st]
            view = await self.chats_view(int(page or 0))
        elif cmd == "hist":
            view = self.hist_view()
        elif cmd == "log":
            view = self.log_view(arg or "b")
        elif cmd == "logf":
            p = self.ddir / ("broadcast.log" if arg == "b" else "circles.log")
            if p.exists() and p.stat().st_size > 0:      # Telegram не принимает пустые файлы
                await self.bot.send_file(self.owner_id, str(p))
                toast = "Отправил файлом"
            else:
                toast = "Лог пока пуст"
            view = self.log_view(arg or "b")
        elif cmd == "diag":
            view = await self.diag_view()
        elif cmd == "clean":
            if arg == "go":
                n, freed = await self.arch.cleanup_sent()
                toast = f"Удалено файлов: {n}, освобождено {human(freed)}"
                view = await self.status_view()
            else:
                view = ("🧹 Удалить копии?\n\nУдалю с диска сервера медиафайлы, которые УЖЕ отправлены в архивную группу. "
                        "В Telegram они останутся. Неотправленные не трогаю.",
                        [[_ui_button("✅ Удалить", b"clean:go"), _ui_button("◀️ Отмена", b"status")]])
        elif cmd == "restart":
            if arg == "go":
                asyncio.create_task(self._do_restart())
                return "🔄 Перезапускаюсь... через несколько секунд напиши мне любое сообщение.", None, ""
            view = ("Перезапустить сервис?\nВыгрузка старых медиа прервётся и продолжится с checkpoint после запуска.",
                    [[_ui_button("✅ Да", b"restart:go"), _ui_button("◀️ Отмена", b"main")]])
        else:
            view = self.main_view()
        return view[0], view[1], toast

    async def _do_restart(self):
        await asyncio.sleep(1.5)  # даём ответу уйти
        await self.restart_cb()

    async def on_callback(self, event):
        if event.sender_id != self.owner_id:
            await event.answer("Нет доступа", alert=True)
            return
        answered = False

        async def ack(text=None):
            nonlocal answered
            if answered:
                return
            answered = True
            try:
                await event.answer((text or "")[:190])
            except Exception:  # noqa: BLE001
                pass

        task = asyncio.ensure_future(self.dispatch(event.data.decode("utf-8")))
        try:
            # быстрые кнопки отвечают тостом вместе с правкой; долгие - сразу снимаем «часики»
            done, _ = await asyncio.wait({task}, timeout=0.8)
            if not done:
                await ack("⏳ Секунду...")
            res = await task
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в боте-панели")
            if answered:
                await self.bot.send_message(self.owner_id, "Ошибка, подробности в логе.")
            else:
                await event.answer("Ошибка, подробности в логе", alert=True)
            return
        if res is None:
            await ack()
            return
        text, buttons, toast = res

        async def edit():
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:  # noqa: BLE001
                if "not modified" not in str(e).lower():
                    log.warning("Не удалось обновить сообщение: %s", e)

        await asyncio.gather(edit(), ack(toast))

    async def on_message(self, event):
        raw_text = event.raw_text or ""
        # Preserve exact whitespace in 2FA passwords; normalize ordinary inputs.
        text = raw_text if self.pending_action and self.pending_action.get("type") == "password" else raw_text.strip()
        secret_input = bool(self.pending_action and self.pending_action.get("type") in {"phone", "code", "password"})
        try:
            if self.pending_action and not text.startswith("/"):
                try:
                    reply, buttons = await self.handle_account_text(text)
                finally:
                    if secret_input:
                        try:
                            await event.delete()  # не оставляем номер, код или пароль 2FA в переписке с ботом
                        except Exception:
                            pass
            elif self.pending and not text.startswith("/"):
                reply, buttons = await self.handle_text(text)
            else:
                self.pending = None
                self.pending_action = None
                data = self.COMMANDS.get(text.split()[0].lower().split("@")[0], "main") if text else "main"
                reply, buttons, _ = await self.dispatch(data)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в боте-панели")
            reply, buttons = "Ошибка, подробности в логе.", None
        await event.respond(reply, buttons=buttons)

    def register(self):
        owner = self.owner_id
        self.bot.add_event_handler(self.on_message,
                                   events.NewMessage(func=lambda e: e.is_private and e.sender_id == owner))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery())

    async def notify_started(self):
        try:
            text, buttons = self.main_view()
            await self.bot.send_message(self.owner_id, "✅ Сервис запущен\n\n" + text, buttons=buttons)
        except Exception as e:  # noqa: BLE001
            log.info("Не смог написать владельцу (открой бота и нажми /start): %s", e)


AUTH_ERRORS = tuple(getattr(errors, n) for n in (
    "AuthKeyError", "AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
    "UserDeactivatedError", "UserDeactivatedBanError") if hasattr(errors, n))


async def _dead_session_notice(cfg, ddir, text):
    """Сессия мертва, а владельца надо предупредить: пишем через бота (id владельца запомнен с прошлых запусков)."""
    if not (cfg.get("bot_token") and cfg.get("owner_id")):
        return
    try:
        b = TelegramClient(str(ddir / "control_bot"), int(cfg["api_id"]), cfg["api_hash"])
        await b.start(bot_token=cfg["bot_token"])
        await b.send_message(int(cfg["owner_id"]), text)
        await b.disconnect()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить уведомление о мёртвой сессии")


async def run_service(cfg, ddir, broadcast=None, *, allow_broadcast=True, handle_signals=True,
                      restart_args=None, enable_account_manager=True, allow_broadcast_autostart=True,
                      on_broadcaster_ready=None,
                      on_archiver_ready=None) -> int:
    """Run one Telegram account with its own archiver and optional broadcaster."""
    # Startup must not silently rewrite recipient restrictions or auto-subscribe policy.
    # Legacy configuration is interpreted as stored; only explicit owner actions change it.
    notifier = Notifier()
    db = DB(ddir / "circles.db")
    client = make_client(cfg, ddir)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Сессия не авторизована. Пройди авторизацию через Telegram-панель → Аккаунты.")
        await client.disconnect()
        await _dead_session_notice(cfg, ddir, "❌ Сессия аккаунта не авторизована. Открой раздел «Аккаунты» в панели управления и нажми «Авторизовать».")
        return 2
    me = await client.get_me()
    if cfg.get("owner_id") != me.id:
        cfg["owner_id"] = me.id
        save_cfg(cfg)

    manager_profile_name = ACTIVE_PROFILE.get() or "default"
    if WEB_MANAGER_RUNTIME is not None:
        try:
            WEB_MANAGER_RUNTIME.register_profile(manager_profile_name, client, cfg, ddir)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось зарегистрировать профиль %s в Smart Cleaner", manager_profile_name)

    arch = Archiver(cfg, ddir, db, client)
    arch.notifier = notifier
    target_err = await arch.setup_target()
    if target_err:
        log.error(target_err)   # не выходим: рассылка и бот продолжают работать
    if on_archiver_ready:
        on_archiver_ready(arch)

    @client.on(events.NewMessage(func=lambda e: e.is_private and bool(getattr(e.message, "media", None))))
    async def handler(event):
        try:
            user = await event.get_chat()
            if isinstance(user, types.User):
                if arch.media_type(event.message):
                    await arch.process(event.message, user)
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения")
            await notifier.alert("handler", f"⚠️ Ошибка обработки медиа: {type(e).__name__}: {e}", 1800)

    # The listener only records supported links to the durable queue. The worker
    # that executes queued actions is started/stopped with the broadcast campaign.
    bc = Broadcaster(cfg, client, ddir, notifier, enabled=True)
    bc.load_files()
    if WEB_MANAGER_RUNTIME is not None:
        WEB_MANAGER_RUNTIME.register_broadcaster(manager_profile_name, bc) 
    if allow_broadcast and on_broadcaster_ready:
        on_broadcaster_ready(bc)

    # Web panel is the only operator control plane. Do not start a Telegram bot
    # process even when legacy bot_token remains in config.json.
    bot = None
    account_manager = AccountManager(me.id) if enable_account_manager and manager_profile_name == "default" else None
    if account_manager is not None and WEB_MANAGER_RUNTIME is not None:
        WEB_MANAGER_RUNTIME.set_account_manager(account_manager)

    if target_err:
        await notifier.alert("target", f"⚠️ Архиватор медиа не работает: {target_err}\n"
                             "Исправь ID группы в ⚙️ Настройки и проверь через 🩺 Диагностика. "
                             "Рассылка при этом работает.", 0)

    web_manager_started = False
    if (WEB_MANAGER_RUNTIME is not None and manager_profile_name == "default"
            and enable_account_manager and cfg.get("web_manager_enabled", True)):
        try:
            await WEB_MANAGER_RUNTIME.start()
            web_manager_started = True
            log.info("Smart Cleaner доступен локально: http://127.0.0.1:%s/", WEB_MANAGER_RUNTIME.port)
        except Exception:  # noqa: BLE001
            log.exception("Web Manager не запущен; Telegram-панель продолжит работу")

    if allow_broadcast:
        # A persisted active campaign is a recoverable checkpoint, not permission
        # to send automatically. Resume only when the explicit autostart flag is on.
        # An explicit --broadcast request still starts a campaign.
        resumed = False
        autostart_enabled = bool(allow_broadcast_autostart and cfg.get("broadcast_autostart", False))
        if autostart_enabled:
            resumed = bc.resume()
        elif bc.state.get("active") and float(bc.state.get("end") or 0) > time.time():
            log.info("Сохранённая рассылка оставлена на паузе: для веб-управляемого аккаунта "
                     "требуется явный запуск кампании из панели")
        if not resumed:
            if broadcast is not None:
                bc.start(*broadcast)
            elif autostart_enabled:
                bc.start(cfg["broadcast_interval_min"], cfg["broadcast_hours"])

    async def _health_loop():
        while True:
            await asyncio.sleep(600)
            await check_health(client, ddir, cfg, notifier)

    async def _warm_cache():
        if not allow_broadcast:
            return
        try:
            await bc.chat_list()      # список чатов готов к первому нажатию «💬 Чаты»
        except Exception:  # noqa: BLE001
            pass

    tasks = [asyncio.create_task(arch.periodic_retry()), asyncio.create_task(_health_loop())]
    if allow_broadcast:
        tasks.append(asyncio.create_task(_warm_cache()))

    loop = asyncio.get_running_loop()

    def _loop_exc(_loop, ctx):
        exc = ctx.get("exception")
        log.error("Необработанная ошибка в задаче: %s", ctx.get("message"), exc_info=exc)
        asyncio.ensure_future(notifier.alert(
            "loop-exc", f"⚠️ Необработанная ошибка: {type(exc).__name__ if exc else ctx.get('message')}", 900))

    if handle_signals:
        loop.set_exception_handler(_loop_exc)

    async def shutdown():
        bc.suspend()
        await bc.close()
        if bot:
            await bot.disconnect()
        await client.disconnect()

    if handle_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(shutdown()))
            except (NotImplementedError, RuntimeError):
                pass
    log.info("Работаю как %s. Жду выбранные типы медиа...", me.first_name)
    code = 0
    try:
        await client.run_until_disconnected()
    except AUTH_ERRORS as e:
        log.error("Сессия аккаунта недействительна: %s", e)
        await notifier.alert("auth", "❌ Сессия аккаунта слетела. Открой раздел «Аккаунты» в панели управления и пройди авторизацию заново.", 0)
        code = 2
    finally:
        bc.suspend()
        await bc.close()
        if account_manager:
            await account_manager.stop_all()
        if account_manager is not None and WEB_MANAGER_RUNTIME is not None:
            WEB_MANAGER_RUNTIME.set_account_manager(None)
        if web_manager_started and WEB_MANAGER_RUNTIME is not None:
            await WEB_MANAGER_RUNTIME.stop()
        for t in tasks + ([bc.task] if bc.task else []):
            t.cancel()
        await asyncio.gather(*tasks, *([bc.task] if bc.task else []), return_exceptions=True)
        if bot:
            await bot.disconnect()
        if client.is_connected():
            await client.disconnect()
        if WEB_MANAGER_RUNTIME is not None and not web_manager_started:
            WEB_MANAGER_RUNTIME.unregister_profile(manager_profile_name)
    log.info("Остановлено")
    return code


async def run_web_manager_host(cfg: dict) -> int:
    """Run the web control plane without holding any Telegram account session."""
    if WEB_MANAGER_RUNTIME is None:
        raise RuntimeError("Web Manager недоступен: проверь зависимости веб-панели")
    manager = AccountManager(int(cfg.get("owner_id") or 0))
    WEB_MANAGER_RUNTIME.set_account_manager(manager)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await WEB_MANAGER_RUNTIME.start()
        log.info("Веб-панель запущена. Telegram-аккаунты ожидают команду из панели.")
        await stop_event.wait()
        return 0
    finally:
        await manager.stop_all()
        await WEB_MANAGER_RUNTIME.stop()
        WEB_MANAGER_RUNTIME.set_account_manager(None)
        for sig in installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass


async def run_all_accounts(items: list[tuple[str, dict, Path]]) -> int:
    """Legacy direct multi-run mode; managed profiles use AccountManager instead."""
    async def one(name: str, cfg: dict, ddir: Path):
        profile_token = ACTIVE_PROFILE.set(name)
        log.info("Запускаю профиль '%s' (user session отдельная)", name)
        try:
            return await run_service(cfg, ddir, allow_broadcast=False, handle_signals=False,
                                     restart_args=["run", "--all-accounts"])
        except Exception:
            log.exception("Профиль '%s' завершился с ошибкой", name)
            return 1
        finally:
            ACTIVE_PROFILE.reset(profile_token)

    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    installed_signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, current.cancel)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        results = await asyncio.gather(*(one(name, cfg, ddir) for name, cfg, ddir in items),
                                       return_exceptions=True)
    except asyncio.CancelledError:
        log.info("Остановка всех аккаунтов по сигналу")
        return 130
    finally:
        for sig in installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass
    codes = []
    for (name, _cfg, _ddir), result in zip(items, results):
        if isinstance(result, BaseException):
            log.error("Профиль '%s': %s", name, result)
            codes.append(1)
        else:
            codes.append(int(result))
    return max(codes, default=0)


async def run_check(cfg, ddir) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    client = make_client(cfg, ddir)
    try:
        await client.connect()
        out.append((True, "Соединение с Telegram есть"))
        if not await client.is_user_authorized():
            out.append((False, "Аккаунт не авторизован - выберите «Войти в аккаунт»"))
            return out
        me = await client.get_me()
        out.append((True, f"Аккаунт: {me.first_name} (id {me.id})"))
        arch = Archiver(cfg, ddir, DB(ddir / "circles.db"), client)
        err = await arch.setup_target()
        if err:
            out.append((False, err))
            return out
        out.append((True, f"Группа найдена: {getattr(arch.target, 'title', '?')}, темы включены"))
        out.append(await check_rights(client, arch.target))
    except Exception as e:  # noqa: BLE001
        out.append((False, f"Ошибка: {e}"))
    finally:
        await client.disconnect()
    return out


async def run_login(cfg, ddir):
    client = make_client(cfg, ddir)
    await client.start(phone=lambda: input("Телефон (в формате +79991234567): ").strip(),
                       code_callback=lambda: input("Код из Telegram: ").strip(),
                       password=lambda: getpass.getpass("Пароль двухэтапной защиты (2FA): "))
    me = await client.get_me()
    print(f"Успешно! Аккаунт: {me.first_name} (id {me.id})")
    await client.disconnect()


async def run_backfill(cfg, ddir, limit):
    client = make_client(cfg, ddir)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("Аккаунт не авторизован - сначала войдите.")
            return
        arch = Archiver(cfg, ddir, DB(ddir / "circles.db"), client)
        err = await arch.setup_target()
        if err:
            print(err)
            return
        print("Выгружаю старые выбранные медиа (может занять время)...")
        n = await arch.backfill(limit)
        print(f"Готово. Новых медиафайлов отправлено: {n}")
    finally:
        await client.disconnect()


# =============================================================================
#  systemd
# =============================================================================
def has_systemd() -> bool:
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").exists()


def sc(*args) -> tuple[int, str]:
    r = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def unit_path() -> Path:
    return Path(f"/etc/systemd/system/{SERVICE}.service")


def service_state() -> str:
    if not has_systemd():
        return "нет systemd"
    if not unit_path().exists():
        return "не установлен"
    return (sc("is-active", SERVICE)[1].splitlines() or ["?"])[0]


def unit_text(all_accounts: bool = False) -> str:
    # A single supervisor process owns the bot panel and starts additional profiles.
    # Keeping this command as `run` makes account management available in Telegram.
    run_args = "run"
    description = "Telegram circles manager (multi-account bot control)" if all_accounts else "Telegram circles archiver + broadcast + account manager"
    return f"""[Unit]
Description={description}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={BASE}
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
Environment=LANG=C.UTF-8
Environment=LC_ALL=C.UTF-8
ExecStart={sys.executable} {Path(__file__).resolve()} {run_args}
Restart=always
RestartSec=10
RestartPreventExitStatus=2 3 4
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def install_service(all_accounts: bool = False) -> str:
    if not has_systemd():
        return "На этой системе нет systemd."
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return "Нужны права root: запустите меню через sudo."
    unit_path().write_text(unit_text(all_accounts=all_accounts), encoding="utf-8")
    sc("daemon-reload")
    sc("enable", SERVICE)
    action = "restart" if service_state() == "active" else "start"
    rc, out = sc(action, SERVICE)
    if rc == 0:
        mode = "с управлением аккаунтами из Telegram-бота" if all_accounts else "для основного аккаунта и дополнительных профилей из Telegram-бота"
        return f"Сервис {mode} установлен, включён в автозапуск и запущен."
    return f"Ошибка: {out}"


# =============================================================================
#  Меню
# =============================================================================
def ask(prompt, default=None):
    d = f" [{default}]" if default not in (None, "") else ""
    s = input(f"{prompt}{d}: ").strip()
    return s if s else default


def yesno(prompt, default=False) -> bool:
    s = input(f"{prompt} ({'Д/н' if default else 'д/Н'}): ").strip().lower()
    return default if not s else s in ("д", "да", "y", "yes", "1")


def pause():
    input("\nEnter - продолжить...")


def human(n: float) -> str:
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ПБ"


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _sqlite_online_backup(source: Path, destination: Path) -> bool:
    """Create a consistent SQLite snapshot, including WAL changes."""
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source), timeout=20)
    dst = sqlite3.connect(str(destination), timeout=20)
    try:
        src.backup(dst, pages=256, sleep=0.05)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return destination.is_file() and destination.stat().st_size > 0


def _redacted_config(cfg: dict) -> dict:
    """Return operational settings while excluding credentials and runtime paths."""
    sensitive_fragments = ("token", "api_hash", "password", "secret", "session_string", "auth_key")
    out = {}
    for key, value in cfg.items():
        key_l = str(key).casefold()
        if key.startswith("_") or key_l == "owner_id":
            continue
        if any(part in key_l for part in sensitive_fragments):
            out[key] = "[REDACTED]"
        else:
            out[key] = value
    return out


def create_safe_backup(keep: int = 5) -> tuple[Path, list[str], int]:
    """Backup settings, archive DBs and broadcast state; never exports auth sessions."""
    backup_dir = BASE / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")
    output = backup_dir / f"circles_backup_{stamp}.zip"
    warnings: list[str] = []
    profiles_done = 0

    with tempfile.TemporaryDirectory(prefix="circles-backup-") as temp_name:
        temp_root = Path(temp_name)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            manifest = [
                f"CIRCLES CONTROL BACKUP {APP_VERSION}",
                f"Created: {stamp}",
                "Included: redacted profile settings, SQLite snapshots, broadcast checkpoints and per-chat cooldowns.",
                "Excluded intentionally: Telegram .session files, bot tokens, API hashes, .env, logs, video files, and autosub_queue.db.",
                "autosub_queue.db contains private invite hashes / bot start payloads; it stays owner-only on the VPS and is not exported.",
                "This is a data/settings backup, not a full login/session restore. Re-authorize accounts after disaster recovery.",
                "",
            ]
            for profile_name, config_path in account_profiles():
                safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", profile_name)[:48] or "profile"
                prefix = f"profiles/{safe_name}"
                try:
                    cfg = read_profile_config(config_path)
                    ddir = data_dir(cfg)
                    zf.writestr(f"{prefix}/config.redacted.json",
                                json.dumps(_redacted_config(cfg), ensure_ascii=False, indent=2))
                    profiles_done += 1
                    manifest.append(f"Profile: {profile_name}")

                    for db_name in ("circles.db", "chat_skip.db"):
                        source = ddir / db_name
                        if source.is_file():
                            snapshot = temp_root / safe_name / db_name
                            try:
                                if _sqlite_online_backup(source, snapshot):
                                    zf.write(snapshot, f"{prefix}/{db_name}")
                                else:
                                    warnings.append(f"{profile_name}: {db_name} snapshot empty")
                            except Exception as exc:  # noqa: BLE001
                                warnings.append(f"{profile_name}: {db_name}: {type(exc).__name__}")

                    for state_name in ("broadcast_state.json", "slow_mode_until.json", "subscriptions.json",
                                       "banned_chats.json", "started_bots.json", "pending_invites.json"):
                        source = ddir / state_name
                        if source.is_file():
                            try:
                                # Validate JSON before placing it in the backup.
                                parsed = json.loads(source.read_text(encoding="utf-8"))
                                zf.writestr(f"{prefix}/{state_name}",
                                            json.dumps(parsed, ensure_ascii=False, indent=2))
                            except Exception as exc:  # noqa: BLE001
                                warnings.append(f"{profile_name}: {state_name}: {type(exc).__name__}")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{profile_name}: profile skipped ({type(exc).__name__})")

            manifest.extend(["", f"Profiles exported: {profiles_done}",
                             f"Warnings: {len(warnings)}"])
            if warnings:
                manifest.extend(warnings)
            zf.writestr("MANIFEST.txt", "\n".join(manifest))

    try:
        os.chmod(output, 0o600)
    except OSError:
        pass
    # Retain only the newest few snapshots to avoid silently filling the VPS disk.
    existing = sorted(backup_dir.glob("circles_backup_*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in existing[max(1, int(keep)):]:
        try:
            old.unlink()
        except OSError:
            pass
    return output, warnings, profiles_done


def _process_rss_mb() -> float | None:
    """Best-effort Linux process RSS; portable fallback is intentionally None."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _sqlite_file_check(path: Path) -> str:
    if not path.is_file():
        return "нет файла"
    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=8)
        row = conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "нет результата"
    except Exception as exc:  # noqa: BLE001
        return f"ошибка: {type(exc).__name__}"
    finally:
        if conn is not None:
            conn.close()


def telegram_action(cfg, make_coro):
    """Выполняет операцию с Telegram, на время останавливая сервис (одна сессия - один процесс)."""
    ddir = data_dir(cfg)
    was_active = service_state() == "active"
    if was_active:
        if not yesno("Сервис запущен и держит сессию. Остановить на время операции?", True):
            return
        sc("stop", SERVICE)
    lk = acquire_lock(ddir)
    try:
        if lk is None:
            print("Другая копия скрипта уже работает с этой сессией. Остановите её и повторите.")
            return
        asyncio.run(make_coro())
    except KeyboardInterrupt:
        print("\nПрервано.")
    except Exception as e:  # noqa: BLE001
        print(f"Ошибка: {e}")
    finally:
        if lk not in (None, True):
            lk.close()
        if was_active:
            sc("start", SERVICE)
            print("Сервис снова запущен.")


def offer_restart():
    if service_state() == "active" and yesno("Перезапустить сервис, чтобы применить изменения?", True):
        print(sc("restart", SERVICE)[1] or "Перезапущено.")


def show_status(cfg):
    ddir = data_dir(cfg)
    db = DB(ddir / "circles.db")
    s = db.stats()
    sess = ddir / (cfg["session_name"] + ".session")
    print("\n--- Статус ---")
    print(f"Сервис:            {service_state()}")
    print(f"Файл сессии:       {'есть' if sess.exists() else 'нет (нужен вход)'}")
    problems = cfg_problems(cfg)
    print(f"Настройки:         {'OK' if not problems else '; '.join(problems)}")
    print(f"Бот-панель:        {'токен задан' if cfg.get('bot_token') else 'не настроена (токен от @BotFather)'}")
    print(f"Тем (людей):       {s['topics']}")
    print(f"Медиа всего:       {s['total']}  (отправлено {s['sent']}, ждут повтора {s['unsent']})")
    print(f"Последнее медиа:  {s['last'] or '-'}")
    vids = ddir / "videos"
    print(f"Медиа на диске:    {human(dir_size(vids)) if vids.exists() else '0 Б'}")
    du = shutil.disk_usage(ddir)
    print(f"Свободно на диске: {human(du.free)} из {human(du.total)}")
    pause()


SETTINGS = [
    ("api_id", "API ID", "int"),
    ("api_hash", "API HASH", "secret"),
    ("target_chat_id", "ID группы-форума (-100...)", "int"),
    ("save_outgoing", "Сохранять и МОИ медиа", "bool"),
    ("info_message", "Подпись (дата/длительность) под медиа", "bool"),
    ("keep_local", "Хранить копии медиа на диске VPS", "bool"),
    ("data_dir", "Папка данных", "str"),
    ("session_name", "Имя файла сессии", "str"),
    ("bot_token", "Токен бота-панели (@BotFather)", "secret"),
    ("broadcast_interval_min", "Рассылка: интервал между кругами (мин)", "int"),
    ("broadcast_hours", "Рассылка: длительность (часов)", "int"),
    ("broadcast_autostart", "Рассылка стартует вместе с сервисом", "bool"),
    ("broadcast_auto_subscribe", "Фоновая автоподписка по ссылкам ботов", "bool"),
    ("broadcast_spambot", "Рассылка: писать @SpamBot при спам-блоке", "bool"),
    ("broadcast_notify_waves", "Рассылка: отчёт после каждой волны", "bool"),
    ("tz_offset", "Часовой пояс для времени в боте (UTC+N)", "int"),
    ("low_disk_percent", "Предупреждать, если свободно меньше N% диска", "int"),
]


def fmt_val(cfg, key, kind):
    v = cfg[key]
    if kind == "bool":
        return "ВКЛ" if v else "выкл"
    if kind == "secret":
        return (v[:3] + "…" + v[-3:]) if v and len(v) > 8 else ("задан" if v else "не задан")
    return str(v) if v not in ("", 0) else "не задано"


def settings_menu(cfg):
    changed = False
    while True:
        print("\n--- Настройки ---")
        for i, (k, title, kind) in enumerate(SETTINGS, 1):
            print(f" {i}. {title}: {fmt_val(cfg, k, kind)}")
        print(" 0. Назад")
        c = ask("Что изменить")
        if c in (None, "0"):
            break
        if not (c.isdigit() and 1 <= int(c) <= len(SETTINGS)):
            continue
        key, title, kind = SETTINGS[int(c) - 1]
        if kind == "bool":
            cfg[key] = not cfg[key]
            if key == "broadcast_auto_subscribe":
                cfg["broadcast_auto_subscribe_user_set"] = True
        elif kind == "int":
            v = ask(f"{title}")
            try:
                cfg[key] = int(v)
            except (TypeError, ValueError):
                print("Нужно число.")
                continue
        else:
            v = ask(f"{title}")
            if not v:
                continue
            if key == "session_name" and not re.fullmatch(r"[\w.-]+", v):
                print("Допустимы буквы, цифры, _ . -")
                continue
            if key == "bot_token" and not re.fullmatch(r"\d+:[\w-]{20,}", v):
                print("Это не похоже на токен. Формат: 123456789:AAH... (от @BotFather)")
                continue
            if key == "data_dir":
                print("Внимание: уже накопленные данные автоматически не переносятся.")
            cfg[key] = v.strip()
        save_cfg(cfg)
        changed = True
        print("Сохранено.")
    if changed:
        offer_restart()


def people_menu(cfg):
    ddir = data_dir(cfg)
    db = DB(ddir / "circles.db")
    while True:
        rows = db.people()
        print("\n--- Люди и игнор-лист ---")
        if not rows:
            print("Пока нет ни одной темы.")
        for i, (uid, title, tid, cnt, last) in enumerate(rows, 1):
            flag = "  [ИГНОР]" if uid in cfg["ignore_users"] else ""
            print(f" {i:>2}. {title}  - медиафайлов: {cnt}, последнее: {(last or '-')[:10]}{flag}")
        print(" Номер - включить/выключить игнор | id - добавить по id | 0 - назад")
        c = ask("Выбор")
        if c in (None, "0"):
            break
        uid = None
        if c.isdigit() and 1 <= int(c) <= len(rows):
            uid = rows[int(c) - 1][0]
        elif c.lstrip("-").isdigit() and len(c) > 4:
            uid = int(c)
        if uid is None:
            continue
        if uid in cfg["ignore_users"]:
            cfg["ignore_users"].remove(uid)
            print("Убран из игнор-листа.")
        else:
            cfg["ignore_users"].append(uid)
            print("Добавлен в игнор-лист.")
        save_cfg(cfg)
        offer_restart()


def service_menu(cfg):
    while True:
        print(f"\n--- Сервис (состояние: {service_state()}) ---")
        print(" 1. Установить сервис с управлением через Telegram\n 2. Запустить\n 3. Остановить\n"
              " 4. Перезапустить\n 5. Удалить из автозапуска\n"
              " 6. Установить сервис для всех аккаунтов с единой бот-панелью\n 0. Назад")
        c = ask("Выбор")
        if c in (None, "0"):
            break
        if c == "1":
            print(install_service())
        elif c == "6":
            if len(account_profiles()) < 2:
                print("Дополнительные аккаунты добавляются из Telegram: открой бота → 👤 Аккаунты → ➕ Добавить аккаунт.")
            else:
                print(install_service(all_accounts=True))
        elif c in ("2", "3", "4", "5"):
            if not has_systemd():
                print("Нет systemd.")
                continue
            if c == "5":
                sc("disable", "--now", SERVICE)
                unit_path().unlink(missing_ok=True)
                sc("daemon-reload")
                print("Удалён.")
            else:
                act = {"2": "start", "3": "stop", "4": "restart"}[c]
                rc, out = sc(act, SERVICE)
                print(out or ("Готово." if rc == 0 else "Ошибка (нужен root?)"))


def show_log(cfg):
    p = data_dir(cfg) / "circles.log"
    if not p.exists():
        print("Лог пока пуст.")
    else:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        print("\n".join(lines))
    pause()


def setup_wizard():
    """Первичная настройка одной командой: ключи -> вход в аккаунт -> проверка -> автозапуск.
    После неё сервером можно больше не заниматься: всё управление - из бота в Telegram."""
    cfg = load_cfg()
    print("\n=== Первичная настройка ===")
    print("Нужно один раз. Дальше всё управление - из бота в Telegram.\n")
    steps = [("api_id", "API ID (my.telegram.org -> API development tools)", int),
             ("api_hash", "API HASH (оттуда же)", str),
             ("target_chat_id", "ID группы-форума для архива медиа (формат -100...)", int),
             ("bot_token", "Токен бота-панели (создай у @BotFather, формат 123456789:AAH...)", str)]
    # A previously interrupted setup saves each field immediately. Detect a malformed
    # positive chat ID so rerunning the wizard asks for it again instead of skipping it.
    if cfg.get("target_chat_id") and not re.fullmatch(r"-100\d+", str(cfg["target_chat_id"])):
        print("Сохранённый ID группы-форума некорректен. Запрошу его повторно.")
        cfg["target_chat_id"] = 0
        save_cfg(cfg)
    for key, title, conv in steps:
        while not cfg[key]:
            raw = ask(title)
            if not raw:
                continue
            try:
                val = conv(raw)
            except (TypeError, ValueError):
                print("Не похоже на то, что нужно. Ещё раз.")
                continue
            if key == "target_chat_id" and not re.fullmatch(r"-100\d+", str(val)):
                print("Нужен ID супергруппы/форума Telegram с префиксом -100, например -1001234567890.")
                continue
            if key == "bot_token" and not re.fullmatch(r"\d+:[\w-]{20,}", val):
                print("Это не похоже на токен. Формат: 123456789:AAH... (от @BotFather)")
                continue
            if key == "api_hash" and not val:
                continue
            cfg[key] = val
        save_cfg(cfg)
    ddir = data_dir(cfg)
    setup_logging(ddir)
    print("\nШаг 2/3. Вход в аккаунт Telegram (код придёт в Telegram).")
    telegram_action(cfg, lambda: run_login(cfg, ddir))
    print("\nШаг 3/3. Проверка.")

    def _check():
        async def inner():
            for ok, text in await run_check(cfg, ddir):
                print(("  [OK]  " if ok else "  [!!]  ") + text)
        return inner()
    telegram_action(cfg, _check)
    print("\nУстанавливаю автозапуск...")
    print(install_service())
    print("\nГотово. Осталось одно: открой своего бота в Telegram и нажми /start.")
    print("Дальше рассылка, статус, логи и ошибки - в боте. На сервер заходить не нужно.")
    return cfg


def menu():
    cfg = load_cfg()
    setup_logging(data_dir(cfg))
    while True:
        cfg = load_cfg()
        print("\n==============================================")
        print("   TELEGRAM КРУЖКИ - управление")
        print(f"   Сервис: {service_state()}")
        print("==============================================")
        print(" 1. Статус и статистика\n 2. Настройки\n 3. Войти в аккаунт Telegram\n"
              " 4. Проверить настройки (связь, группа, права)\n 5. Выгрузить старые медиа выбранных типов\n"
              " 6. Сервис (автозапуск, старт/стоп)\n 7. Люди и игнор-лист\n 8. Последние строки лога\n"
              " 9. Запустить архив медиа + рассылку в этом окне\n"
              " 0. Выход")
        c = ask("Выбор")
        try:
            if c in (None, "0"):
                return
            if c == "1":
                show_status(cfg)
            elif c == "2":
                settings_menu(cfg)
            elif c in ("3", "4", "5", "9"):
                bad = cfg_problems(cfg)
                if bad:
                    print("Сначала заполните настройки:", "; ".join(bad))
                    continue
                ddir = data_dir(cfg)
                if c == "9":
                    try:
                        iv = int(ask("Интервал между кругами (минут)", cfg["broadcast_interval_min"]))
                        hh = float(ask("Длительность рассылки (часов)", cfg["broadcast_hours"]))
                    except (TypeError, ValueError):
                        print("Нужны числа.")
                        continue
                    print("Работаю. Остановить - Ctrl+C.")
                    telegram_action(cfg, lambda: run_service(cfg, ddir, (iv, hh)))
                elif c == "3":
                    telegram_action(cfg, lambda: run_login(cfg, ddir))
                elif c == "4":
                    def _check():
                        async def inner():
                            for ok, text in await run_check(cfg, ddir):
                                print(("  [OK]  " if ok else "  [!!]  ") + text)
                        return inner()
                    telegram_action(cfg, _check)
                    pause()
                else:
                    lim = ask("Максимум медиафайлов одного типа на один чат (Enter - все)")
                    if yesno("Начать выгрузку?", True):
                        telegram_action(cfg, lambda: run_backfill(cfg, ddir, int(lim) if lim and lim.isdigit() else None))
                        pause()
            elif c == "6":
                service_menu(cfg)
            elif c == "7":
                people_menu(cfg)
            elif c == "8":
                show_log(cfg)
        except (KeyboardInterrupt, EOFError):
            print()
            return


# =============================================================================
#  Точка входа
# =============================================================================
def main():
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description="Архиватор медиа Telegram")
    ap.add_argument("command", nargs="?", choices=["menu", "run", "web", "login", "check", "backfill", "setup",
                                                        "accounts", "add-account"])
    ap.add_argument("profile_name", nargs="?", help="имя для add-account")
    ap.add_argument("--profile", help="имя профиля аккаунта для login/check/backfill/run/menu")
    ap.add_argument("--all-accounts", action="store_true",
                    help="запустить архиватор для всех профилей; массовая рассылка при этом выключена")
    ap.add_argument("--limit", type=int, default=None, help="для backfill: максимум архивируемых медиа на чат")
    ap.add_argument("--broadcast", action="store_true", help="для run: запустить рассылку этого одного профиля")
    ap.add_argument("--interval", type=int, default=None, help="для --broadcast: интервал, минут")
    ap.add_argument("--hours", type=float, default=None, help="для --broadcast: длительность, часов")
    args = ap.parse_args()
    cmd = args.command or ("menu" if sys.stdin.isatty() else "run")

    if cmd == "accounts":
        print_accounts()
        return
    if cmd == "add-account":
        if not args.profile_name:
            ap.error("Укажите имя: python combined.py add-account work")
        try:
            path = add_account_profile(args.profile_name)
        except Exception as e:
            ap.error(str(e))
        print(f"Создан профиль: {path}")
        print("Далее: python combined.py login --profile " + args.profile_name)
        print("Настройки и данные профиля изолированы. API ID/Hash унаследованы от основного профиля.")
        return

    if args.all_accounts and cmd != "run":
        ap.error("--all-accounts можно использовать только с командой run")
    if args.all_accounts and args.profile:
        ap.error("Не указывайте --profile вместе с --all-accounts")
    if args.all_accounts and args.broadcast:
        ap.error("Массовая рассылка запрещена в режиме --all-accounts. Запускайте один профиль отдельно.")

    if args.profile and cmd == "web":
        ap.error("--profile нельзя использовать с командой web")
    if args.profile:
        try:
            CONFIG_PATH = profile_config_path(args.profile)
        except ValueError as e:
            ap.error(str(e))
        if not CONFIG_PATH.exists():
            ap.error(f"Профиль '{args.profile}' не найден. Создайте его: python combined.py add-account {args.profile}")

    if cmd == "menu":
        return menu()
    if cmd == "setup":
        return setup_wizard()

    if args.all_accounts:
        # Backward-compatible with an older systemd ExecStart. The primary profile
        # now owns the single bot panel and starts named profiles as managed workers.
        print("Запускаю единый менеджер: все сохранённые аккаунты будут доступны в Telegram-боте.")

    cfg = load_cfg()
    bad = cfg_problems(cfg)
    if cmd == "web":
        bad = [problem for problem in bad if problem != "ID группы-форума должен быть в формате -100..."]
    if bad:
        print("Настройки не заполнены (" + "; ".join(bad) + "). Запустите: python combined.py", file=sys.stderr)
        sys.exit(3)
    ddir = data_dir(cfg)
    setup_logging(ddir)
    if cmd == "web":
        runtime_lock_dir = BASE / "manager_data"
        runtime_lock_dir.mkdir(parents=True, exist_ok=True)
        lk = acquire_lock(runtime_lock_dir)
    else:
        lk = acquire_lock(ddir)
    if lk is None:
        log.error("Другая копия уже работает с этой сессией.")
        sys.exit(4)

    try:
        if cmd == "web":
            sys.exit(asyncio.run(run_web_manager_host(cfg)))
        elif cmd == "run":
            bcast = ((args.interval or cfg["broadcast_interval_min"], args.hours or cfg["broadcast_hours"])
                     if args.broadcast else None)
            restart_args = ["run"] + (["--profile", args.profile] if args.profile else [])
            sys.exit(asyncio.run(run_service(cfg, ddir, bcast, restart_args=restart_args)))
        elif cmd == "login":
            asyncio.run(run_login(cfg, ddir))
        elif cmd == "check":
            for ok, text in asyncio.run(run_check(cfg, ddir)):
                print(("[OK] " if ok else "[!!] ") + text)
        elif cmd == "backfill":
            asyncio.run(run_backfill(cfg, ddir, args.limit))
    finally:
        if lk not in (None, True):
            lk.close()


if __name__ == "__main__":
    main()