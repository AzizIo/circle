"""
broadcast_core.py - ядро рассылки для Telethon.

Что внутри:
  classify()      - разбор ошибок Telegram -> вердикт (постоянный/временный отказ, цена, пауза)
  Store           - SQLite: паспорт чата, чёрный список со сроком перепроверки, статистика
  preflight()     - проверка прав/цены/слоумода ДО отправки, без пробных запросов
  Broadcaster     - планировщик: у каждого чата своё "когда можно", глобальная адаптивная пауза
  JoinQueue       - очередь подписок с суточным лимитом и кэшем мёртвых юзернеймов
  make_telethon_sender() - готовая функция отправки (форвард + запасной вариант без превью)

Подключение (набросок):

    store = Store("broadcast.db")
    chats = [d.entity async for d in client.iter_dialogs() if d.is_group or d.is_channel]
    ids = await preflight(client, store, chats, message_has_links=True)
    send = make_telethon_sender(client, from_peer, msg_id, fallback_text=text)
    bc = Broadcaster(store, send, cycle=3 * 3600)
    await bc.run(ids)

Платные чаты (ALLOW_PAYMENT_REQUIRED_N) модуль НЕ оплачивает - они просто уходят
в чёрный список на срок, цена запоминается. Оплата Stars сознательно не включена.
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional

log = logging.getLogger("broadcast")
DAY = 86400


# ───────────────────────── классификация ошибок ─────────────────────────

@dataclass
class Verdict:
    kind: str                 # paid / no_plain / no_links / topic_closed / no_rights / slowmode / flood
    ttl: Optional[int] = None  # секунд до перепроверки; None = навсегда (до ручной очистки)
    price: int = 0
    wait: int = 0
    scope: str = "chat"        # "chat" или "global" (FloodWait касается всего аккаунта)


_PERMANENT_CODES = (
    "CHAT_WRITE_FORBIDDEN", "CHAT_ADMIN_REQUIRED", "USER_BANNED_IN_CHANNEL",
    "CHANNEL_PRIVATE", "USER_NOT_PARTICIPANT", "CHAT_RESTRICTED", "CHANNEL_INVALID",
    "PEER_ID_INVALID", "CHAT_SEND_PLAIN_FORBIDDEN", "CHAT_SEND_MEDIA_FORBIDDEN",
    "CHAT_SEND_PHOTOS_FORBIDDEN", "CHAT_SEND_VIDEOS_FORBIDDEN", "CHAT_SEND_DOCS_FORBIDDEN",
    "CHAT_SEND_STICKERS_FORBIDDEN", "CHAT_SEND_GIFS_FORBIDDEN", "CHAT_SEND_AUDIOS_FORBIDDEN",
    "CHAT_SEND_VOICES_FORBIDDEN", "CHAT_SEND_ROUNDVIDEOS_FORBIDDEN", "CHAT_SEND_POLLS_FORBIDDEN",
)


def _seconds(exc: BaseException, text: str) -> int:
    s = getattr(exc, "seconds", None)
    if isinstance(s, int):
        return s
    m = re.search(r"(?:wait of|WAIT_|SLOWMODE_WAIT_)\s*(\d+)", text)
    return int(m.group(1)) if m else 60


def classify(exc: BaseException) -> Optional[Verdict]:
    """None = неизвестная ошибка (считаем временной с экспоненциальным откатом)."""
    name, text = type(exc).__name__, str(exc)

    if name == "SlowModeWaitError" or "SLOWMODE_WAIT" in text or "before sending another message in this chat" in text:
        return Verdict("slowmode", wait=_seconds(exc, text))
    if name == "FloodWaitError" or "FLOOD_WAIT" in text or name == "FloodPremiumWaitError":
        return Verdict("flood", wait=_seconds(exc, text), scope="global")
    if name == "PeerFloodError" or "PEER_FLOOD" in text:
        # PeerFlood is an account-level restriction, never a chat-local failure.
        return Verdict("account_restricted", wait=0, scope="account")
    if name in {"ChatForwardsRestrictedError", "MessageForwardsRestrictedError"} or \
            "CHAT_FORWARDS_RESTRICTED" in text or "MESSAGE_FORWARDS_RESTRICTED" in text:
        return Verdict("source_restricted", ttl=None, scope="source")
    if name in {"ChannelsTooMuchError", "UserChannelsTooMuchError"}:
        # Joining capacity is account-scoped; do not blacklist the current URL as dead.
        return Verdict("join_capacity", wait=DAY, scope="account")

    m = re.search(r"ALLOW_PAYMENT_REQUIRED_(\d+)", text)
    if m:
        price = int(m.group(1))
        # цену админ может менять: дешёвые перепроверяем чаще, дорогие почти не трогаем
        return Verdict("paid", ttl=(3 if price <= 100 else 30) * DAY, price=price)
    if "cannot send plain results" in text:
        return Verdict("no_plain", ttl=7 * DAY)
    if "TOPIC_CLOSED" in text:
        return Verdict("topic_closed", ttl=1 * DAY)
    if "CHAT_SEND_WEBPAGE_FORBIDDEN" in text:
        return Verdict("no_links", ttl=7 * DAY)
    if any(code in text for code in _PERMANENT_CODES):
        return Verdict("no_rights", ttl=30 * DAY)
    return None


# ───────────────────────────── хранилище ─────────────────────────────

class Store:
    def __init__(self, path: str = "broadcast.db"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20, isolation_level="DEFERRED")
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS chats(
            chat_id INTEGER PRIMARY KEY, title TEXT,
            status TEXT DEFAULT 'ok',          -- ok / blocked
            reason TEXT, price INTEGER DEFAULT 0, retry_at REAL,
            ok INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, unknown_streak INTEGER DEFAULT 0,
            last_ok REAL, last_err TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS bad_targets(target TEXT PRIMARY KEY, reason TEXT, until REAL);
        CREATE INDEX IF NOT EXISTS idx_chats_retry ON chats(status, retry_at);
        CREATE INDEX IF NOT EXISTS idx_bad_targets_until ON bad_targets(until);
        """)
        self.db.commit()

    def touch(self, chat_id: int, title: str = ""):
        now = time.time()
        self.db.execute(
            "INSERT INTO chats(chat_id,title,updated) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE chats.title END, "
            "updated=excluded.updated", (int(chat_id), str(title or ""), now))
        self.db.commit()

    def blocked_until(self, chat_id: int) -> Optional[float]:
        """None = чат свободен. float('inf') = заблокирован навсегда. Иначе время разблокировки."""
        r = self.db.execute("SELECT status,retry_at FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not r or r[0] != "blocked":
            return None
        if r[1] is None:
            return float("inf")
        return r[1] if r[1] > time.time() else None

    def block(self, chat_id: int, v: Verdict, err: str = ""):
        now = time.time()
        retry = None if v.ttl is None else now + v.ttl
        self.db.execute(
            "INSERT INTO chats(chat_id,status,reason,price,retry_at,fails,last_err,unknown_streak,updated) "
            "VALUES(?,'blocked',?,?,?,1,?,0,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET status='blocked',reason=excluded.reason,price=excluded.price,"
            "retry_at=excluded.retry_at,fails=chats.fails+1,last_err=excluded.last_err,"
            "unknown_streak=0,updated=excluded.updated",
            (int(chat_id), v.kind, int(v.price or 0), retry, str(err)[:200], now))
        self.db.commit()

    def mark_ok(self, chat_id: int):
        now = time.time()
        self.db.execute(
            "INSERT INTO chats(chat_id,status,ok,last_ok,updated) VALUES(?,'ok',1,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET status='ok',reason=NULL,retry_at=NULL,"
            "ok=chats.ok+1,unknown_streak=0,last_ok=excluded.last_ok,updated=excluded.updated",
            (int(chat_id), now, now))
        self.db.commit()

    def unknown_failure(self, chat_id: int, err: str) -> int:
        now = time.time()
        self.db.execute(
            "INSERT INTO chats(chat_id,status,fails,unknown_streak,last_err,updated) "
            "VALUES(?,'ok',1,1,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
            "fails=chats.fails+1,unknown_streak=chats.unknown_streak+1,last_err=excluded.last_err,updated=excluded.updated",
            (int(chat_id), str(err)[:200], now))
        self.db.commit()
        return self.db.execute("SELECT unknown_streak FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()[0]

    def report(self) -> str:
        rows = self.db.execute(
            "SELECT reason,COUNT(*) FROM chats WHERE status='blocked' AND (retry_at IS NULL OR retry_at>?) "
            "GROUP BY reason ORDER BY 2 DESC", (time.time(),)).fetchall()
        total = sum(n for _, n in rows)
        return f"на паузе {total} чатов" + (": " + ", ".join(f"{r}={n}" for r, n in rows) if rows else "")

    def close(self) -> None:
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.db.close()


# ─────────────────────── проверка до отправки ───────────────────────

def preflight_verdict(ent, message_has_links: bool = False) -> Optional[Verdict]:
    """Блокирует заранее ТОЛЬКО то, в чём можно быть уверенным по данным чата:
      - у чата есть цена за сообщение (платные сообщения);
      - лично тебе запретили писать (banned_rights для твоего аккаунта).
    Общие права группы (default_banned_rights: ссылки, обычный текст и т.п.) НЕ смотрим:
    админы и одобренные участники их обходят, а запрет 'plain' не касается медиа-постов.
    Остальное узнаём по первой ошибке и ставим чат на паузу со сроком (см. classify)."""
    price = getattr(ent, "send_paid_messages_stars", 0) or 0
    if price > 0:
        return Verdict("paid", ttl=3 * DAY, price=int(price))
    if getattr(ent, "left", False):
        return Verdict("no_rights", ttl=7 * DAY)
    own = getattr(ent, "banned_rights", None)
    if own is not None and getattr(own, "send_messages", False):
        until = getattr(own, "until_date", None)
        if until is None or getattr(until, "timestamp", lambda: float("inf"))() > time.time():
            return Verdict("no_rights", ttl=7 * DAY)
    return None


async def preflight(client, store: Store, entities: Iterable, message_has_links: bool = False) -> list[int]:
    """Возвращает id чатов, куда отправка вообще возможна. Заодно пополняет базу."""
    entities = list(entities)
    ids = []
    for ent in entities:
        cid = ent.id
        store.touch(cid, getattr(ent, "title", "") or "")
        until = store.blocked_until(cid)
        if until is not None:
            continue
        v = preflight_verdict(ent, message_has_links)
        if v:
            store.block(cid, v, "preflight")
            continue
        ids.append(cid)
    log.info("preflight: допущено %d из %d", len(ids), len(entities))
    return ids


# ───────────────────────────── планировщик ─────────────────────────────

SendFn = Callable[[int], Awaitable[bool]]


class Broadcaster:
    """Один воркер, очередь по времени. Нет волн и пропусков 'SlowMode - пропускаю':
    чат с слоумодом просто получает свою очередь позже."""

    def __init__(self, store: Store, send: SendFn, *, cycle: float = 3 * 3600,
                 base_gap: float = 4.0, max_gap: float = 60.0, jitter: float = 0.25,
                 unknown_limit: int = 5):
        self.store, self.send = store, send
        self.cycle, self.base_gap, self.max_gap = cycle, base_gap, max_gap
        self.jitter, self.unknown_limit = jitter, unknown_limit
        self.gap = base_gap
        self.pause_until = 0.0
        self._last_send = 0.0
        self._streak_ok = 0
        self.sent = self.failed = 0
        self.account_restricted = False
        self.source_restricted = False

    def _j(self, x: float) -> float:
        return x * random.uniform(1 - self.jitter, 1 + self.jitter)

    async def run(self, chat_ids: Iterable[int], stop: Optional[asyncio.Event] = None):
        stop = stop or asyncio.Event()
        now = time.time()
        heap = [(now + i * self.base_gap, cid) for i, cid in enumerate(chat_ids)]
        heapq.heapify(heap)

        while (heap and not stop.is_set()
               and not self.account_restricted and not self.source_restricted):
            due, cid = heap[0]
            ready = max(due, self.pause_until, self._last_send + self._j(self.gap))
            delay = ready - time.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=min(delay, 30))
                except asyncio.TimeoutError:
                    pass
                continue  # пересчитать: за время ожидания могла прийти пауза
            heapq.heappop(heap)

            until = self.store.blocked_until(cid)
            if until is not None:
                if until != float("inf"):
                    heapq.heappush(heap, (until + 1, cid))  # вернуться, когда срок истечёт
                continue

            self._last_send = time.time()
            try:
                send_result = await self.send(cid)
                if send_result is not True:
                    raise RuntimeError(f"sender returned non-success result: {send_result!r}")
            except Exception as e:  # noqa: BLE001
                self.failed += 1
                self._on_error(cid, e, heap)
                continue
            self.sent += 1
            self.store.mark_ok(cid)
            self._streak_ok += 1
            if self._streak_ok >= 30:                      # медленно возвращаем скорость
                self.gap = max(self.base_gap, self.gap * 0.9)
                self._streak_ok = 0
            heapq.heappush(heap, (time.time() + self._j(self.cycle), cid))

        log.info("рассылка остановлена: отправлено %d, ошибок %d | %s", self.sent, self.failed, self.store.report())

    def _on_error(self, cid: int, e: Exception, heap: list):
        v = classify(e)
        now = time.time()
        if v is None:                                       # неизвестное: откат 1м,2м,4м... потом бан на сутки
            n = self.store.unknown_failure(cid, f"{type(e).__name__}: {e}")
            if n >= self.unknown_limit:
                self.store.block(cid, Verdict("unknown", ttl=DAY), str(e))
                heapq.heappush(heap, (now + DAY + 1, cid))
            else:
                heapq.heappush(heap, (now + 60 * 2 ** n, cid))
            log.warning("неизвестная ошибка в %s: %s", cid, e)
            return
        if v.scope == "account":
            self.account_restricted = True
            log.error("Останавливаю Broadcaster: ограничение уровня аккаунта (%s)", v.kind)
            return
        if v.scope == "source":
            self.source_restricted = True
            log.error("Останавливаю Broadcaster: Telegram запретил пересылку исходного сообщения (%s)", v.kind)
            return
        if v.scope == "global":                             # FloodWait: тормозим весь аккаунт
            self.pause_until = now + max(1, v.wait) + random.uniform(3, 10)
            self.gap = min(self.gap * 1.5, self.max_gap)
            self._streak_ok = 0
            heapq.heappush(heap, (self.pause_until, cid))
            log.warning("FloodWait %ss: пауза, интервал между отправками теперь %.1fs", v.wait, self.gap)
            return
        if v.kind == "slowmode":                            # не отказ: просто ждём слоумод
            heapq.heappush(heap, (now + v.wait + random.uniform(3, 10), cid))
            return
        self.store.block(cid, v, f"{type(e).__name__}: {e}")
        log.info("чат %s -> чёрный список (%s%s)", cid, v.kind, f", {v.price}★" if v.price else "")
        if v.ttl is not None:
            heapq.heappush(heap, (now + v.ttl + 1, cid))    # перепроверка после срока


# ─────────────────────────── отправка через Telethon ───────────────────────────

def make_telethon_sender(client, from_peer, msg_id: int, fallback_text: Optional[str] = None) -> SendFn:
    async def send(chat_id: int) -> bool:
        try:
            res = await client.forward_messages(chat_id, msg_id, from_peer)
        except Exception as e:  # noqa: BLE001
            # запрет превью/ссылок: один раз пробуем обычной отправкой без превью
            if fallback_text and "WEBPAGE_FORBIDDEN" in str(e):
                res = await client.send_message(chat_id, fallback_text, link_preview=False)
            else:
                raise
        m = res[0] if isinstance(res, list) and res else res
        if not m or not getattr(m, "id", None):             # успех = вернулся message_id, а не "нет исключения"
            raise RuntimeError("Telegram не вернул id сообщения")
        return True
    return send


# ───────────────────────────── очередь подписок ─────────────────────────────


class DurableActionQueue:
    """Small per-account SQLite queue for Telegram join/start actions.

    Payloads may contain private invite hashes or bot start parameters. The DB is
    therefore created with owner-only permissions and must not be included in
    portable backups. Raw payloads are never returned by list_recent().
    """

    ACTIVE_STATUSES = ("queued", "retry", "waiting_approval", "processing")

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self.db = sqlite3.connect(self.path, timeout=20, isolation_level="DEFERRED")
        finally:
            os.umask(old_umask)
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS autosub_jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            source_chat_id INTEGER,
            source_message_id INTEGER,
            result TEXT,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_autosub_jobs_due
            ON autosub_jobs(status, next_attempt_at, id);
        CREATE TABLE IF NOT EXISTS autosub_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS autosub_action_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            occurred_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autosub_action_log_time
            ON autosub_action_log(kind, occurred_at);
        """)
        self.db.commit()
        self._secure_permissions()
        # A process can die after claiming a job. At-most-once is not possible for
        # Telegram RPCs, so return it to the queue; processors must verify existing
        # membership / sent / invite state before repeating an action.
        self.db.execute("UPDATE autosub_jobs SET status='queued', updated_at=? WHERE status='processing'",
                        (time.time(),))
        self.db.commit()

    def _secure_permissions(self):
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass

    @staticmethod
    def action_key(action: dict) -> str:
        canonical = json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def enqueue_many(self, actions: Iterable[dict], source_chat_id=None, source_message_id=None) -> tuple[int, int]:
        inserted = duplicates = 0
        now = time.time()
        with self.db:
            for action in actions:
                if not isinstance(action, dict) or not action.get("type") or not action.get("value"):
                    continue
                payload = json.dumps(action, ensure_ascii=False, sort_keys=True)
                key = self.action_key(action)
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO autosub_jobs(action_key,payload_json,status,created_at,updated_at,"
                    "next_attempt_at,source_chat_id,source_message_id) VALUES(?,?,'queued',?,?,0,?,?)",
                    (key, payload, now, now, source_chat_id, source_message_id))
                if cur.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
        self._secure_permissions()
        return inserted, duplicates

    def claim_next(self, now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else float(now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            pause = self.db.execute("SELECT value FROM autosub_meta WHERE key='pause_until'").fetchone()
            reason = self.db.execute("SELECT value FROM autosub_meta WHERE key='pause_reason'").fetchone()
            if pause:
                try:
                    pause_until = float(pause[0])
                except (TypeError, ValueError):
                    pause_until = -1
                if pause_until < 0 or pause_until > now:
                    self.db.commit()
                    return None
            row = self.db.execute(
                "SELECT id,payload_json,attempts,status,source_chat_id,source_message_id "
                "FROM autosub_jobs WHERE status IN ('queued','retry','waiting_approval') "
                "AND next_attempt_at<=? ORDER BY id LIMIT 1", (now,)).fetchone()
            if not row:
                self.db.commit()
                return None
            job_id, payload_json, attempts, previous_status, source_chat_id, source_message_id = row
            self.db.execute("UPDATE autosub_jobs SET status='processing',attempts=attempts+1,updated_at=? WHERE id=?",
                            (now, job_id))
            self.db.commit()
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = {}
            return {"id": int(job_id), "payload": payload, "attempts": int(attempts) + 1,
                    "previous_status": str(previous_status),
                    "source_chat_id": source_chat_id, "source_message_id": source_message_id}
        except Exception:
            self.db.rollback()
            raise

    def finish(self, job_id: int, status: str, *, result: str = "", error: str = "",
               next_attempt_at: float = 0.0) -> None:
        allowed = {"completed", "already_done", "waiting_approval", "retry", "needs_review",
                   "failed_permanent", "unsupported", "account_paused"}
        if status not in allowed:
            raise ValueError(f"Invalid autosub status: {status}")
        self.db.execute(
            "UPDATE autosub_jobs SET status=?,result=?,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
            (status, str(result)[:120], str(error)[:240], float(next_attempt_at or 0), time.time(), int(job_id)))
        self.db.commit()
        self._secure_permissions()

    def set_pause(self, until: Optional[float], reason: str) -> None:
        # None means manual/account-level pause until the owner explicitly resumes.
        value = -1.0 if until is None else float(until)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO autosub_meta(key,value) VALUES('pause_until',?)", (str(value),))
            self.db.execute("INSERT OR REPLACE INTO autosub_meta(key,value) VALUES('pause_reason',?)", (str(reason)[:160],))
        self._secure_permissions()

    def pause_info(self) -> tuple[Optional[float], str]:
        row = self.db.execute("SELECT value FROM autosub_meta WHERE key='pause_until'").fetchone()
        reason_row = self.db.execute("SELECT value FROM autosub_meta WHERE key='pause_reason'").fetchone()
        if not row:
            return None, str(reason_row[0]) if reason_row else ""
        try:
            until = float(row[0])
        except (TypeError, ValueError):
            until = -1.0
        return until, str(reason_row[0]) if reason_row else ""

    def clear_pause(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM autosub_meta WHERE key IN ('pause_until','pause_reason')")

    def next_due(self) -> Optional[float]:
        row = self.db.execute(
            "SELECT MIN(next_attempt_at) FROM autosub_jobs WHERE status IN ('queued','retry','waiting_approval')"
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def record_action(self, kind: str, occurred_at: Optional[float] = None) -> None:
        """Persist a completed join/start action and its batch counter atomically."""
        now = time.time() if occurred_at is None else float(occurred_at)
        kind = "join" if kind == "join" else "bot"
        with self.db:
            self.db.execute("INSERT INTO autosub_action_log(kind,occurred_at) VALUES(?,?)", (kind, now))
            row = self.db.execute(
                "SELECT value FROM autosub_meta WHERE key='batch_action_count'"
            ).fetchone()
            try:
                batch_count = max(0, int(row[0])) if row else 0
            except (TypeError, ValueError):
                batch_count = 0
            self.db.execute(
                "INSERT OR REPLACE INTO autosub_meta(key,value) VALUES('batch_action_count',?)",
                (str(batch_count + 1),),
            )
            # Keep a bounded audit window; the limiter only needs the latest 30 days.
            self.db.execute("DELETE FROM autosub_action_log WHERE occurred_at<?", (now - 30 * DAY,))
        self._secure_permissions()

    def batch_action_count(self) -> int:
        """Return the persisted number of completed actions in the current batch."""
        row = self.db.execute(
            "SELECT value FROM autosub_meta WHERE key='batch_action_count'"
        ).fetchone()
        try:
            return max(0, int(row[0])) if row else 0
        except (TypeError, ValueError):
            return 0

    def reset_batch_action_count(self) -> None:
        """Reset the batch counter after the scheduled rest has been committed."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO autosub_meta(key,value) VALUES('batch_action_count','0')"
            )
        self._secure_permissions()

    def start_batch_pause(self, until: float, reason: str) -> None:
        """Atomically persist a planned rest and reset the completed-action counter."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO autosub_meta(key,value) VALUES('pause_until',?)",
                (str(float(until)),),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO autosub_meta(key,value) VALUES('pause_reason',?)",
                (str(reason)[:160],),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO autosub_meta(key,value) VALUES('batch_action_count','0')"
            )
        self._secure_permissions()

    def rate_limit_wait(self, *, gap_seconds: float = 90.0, daily_limit: int = 15,
                        now: Optional[float] = None) -> tuple[float, str]:
        """Return persisted cooldown needed before another join/start action.

        The gap applies to all completed actions; the rolling daily cap applies
        only to channel/group joins. Values are conservative defaults, not claims
        about Telegram's unpublished universal limits.
        """
        now = time.time() if now is None else float(now)
        gap = max(0.0, float(gap_seconds))
        limit = max(0, int(daily_limit))
        row = self.db.execute("SELECT MAX(occurred_at) FROM autosub_action_log").fetchone()
        last_action = float(row[0]) if row and row[0] is not None else None
        wait_gap = max(0.0, last_action + gap - now) if last_action is not None else 0.0
        wait_daily = 0.0
        if limit > 0:
            rows = self.db.execute(
                "SELECT occurred_at FROM autosub_action_log WHERE kind='join' AND occurred_at>? "
                "ORDER BY occurred_at ASC", (now - DAY,)).fetchall()
            if len(rows) >= limit:
                wait_daily = max(0.0, float(rows[0][0]) + DAY - now)
        if wait_daily >= wait_gap and wait_daily > 0:
            return wait_daily, "daily_join_limit"
        if wait_gap > 0:
            return wait_gap, "action_gap"
        return 0.0, ""

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status,COUNT(*) FROM autosub_jobs GROUP BY status").fetchall()
        return {str(status): int(count) for status, count in rows}

    def list_recent(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,status,attempts,created_at,updated_at,next_attempt_at,source_chat_id,"
            "source_message_id,result,last_error FROM autosub_jobs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),)).fetchall()
        keys = ("id", "status", "attempts", "created_at", "updated_at", "next_attempt_at",
                "source_chat_id", "source_message_id", "result", "last_error")
        return [dict(zip(keys, row)) for row in rows]

    def close(self) -> None:
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.db.close()

class JoinQueue:
    """Legacy in-memory compatibility queue.

    The current application uses DurableActionQueue instead, which persists jobs,
    rate-limit history and account pauses in the per-profile SQLite database.
    """

    def __init__(self, store: Store, join: Callable[[str], Awaitable[None]],
                 daily_limit: int = 15, gap: float = 90.0):
        self.store, self.join = store, join
        self.daily_limit, self.gap = daily_limit, gap
        self.q: asyncio.Queue[str] = asyncio.Queue()
        self._stamps: deque[float] = deque()
        self._pause_until = 0.0

    def add(self, target: str):
        r = self.store.db.execute("SELECT until FROM bad_targets WHERE target=?", (target,)).fetchone()
        if r and (r[0] is None or r[0] > time.time()):
            return
        self.q.put_nowait(target)

    def _bad(self, target: str, reason: str, ttl: Optional[int]):
        self.store.db.execute("INSERT OR REPLACE INTO bad_targets VALUES(?,?,?)",
                              (target, reason, None if ttl is None else time.time() + ttl))
        self.store.db.commit()

    async def _wait_stop(self, stop: asyncio.Event, seconds: float) -> bool:
        """Wait for at most `seconds`; return True when a stop was requested."""
        if seconds <= 0:
            return stop.is_set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return stop.is_set()

    async def run(self, stop: Optional[asyncio.Event] = None):
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                target = await asyncio.wait_for(self.q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            requeue = False
            try:
                while time.time() < self._pause_until and not stop.is_set():
                    if await self._wait_stop(stop, min(5.0, self._pause_until - time.time())):
                        requeue = True
                        return
                if stop.is_set():
                    requeue = True
                    return

                now = time.time()
                while self._stamps and now - self._stamps[0] > DAY:
                    self._stamps.popleft()
                if len(self._stamps) >= self.daily_limit:
                    wait_for_limit = max(1.0, DAY - (now - self._stamps[0]))
                    requeue = True
                    if await self._wait_stop(stop, wait_for_limit):
                        return
                    # Re-evaluate the rolling window before claiming this target.
                    continue
                if stop.is_set():
                    requeue = True
                    return

                try:
                    await self.join(target)
                    self._stamps.append(time.time())
                except asyncio.CancelledError:
                    requeue = True
                    raise
                except Exception as e:  # noqa: BLE001
                    t = str(e)
                    if "joined too many channels" in t:
                        self._pause_until = time.time() + DAY
                        requeue = True
                        log.warning("лимит подписок: очередь на паузе 24ч (освободи слоты, см. leave_dead)")
                    elif "wait of" in t or type(e).__name__ == "FloodWaitError":
                        w = _seconds(e, t)
                        self._pause_until = time.time() + w + 10
                        requeue = True
                    elif "Nobody is using this username" in t or "No user has" in t:
                        self._bad(target, "dead_username", None)
                    else:
                        self._bad(target, t[:100], DAY)
                if not requeue and await self._wait_stop(stop, self._j()):
                    return
            finally:
                if requeue:
                    self.q.put_nowait(target)
                self.q.task_done()

    def _j(self) -> float:
        return self.gap * random.uniform(0.8, 1.3)


async def leave_dead(client, store: Store, limit: int = 10, reasons=("no_rights", "no_plain")) -> int:
    """Выходит из чатов с постоянным отказом, чтобы освободить слоты подписок.
    НЕ вызывается автоматически - запускай руками и с небольшим limit."""
    q = ",".join("?" * len(reasons))
    rows = store.db.execute(
        f"SELECT chat_id FROM chats WHERE status='blocked' AND reason IN ({q}) LIMIT ?", (*reasons, limit)).fetchall()
    n = 0
    for (cid,) in rows:
        try:
            await client.delete_dialog(cid)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("не вышел из %s: %s", cid, e)
        await asyncio.sleep(random.uniform(5, 15))
    return n