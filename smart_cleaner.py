"""Smart Cleaner engine for CIRCLES CONTROL.

This module deliberately accepts an already-connected Telethon client. It never
creates a TelegramClient, logs in, edits session files, or touches CIRCLES' DBs.
Each profile gets an isolated smart_cleaner.sqlite3 in its existing data_dir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("circles.smart_cleaner")

# Lightweight, explainable rules. We intentionally classify into "uncertain"
# rather than guessing when evidence is weak.
CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "crypto": ("crypto", "bitcoin", "btc", "eth", "usdt", "ton", "blockchain", "крипт", "биткоин", "токен", "кошел", "трейд", "трейдинг", "airdrop", "web3", "nft"),
    "jobs": ("job", "vacancy", "career", "работа", "ваканс", "резюме", "подработ", "зарплат", "фриланс", "hire"),
    "shopping": ("shop", "sale", "discount", "купить", "магазин", "скидк", "распрод", "товар", "маркет", "промокод"),
    "news": ("news", "новости", "сми", "breaking", "политик", "экономик", "общество", "события"),
    "education": ("курс", "обучен", "урок", "школ", "университет", "education", "learn", "study", "tutorial"),
    "entertainment": ("мем", "юмор", "funny", "music", "музык", "кино", "фильм", "игр", "game", "развлеч"),
    "marketing": ("маркетинг", "smm", "реклам", "продвиж", "таргет", "marketing", "traffic", "лид", "продаж"),
    "finance": ("банк", "инвест", "финанс", "акци", "бирж", "forex", "forex", "облигац", "финансы", "экономика"),
    "real_estate": ("недвиж", "аренд", "квартир", "дом", "риелтор", "real estate", "property", "жильё", "жилье"),
    "local_community": ("район", "город", "сосед", "чат жильцов", "community", "local", "афиша", "мероприят"),
}


def _clean(value: Any, limit: int = 4000) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _term_present(text: str, term: str) -> bool:
    # Very short Latin acronyms (TON/BTC/ETH/NFT) must be whole tokens;
    # substring matching would incorrectly classify e.g. "button" as TON.
    if term.isascii() and term.isalnum() and len(term) <= 4:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text, re.I) is not None
    return term in text


def classify_combined(title: str, username: str = "", about: str = "", samples: Iterable[str] = ()) -> tuple[str, float, dict[str, int]]:
    """Return (category, confidence, scores) from title/about/recent messages."""
    title_text = _clean(f"{title} {username}", 1000).casefold()
    about_text = _clean(about, 2000).casefold()
    sample_text = _clean(" ".join(samples), 6000).casefold()
    scores: dict[str, int] = {}
    for category, terms in CATEGORY_TERMS.items():
        score = 0
        for term in terms:
            # Title is strongest evidence, then about, then recent message sample.
            if _term_present(title_text, term):
                score += 4
            if _term_present(about_text, term):
                score += 2
            if _term_present(sample_text, term):
                score += 1
        if score:
            scores[category] = score
    if not scores:
        return "uncertain", 0.0, {}
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_score = ranked[0][1]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    # Low evidence or near-tie is deliberately left for human review.
    if top_score < 3 or (second_score and top_score - second_score < 2):
        return "uncertain", min(0.49, top_score / 12), scores
    confidence = min(0.98, 0.45 + (top_score - second_score) / max(8, top_score * 2))
    return ranked[0][0], round(confidence, 3), scores


def _dialog_kind(dialog: Any) -> str:
    entity = getattr(dialog, "entity", None)
    name = type(entity).__name__
    if name == "Channel":
        return "channel" if bool(getattr(entity, "broadcast", False)) else "group"
    if name == "Chat":
        return "group"
    return "other"


def _safe_bool(entity: Any, attr: str) -> bool:
    return bool(getattr(entity, attr, False))


class SmartCleaner:
    """Per-profile catalog, preview and resumable leave queue."""

    def __init__(self, data_dir: str | Path, *, whitelist: Iterable[str] = (), min_delay: float = 4.0):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        self.path = self.data_dir / "smart_cleaner.sqlite3"
        self.whitelist = tuple(_clean(x, 256).casefold().lstrip("@") for x in whitelist if _clean(x, 256))
        self.min_delay = max(2.0, float(min_delay))
        self._lock = asyncio.Lock()
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level="IMMEDIATE")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS catalog (
            chat_id INTEGER PRIMARY KEY, peer_type TEXT NOT NULL, title TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '', about TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'uncertain', confidence REAL NOT NULL DEFAULT 0,
            scores_json TEXT NOT NULL DEFAULT '{}', sample_text TEXT NOT NULL DEFAULT '',
            is_creator INTEGER NOT NULL DEFAULT 0, is_admin INTEGER NOT NULL DEFAULT 0,
            is_scam INTEGER NOT NULL DEFAULT 0, unread_count INTEGER NOT NULL DEFAULT 0,
            is_muted INTEGER NOT NULL DEFAULT 0, is_archived INTEGER NOT NULL DEFAULT 0,
            last_message_at TEXT, protected_reason TEXT NOT NULL DEFAULT '',
            scanned_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_catalog_category ON catalog(category, peer_type);
        CREATE INDEX IF NOT EXISTS idx_catalog_scanned ON catalog(scanned_at);
        CREATE TABLE IF NOT EXISTS cleanup_jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, category TEXT NOT NULL,
            target_ids_json TEXT NOT NULL, done_json TEXT NOT NULL DEFAULT '[]',
            failed_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
            updated_at REAL NOT NULL, last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS cleanup_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL, title TEXT NOT NULL, result TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cleanup_events_job ON cleanup_events(job_id, id);
        """)
        self.db.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _is_rate_limit(exc: BaseException) -> bool:
        name, message = type(exc).__name__, str(exc).upper()
        return name in {"FloodWaitError", "FloodPremiumWaitError", "PeerFloodError"} or "FLOOD_WAIT" in message or "PEER_FLOOD" in message

    async def _fetch_about(self, client: Any, entity: Any, kind: str) -> str:
        """Fetch a full Telegram description only when metadata did not classify the chat."""
        if hasattr(client, "get_full_chat_about"):
            value = await client.get_full_chat_about(entity)
            return _clean(value, 2000)
        try:
            if kind == "channel":
                from telethon.tl.functions.channels import GetFullChannelRequest
                result = await client(GetFullChannelRequest(entity))
            else:
                from telethon.tl.functions.messages import GetFullChatRequest
                result = await client(GetFullChatRequest(entity.id))
            return _clean(getattr(getattr(result, "full_chat", None), "about", ""), 2000)
        except Exception as exc:
            if self._is_rate_limit(exc):
                raise
            log.debug("full description unavailable for %s: %s", getattr(entity, "id", "?"), type(exc).__name__)
            return ""

    async def scan(self, client: Any, *, sample_messages: int = 5, progress=None) -> dict[str, Any]:
        """Scan dialogs once; enrich only uncertain titles with full info/history.

        This staged strategy avoids one history request per group when the title
        already provides a strong classification, keeping large accounts snappy.
        """
        scanned = 0
        eligible = 0
        now = time.time()
        partial_reason = ""
        async with self._lock:
            try:
                async for dialog in client.iter_dialogs():
                    entity = getattr(dialog, "entity", None)
                    kind = _dialog_kind(dialog)
                    if entity is None or kind not in {"group", "channel"}:
                        continue
                    eligible += 1
                    title = _clean(getattr(dialog, "name", "") or getattr(entity, "title", ""), 512)
                    username = _clean(getattr(entity, "username", "") or "", 128).casefold()
                    about = _clean(getattr(entity, "about", "") or "", 2000)
                    samples: list[str] = []
                    category, confidence, scores = classify_combined(title, username, about, ())
                    # Ask for Telegram's full description only when name/available metadata
                    # is ambiguous. Recent messages are the final evidence layer.
                    if category == "uncertain" or confidence < 0.76:
                        if not about:
                            about = await self._fetch_about(client, entity, kind)
                        category, confidence, scores = classify_combined(title, username, about, ())
                    if category == "uncertain" and sample_messages > 0:
                        try:
                            async for message in client.iter_messages(entity, limit=max(0, min(int(sample_messages), 10))):
                                body = _clean(getattr(message, "message", "") or "", 1000)
                                if body:
                                    samples.append(body)
                        except Exception as exc:  # inaccessible history is not a catalog failure
                            if self._is_rate_limit(exc):
                                raise
                            log.debug("recent-message sample unavailable for %s: %s", getattr(entity, "id", "?"), type(exc).__name__)
                        category, confidence, scores = classify_combined(title, username, about, samples)
                    creator = _safe_bool(entity, "creator")
                    admin = bool(getattr(entity, "admin_rights", None))
                    scam = _safe_bool(entity, "scam") or _safe_bool(entity, "fake")
                    chat_id = int(getattr(dialog, "id", getattr(entity, "id")))
                    protected = "owner" if creator else "admin" if admin else "whitelist" if self._is_whitelisted(chat_id, title, username) else ""
                    last_date = getattr(dialog, "date", None)
                    last_iso = last_date.astimezone(timezone.utc).isoformat() if isinstance(last_date, datetime) else None
                    mute_until = getattr(getattr(getattr(dialog, "dialog", None), "notify_settings", None), "mute_until", None)
                    muted = bool(mute_until and isinstance(mute_until, datetime) and mute_until > datetime.now(timezone.utc))
                    archived = bool(getattr(dialog, "archived", False))
                    sample = " | ".join(samples)[:6000]
                    self.db.execute("""INSERT INTO catalog(
                        chat_id,peer_type,title,username,about,category,confidence,scores_json,sample_text,
                        is_creator,is_admin,is_scam,unread_count,is_muted,is_archived,last_message_at,
                        protected_reason,scanned_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(chat_id) DO UPDATE SET peer_type=excluded.peer_type,title=excluded.title,
                        username=excluded.username,about=excluded.about,category=excluded.category,
                        confidence=excluded.confidence,scores_json=excluded.scores_json,sample_text=excluded.sample_text,
                        is_creator=excluded.is_creator,is_admin=excluded.is_admin,is_scam=excluded.is_scam,
                        unread_count=excluded.unread_count,is_muted=excluded.is_muted,is_archived=excluded.is_archived,
                        last_message_at=excluded.last_message_at,protected_reason=excluded.protected_reason,
                        scanned_at=excluded.scanned_at,updated_at=excluded.updated_at""",
                        (chat_id, kind, title, username, about, category, confidence, json.dumps(scores), sample,
                         int(creator), int(admin), int(scam), int(getattr(dialog, "unread_count", 0) or 0),
                         int(muted), int(archived), last_iso, protected, now, now))
                    scanned += 1
                    if scanned % 25 == 0:
                        self.db.commit()
                    if progress and scanned % 50 == 0:
                        result = progress(scanned, eligible)
                        if asyncio.iscoroutine(result):
                            await result
            except asyncio.CancelledError:
                self.db.commit()
                raise
            except Exception as exc:
                if self._is_rate_limit(exc):
                    partial_reason = f"Сканирование остановлено ограничением Telegram ({type(exc).__name__}); повторите позже"
                    log.warning("Smart Cleaner scan paused by Telegram rate limit: %s", type(exc).__name__)
                else:
                    self.db.commit()
                    raise
            if not partial_reason:
                # Remove dialogs no longer present only after a complete scan.
                self.db.execute("DELETE FROM catalog WHERE scanned_at < ?", (now,))
            self.db.commit()
        counts = {row[0]: row[1] for row in self.db.execute("SELECT category,COUNT(*) FROM catalog GROUP BY category")}
        return {"scanned": scanned, "eligible": eligible, "categories": counts,
                "partial": bool(partial_reason), "warning": partial_reason, "scanned_at": now}

    def _is_whitelisted(self, chat_id: int, title: str, username: str) -> bool:
        title_l, username_l = title.casefold(), username.casefold()
        for token in self.whitelist:
            if token.lstrip("-").isdigit():
                if token in {str(chat_id), str(abs(chat_id))}:
                    return True
            elif token in title_l or token == username_l:
                return True
        return False

    def list_catalog(self, *, category: str | None = None, query: str = "", peer_type: str | None = None,
                     include_protected: bool = True, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        where, args = [], []
        if category:
            where.append("category=?"); args.append(category)
        if peer_type in {"group", "channel"}:
            where.append("peer_type=?"); args.append(peer_type)
        if query.strip():
            q = f"%{_clean(query, 120)}%"
            where.append("(title LIKE ? OR username LIKE ? OR sample_text LIKE ?)"); args.extend([q, q, q])
        if not include_protected:
            where.append("protected_reason=''")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute("SELECT COUNT(*) FROM catalog" + clause, args).fetchone()[0]
        rows = self.db.execute("SELECT * FROM catalog" + clause +
                               " ORDER BY is_archived ASC, unread_count DESC, title COLLATE NOCASE LIMIT ? OFFSET ?",
                               (*args, max(1, min(int(limit), 500)), max(0, int(offset)))).fetchall()
        return {"total": total, "items": [self._row_dict(r) for r in rows]}

    def catalog_stats(self) -> dict[str, Any]:
        """Fast counts from the local catalog; no Telegram requests are made."""
        total = self.db.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
        groups, channels = self.db.execute(
            "SELECT SUM(peer_type='group'), SUM(peer_type='channel') FROM catalog").fetchone()
        protected = self.db.execute(
            "SELECT COUNT(*) FROM catalog WHERE protected_reason<>'' OR is_creator=1 OR is_admin=1 OR is_scam=1").fetchone()[0]
        unread = self.db.execute("SELECT COALESCE(SUM(unread_count),0) FROM catalog").fetchone()[0]
        categories = {str(row[0]): int(row[1]) for row in self.db.execute(
            "SELECT category,COUNT(*) FROM catalog GROUP BY category ORDER BY COUNT(*) DESC")}
        last_scan = self.db.execute("SELECT MAX(scanned_at) FROM catalog").fetchone()[0]
        return {"total": int(total), "groups": int(groups or 0), "channels": int(channels or 0),
                "protected": int(protected), "unread": int(unread), "categories": categories,
                "last_scan": float(last_scan) if last_scan else None}

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["scores"] = json.loads(d.pop("scores_json") or "{}")
        d["protected"] = bool(d.get("protected_reason"))
        return d

    def preview(self, *, category: str, chat_ids: Iterable[int] | None = None,
                allow_scam: bool = False) -> dict[str, Any]:
        if chat_ids is not None:
            ids = sorted({int(x) for x in chat_ids})
            if not ids:
                raise ValueError("Не выбраны чаты")
            if len(ids) > 500:
                raise ValueError("За один предпросмотр можно выбрать не более 500 чатов")
            rows = self.db.execute("SELECT * FROM catalog WHERE chat_id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
        else:
            if category == "all":
                rows = self.db.execute("SELECT * FROM catalog").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM catalog WHERE category=?", (category,)).fetchall()
        eligible, protected = [], []
        for row in rows:
            item = self._row_dict(row)
            reason = item["protected_reason"]
            if reason or item["is_creator"] or item["is_admin"]:
                protected.append({"id": item["chat_id"], "title": item["title"], "reason": reason or "admin/owner"})
                continue
            if item["peer_type"] not in {"group", "channel"}:
                continue
            if item["is_scam"] and not allow_scam:
                protected.append({"id": item["chat_id"], "title": item["title"], "reason": "scam/fake — требуется отдельный выбор"})
                continue
            eligible.append(item)
        if not eligible:
            raise ValueError("После защитных исключений нет чатов для выхода")
        preview_id = secrets.token_urlsafe(18)
        # A preview is immutable and one-use; it expires after 15 minutes.
        self.db.execute("INSERT INTO cleanup_jobs(id,status,category,target_ids_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (preview_id, "preview", category, json.dumps([x["chat_id"] for x in eligible]), time.time(), time.time()))
        self.db.commit()
        return {"preview_id": preview_id, "category": category, "count": len(eligible),
                "confirmation": f"LEAVE {len(eligible)}", "expires_in_seconds": 900,
                "targets": [{"id": x["chat_id"], "title": x["title"], "type": x["peer_type"],
                             "category": x["category"], "confidence": x["confidence"]} for x in eligible[:500]],
                "truncated": len(eligible) > 500, "protected": protected[:500]}

    async def _leave_dialog(self, client: Any, entity: Any) -> None:
        """Preserve clear3.py semantics: channels use LeaveChannelRequest."""
        try:
            from telethon.tl.types import Channel
            from telethon.tl.functions.channels import LeaveChannelRequest
            if isinstance(entity, Channel):
                await client(LeaveChannelRequest(entity))
                return
        except ImportError:
            # Unit tests can use a duck-typed client without Telethon installed.
            pass
        await client.delete_dialog(entity)

    async def execute(self, client: Any, *, preview_id: str, confirmation: str,
                      delay_seconds: float | None = None, max_actions: int | None = None) -> dict[str, Any]:
        # One Telegram operation per profile at a time: no competing catalog scan
        # or second cleanup task can interleave with this destructive sequence.
        async with self._lock:
            row = self.db.execute("SELECT * FROM cleanup_jobs WHERE id=?", (preview_id,)).fetchone()
            if not row or row["status"] != "preview":
                raise ValueError("Предпросмотр не найден, уже использован или истёк")
            if time.time() - row["created_at"] > 900:
                self.db.execute("UPDATE cleanup_jobs SET status='expired',updated_at=? WHERE id=?", (time.time(), preview_id)); self.db.commit()
                raise ValueError("Предпросмотр истёк. Создай новый.")
            targets = json.loads(row["target_ids_json"])
            expected = f"LEAVE {len(targets)}"
            if not secrets.compare_digest(str(confirmation).strip(), expected):
                raise ValueError(f"Для подтверждения введи точно: {expected}")
            self.db.execute("UPDATE cleanup_jobs SET status='running',updated_at=? WHERE id=?", (time.time(), preview_id)); self.db.commit()

            done, failed, processed_ids = [], [], []
            delay = max(self.min_delay, float(delay_seconds if delay_seconds is not None else self.min_delay))
            limit = len(targets) if max_actions is None else max(1, min(int(max_actions), len(targets)))
            stop_reason = ""
            try:
                # Resolve from the already-connected client's current dialogs/session cache.
                current: dict[int, Any] = {}
                async for dialog in client.iter_dialogs():
                    if _dialog_kind(dialog) in {"group", "channel"}:
                        current[int(dialog.id)] = dialog.entity

                for chat_id in targets[:limit]:
                    entity = current.get(int(chat_id))
                    item = self.db.execute("SELECT title,protected_reason,is_creator,is_admin FROM catalog WHERE chat_id=?", (int(chat_id),)).fetchone()
                    title = item["title"] if item else str(chat_id)
                    if not entity:
                        failed.append({"id": chat_id, "title": title, "error": "Чат больше не найден в диалогах"})
                        processed_ids.append(int(chat_id))
                        self._event(preview_id, chat_id, title, "skipped", "not_found")
                        continue
                    # Re-check protection immediately before the destructive action.
                    live_protected = (_safe_bool(entity, "creator") or bool(getattr(entity, "admin_rights", None))
                                      or self._is_whitelisted(int(chat_id), title,
                                          _clean(getattr(entity, "username", ""), 128)))
                    if live_protected or (item and (item["protected_reason"] or item["is_creator"] or item["is_admin"])):
                        failed.append({"id": chat_id, "title": title, "error": "Защищённый чат"})
                        processed_ids.append(int(chat_id))
                        self._event(preview_id, chat_id, title, "skipped", "protected")
                        continue
                    try:
                        await self._leave_dialog(client, entity)
                        done.append(int(chat_id))
                        processed_ids.append(int(chat_id))
                        # Remove from the local catalog only after Telegram confirms
                        # the leave RPC; a failed/deferred leave remains visible.
                        self.db.execute("DELETE FROM catalog WHERE chat_id=?", (int(chat_id),))
                        self.db.commit()
                        self._event(preview_id, chat_id, title, "left", "")
                    except Exception as exc:
                        err_name = type(exc).__name__
                        err_text = _clean(exc, 300)
                        # Account-level limits defer the current target and every later target.
                        if err_name in {"PeerFloodError", "FloodWaitError", "FloodPremiumWaitError"} or "PEER_FLOOD" in err_text or "FLOOD_WAIT" in err_text:
                            stop_reason = f"Остановлено Telegram ({err_name}); не продолжаю действия автоматически"
                            self._event(preview_id, chat_id, title, "deferred", f"{err_name}: {err_text}")
                            break
                        failed.append({"id": chat_id, "title": title, "error": f"{err_name}: {err_text}"})
                        processed_ids.append(int(chat_id))
                        self._event(preview_id, chat_id, title, "failed", f"{err_name}: {err_text}")
                    if len(processed_ids) < limit and not stop_reason:
                        await asyncio.sleep(delay)
            except asyncio.CancelledError:
                remaining = targets[len(processed_ids):]
                self.db.execute("UPDATE cleanup_jobs SET status='paused',done_json=?,failed_json=?,updated_at=?,last_error=? WHERE id=?",
                                (json.dumps(done), json.dumps(failed), time.time(), "Задача прервана остановкой сервиса; можно продолжить вручную", preview_id))
                self.db.commit()
                raise
            except Exception as exc:
                remaining = targets[len(processed_ids):]
                self.db.execute("UPDATE cleanup_jobs SET status='paused',done_json=?,failed_json=?,updated_at=?,last_error=? WHERE id=?",
                                (json.dumps(done), json.dumps(failed), time.time(), f"{type(exc).__name__}: {_clean(exc, 250)}", preview_id))
                self.db.commit()
                raise

            remaining = targets[len(processed_ids):]
            status = "completed" if not remaining else "paused"
            last_error = stop_reason or ("Часть целей оставлена в очереди" if remaining else "")
            self.db.execute("UPDATE cleanup_jobs SET status=?,done_json=?,failed_json=?,updated_at=?,last_error=? WHERE id=?",
                            (status, json.dumps(done), json.dumps(failed), time.time(), last_error, preview_id))
            self.db.commit()
            return {"job_id": preview_id, "status": status, "completed": len(done), "failed": len(failed),
                    "remaining": len(remaining), "stop_reason": stop_reason, "done_ids": done,
                    "failed_items": failed, "remaining_ids": remaining}

    def resume(self, job_id: str) -> dict[str, Any]:
        """Create a fresh one-use preview for the remaining targets of a paused job."""
        old = self.job(job_id)
        if not old or old["status"] != "paused":
            raise ValueError("Нет приостановленной задачи для продолжения")
        # Failed permanent targets are not retried automatically; only unprocessed IDs remain.
        events = self.db.execute("SELECT chat_id,result FROM cleanup_events WHERE job_id=?", (job_id,)).fetchall()
        processed = {int(r[0]) for r in events if r[1] in {"left", "failed", "skipped"}}
        remaining = [int(x) for x in old["target_ids"] if int(x) not in processed]
        if not remaining:
            raise ValueError("В задаче не осталось необработанных чатов")
        rows = self.db.execute("SELECT * FROM catalog WHERE chat_id IN (%s)" % ",".join("?" * len(remaining)), remaining).fetchall()
        eligible = [self._row_dict(r) for r in rows if not r["protected_reason"] and not r["is_creator"] and not r["is_admin"] and not r["is_scam"]]
        if not eligible:
            raise ValueError("Оставшиеся чаты теперь защищены или отсутствуют в каталоге")
        new_id = secrets.token_urlsafe(18)
        ids = [x["chat_id"] for x in eligible]
        now = time.time()
        self.db.execute("INSERT INTO cleanup_jobs(id,status,category,target_ids_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (new_id, "preview", old["category"], json.dumps(ids), now, now))
        self.db.commit()
        return {"preview_id": new_id, "category": old["category"], "count": len(ids),
                "confirmation": f"LEAVE {len(ids)}", "targets": [{"id": x["chat_id"], "title": x["title"],
                "type": x["peer_type"], "category": x["category"], "confidence": x["confidence"]} for x in eligible[:500]],
                "truncated": len(ids) > 500, "resumed_from": job_id}

    def _event(self, job_id: str, chat_id: int, title: str, result: str, error: str) -> None:
        self.db.execute("INSERT INTO cleanup_events(job_id,chat_id,title,result,error,created_at) VALUES(?,?,?,?,?,?)",
                        (job_id, int(chat_id), title[:512], result, error[:500], time.time()))
        self.db.commit()

    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM cleanup_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["target_ids"] = json.loads(d.pop("target_ids_json"))
        d["done_ids"] = json.loads(d.pop("done_json"))
        d["failed_items"] = json.loads(d.pop("failed_json"))
        return d

    def close(self) -> None:
        try:
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        self.db.close()