"""Authenticated local control plane for CIRCLES CONTROL.

The manager deliberately reuses the Telegram clients and long-lived services owned
by combined_multi_account.run_service. It never opens a second Telethon session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from smart_cleaner import SmartCleaner

log = logging.getLogger("circles.web_manager")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
PANEL_PATH = Path(__file__).with_name("telegram_manager_panel.html")
INDEX_HTML = PANEL_PATH.read_text(encoding="utf-8")

# Only non-secret, supported settings are exposed to the browser.
BOOL_SETTINGS = {
    "paused", "save_outgoing", "info_message", "keep_local",
    "broadcast_notify_waves", "broadcast_autostart", "broadcast_auto_subscribe",
    "broadcast_spambot",
}
NUMBER_SETTINGS = {
    "broadcast_interval_min": (1, 1440, int),
    "broadcast_hours": (0.1, 720, float),
    "broadcast_delay_min": (0, 60, float),
    "broadcast_delay_max": (0, 120, float),
    "broadcast_long_pause_chance": (0, 1, float),
    "low_disk_percent": (1, 50, int),
    "broadcast_auto_subscribe_gap_seconds": (5, 86400, int),
    "broadcast_auto_subscribe_daily_limit": (0, 100, int),
    "broadcast_auto_subscribe_max_per_message": (1, 100, int),
    "broadcast_auto_subscribe_batch_size": (1, 50, int),
    "broadcast_auto_subscribe_batch_pause_minutes": (5, 1440, int),
}
MEDIA_TYPES = {"round_video", "photo", "video"}


class TelegramManagerRuntime:
    def __init__(self, *, base_dir: str | Path,
                 profiles_provider: Callable[[], list[tuple[str, dict, Path]]] | None = None):
        self.base_dir = Path(base_dir)
        self.profiles_provider = profiles_provider
        self.clients: dict[str, Any] = {}
        self.configs: dict[str, dict] = {}
        self.data_dirs: dict[str, Path] = {}
        self.cleaners: dict[str, SmartCleaner] = {}
        self.broadcasters: dict[str, Any] = {}
        self.scan_tasks: dict[str, asyncio.Task] = {}
        self.scan_results: dict[str, dict] = {}
        self.media_cleanup_previews: dict[str, dict] = {}
        self.job_tasks: dict[str, asyncio.Task] = {}
        self.profile_tasks: dict[str, set[asyncio.Task]] = {}
        self.account_manager: Any = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.token = ""
        self.host = "127.0.0.1"
        self.port = 8765
        self.started_at = time.time()

    def set_account_manager(self, manager: Any) -> None:
        self.account_manager = manager

    def register_profile(self, name: str, client: Any, cfg: dict, data_dir: Path) -> None:
        if not PROFILE_RE.fullmatch(name):
            raise ValueError("Invalid profile name")
        self.clients[name] = client
        self.configs[name] = cfg
        self.data_dirs[name] = Path(data_dir)
        cleaner = self.cleaners.get(name)
        if cleaner is None:
            whitelist_path = self.base_dir / "whitelist.txt"
            whitelist = []
            if whitelist_path.is_file():
                try:
                    whitelist = [x.strip() for x in whitelist_path.read_text(encoding="utf-8").splitlines() if x.strip()]
                except OSError:
                    log.warning("Cannot read cleaner whitelist at %s", whitelist_path)
            self.cleaners[name] = SmartCleaner(data_dir, whitelist=whitelist)

    def register_broadcaster(self, name: str, broadcaster: Any) -> None:
        if PROFILE_RE.fullmatch(name):
            self.broadcasters[name] = broadcaster

    def _track_profile_task(self, name: str, task: asyncio.Task) -> None:
        self.profile_tasks.setdefault(name, set()).add(task)
        def finished(done_task):
            tasks = self.profile_tasks.get(name)
            if tasks is not None:
                tasks.discard(done_task)
                if not tasks:
                    self.profile_tasks.pop(name, None)
        task.add_done_callback(finished)

    def unregister_profile(self, name: str) -> None:
        self.clients.pop(name, None)
        self.configs.pop(name, None)
        self.data_dirs.pop(name, None)
        self.broadcasters.pop(name, None)
        pending = list(self.profile_tasks.get(name, set()))
        for task in pending:
            if not task.done():
                task.cancel()
        cleaner = self.cleaners.get(name)
        if cleaner and not self.runner and not pending:
            self.cleaners.pop(name, None)
            cleaner.close()
        elif pending:
            async def close_when_idle():
                await asyncio.gather(*pending, return_exceptions=True)
                if name not in self.clients and not self.profile_tasks.get(name):
                    current = self.cleaners.pop(name, None)
                    if current:
                        current.close()
            try:
                asyncio.get_running_loop().create_task(close_when_idle())
            except RuntimeError:
                pass

    def _get_profile(self, name: str) -> tuple[Any, SmartCleaner]:
        if not PROFILE_RE.fullmatch(name) or name not in self.clients:
            raise web.HTTPServiceUnavailable(text="Профиль не запущен в CIRCLES CONTROL")
        return self.clients[name], self.cleaners[name]

    def _get_broadcaster(self, name: str):
        self._get_profile(name)
        bc = self.broadcasters.get(name)
        if bc is None:
            raise web.HTTPServiceUnavailable(text="Движок рассылки этого профиля ещё не готов")
        return bc

    @staticmethod
    def _save_config(cfg: dict) -> None:
        path_value = cfg.get("_config_path")
        if not path_value:
            raise RuntimeError("Не задан путь конфигурации профиля")
        path = Path(path_value)
        path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
        tmp = path.with_suffix(path.suffix + ".web.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(serializable, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    @staticmethod
    def _safe_config(cfg: dict) -> dict:
        keys = BOOL_SETTINGS | set(NUMBER_SETTINGS) | {
            "target_chat_id", "archive_media_types", "account_label",
            "session_name", "data_dir", "broadcast_allowlist_only",
            "broadcast_allowlist", "broadcast_exclude",
        }
        return {key: cfg.get(key) for key in keys if key in cfg}

    def _token(self) -> str:
        env_token = os.getenv("CIRCLES_WEB_TOKEN", "").strip()
        if env_token:
            return env_token
        path = self.base_dir / ".telegram_manager_token"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(36)
        path.write_text(value + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.path in {"/", "/health"}:
            return await handler(request)
        supplied = request.headers.get("Authorization", "")
        expected = "Bearer " + self.token
        if not secrets.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized(text="Требуется Bearer token")
        return await handler(request)

    async def start(self) -> str:
        if self.runner:
            return self.token
        self.host = os.getenv("CIRCLES_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        if self.host not in {"127.0.0.1", "localhost", "::1"} and os.getenv("CIRCLES_WEB_ALLOW_REMOTE") != "1":
            raise RuntimeError("По умолчанию Web Manager доступен только на localhost. Используй SSH-туннель.")
        self.port = int(os.getenv("CIRCLES_WEB_PORT", "8765"))
        self.token = self._token()
        app = web.Application(middlewares=[self.auth_middleware], client_max_size=128 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/system", self.system)
        app.router.add_get("/api/profiles", self.profiles)
        app.router.add_post("/api/accounts", self.create_account)
        app.router.add_post("/api/profiles/{name}/login/start", self.login_start)
        app.router.add_post("/api/profiles/{name}/login/code", self.login_code)
        app.router.add_post("/api/profiles/{name}/login/password", self.login_password)
        app.router.add_post("/api/profiles/{name}/start", self.start_profile)
        app.router.add_post("/api/profiles/{name}/stop", self.stop_profile)
        app.router.add_get("/api/profiles/{name}/dashboard", self.dashboard)
        app.router.add_get("/api/profiles/{name}/settings", self.settings)
        app.router.add_patch("/api/profiles/{name}/settings", self.settings)
        app.router.add_get("/api/profiles/{name}/logs", self.logs)
        app.router.add_get("/api/profiles/{name}/archive/summary", self.archive_summary)
        app.router.add_get("/api/profiles/{name}/archive/people", self.archive_people)
        app.router.add_get("/api/profiles/{name}/archive/people/{user_id}/media", self.archive_person_media)
        app.router.add_get("/api/profiles/{name}/media/cleanup/scan", self.media_cleanup_scan)
        app.router.add_post("/api/profiles/{name}/media/cleanup/execute", self.media_cleanup_execute)
        app.router.add_get("/api/profiles/{name}/broadcast", self.broadcast_status)
        app.router.add_get("/api/profiles/{name}/saved-posts", self.saved_posts)
        app.router.add_post("/api/profiles/{name}/broadcast/post", self.select_broadcast_post)
        app.router.add_post("/api/profiles/{name}/broadcast/recipients", self.set_recipients)
        app.router.add_get("/api/profiles/{name}/broadcast/recipients", self.recipients)
        app.router.add_post("/api/profiles/{name}/broadcast/start", self.start_broadcast)
        app.router.add_post("/api/profiles/{name}/broadcast/stop", self.stop_broadcast)
        app.router.add_post("/api/profiles/{name}/scan", self.start_scan)
        app.router.add_get("/api/scans/{scan_id}", self.scan_status)
        app.router.add_get("/api/profiles/{name}/dialogs", self.dialogs)
        app.router.add_get("/api/profiles/{name}/summary", self.summary)
        app.router.add_post("/api/profiles/{name}/leave", self.leave_one)
        app.router.add_post("/api/profiles/{name}/preview", self.preview)
        app.router.add_post("/api/profiles/{name}/execute", self.execute)
        app.router.add_post("/api/profiles/{name}/resume", self.resume)
        app.router.add_get("/api/profiles/{name}/jobs/{job_id}", self.job_status)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        log.info("CIRCLES Web Control доступен: http://%s:%s/", self.host, self.port)
        return self.token

    async def stop(self) -> None:
        pending = list(self.scan_tasks.values()) + list(self.job_tasks.values())
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        self.runner = None
        self.site = None
        for name in list(self.cleaners):
            cleaner = self.cleaners.pop(name)
            cleaner.close()
        self.clients.clear()
        self.configs.clear()
        self.data_dirs.clear()
        self.broadcasters.clear()
        self.profile_tasks.clear()
        self.account_manager = None

    async def index(self, request):
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")

    async def health(self, request):
        return web.json_response({"ok": True, "version": "3.9.0"})

    async def system(self, request):
        disk_path = self.base_dir
        usage = shutil.disk_usage(disk_path)
        return web.json_response({
            "ok": True, "version": "3.9.0", "uptime_seconds": int(time.time() - self.started_at),
            "disk_total": usage.total, "disk_free": usage.free,
            "profiles_online": len(self.clients),
        })

    async def profiles(self, request):
        items = []
        if self.profiles_provider:
            try:
                profiles = self.profiles_provider()
            except Exception:
                profiles = []
            for name, cfg, ddir in profiles:
                connected = name in self.clients
                running = False
                status = "Подключён" if connected else "Сессия сохранена · не запущен"
                if self.account_manager is not None:
                    try:
                        status = self.account_manager.status(name)
                        running = self.account_manager.is_running(name)
                    except Exception:
                        pass
                items.append({
                    "name": name, "label": cfg.get("account_label") or name,
                    "status": status, "active": connected, "running": running,
                    "can_start": bool(self.account_manager and name != "default" and not running and not connected
                                      and self.account_manager.has_session(name)),
                    "can_stop": bool(self.account_manager and name != "default" and running),
                })
        else:
            items = [{"name": n, "label": self.configs[n].get("account_label") or n,
                      "status": "Подключён", "active": True, "running": True,
                      "can_start": False, "can_stop": False} for n in self.clients]
        return web.json_response({"profiles": items})

    async def create_account(self, request):
        if self.account_manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен")
        try:
            payload = await request.json()
            name = str(payload.get("name", "")).strip()
            if not PROFILE_RE.fullmatch(name) or name == "default":
                raise ValueError("Имя профиля: 1–40 символов латиницей, цифрами, точкой, _ или -")
            path = self.account_manager.add_profile(name)
            return web.json_response({"ok": True, "name": name, "config": path.name}, status=201)
        except (ValueError, FileExistsError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def login_start(self, request):
        manager = self.account_manager
        if manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен")
        try:
            payload = await request.json()
            stage, message = await manager.begin_login(request.match_info["name"], str(payload.get("phone", "")))
            return web.json_response({"stage": stage, "message": message}, status=200 if stage in {"code", "done"} else 400)
        except (ValueError, OSError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def login_code(self, request):
        manager = self.account_manager
        if manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен")
        payload = await request.json()
        stage, message = await manager.submit_code(request.match_info["name"], str(payload.get("code", "")))
        return web.json_response({"stage": stage, "message": message}, status=200 if stage in {"done", "password"} else 400)

    async def login_password(self, request):
        manager = self.account_manager
        if manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен")
        payload = await request.json()
        stage, message = await manager.submit_password(request.match_info["name"], str(payload.get("password", "")))
        return web.json_response({"stage": stage, "message": message}, status=200 if stage == "done" else 400)

    async def start_profile(self, request):
        name = request.match_info["name"]
        if not PROFILE_RE.fullmatch(name) or self.account_manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен")
        ok, message = await self.account_manager.start_profile(name)
        if not ok:
            raise web.HTTPConflict(text=message)
        return web.json_response({"ok": True, "message": message})

    async def stop_profile(self, request):
        name = request.match_info["name"]
        if not PROFILE_RE.fullmatch(name) or self.account_manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен")
        ok, message = await self.account_manager.stop_profile(name)
        if not ok:
            raise web.HTTPConflict(text=message)
        return web.json_response({"ok": True, "message": message})

    def _broadcast_view(self, name: str, bc: Any, post: dict | None = None) -> dict:
        state = dict(bc.state)
        pending = state.get("pending_wave") if isinstance(state.get("pending_wave"), dict) else {}
        total = len(pending.get("chat_ids", []))
        completed = len(pending.get("completed_ids", []))
        progress = list(getattr(bc, "progress", (0, 0)) or (0, 0))
        return {
            "running": bool(bc.running), "status": bc.short(),
            "active": bool(state.get("active")), "end": state.get("end", 0),
            "next": state.get("next", 0), "interval": state.get("interval", 0),
            "wave": state.get("wave", 0), "run_ok": state.get("run_ok", 0),
            "run_fail": state.get("run_fail", 0), "life_ok": state.get("life_ok", 0),
            "life_fail": state.get("life_fail", 0), "life_waves": state.get("life_waves", 0),
            "last_error": state.get("last_error", ""),
            "pending_total": total, "pending_completed": completed,
            "pending_remaining": max(0, total - completed), "progress": progress,
            "history": state.get("history", [])[-30:], "post": post,
            "allowlist_only": bool(self.configs[name].get("broadcast_allowlist_only")),
            "selected_recipients": len(self.configs[name].get("broadcast_allowlist") or []),
        }

    async def dashboard(self, request):
        name = request.match_info["name"]
        _, cleaner = self._get_profile(name)
        cfg = self.configs[name]
        stats = cleaner.catalog_stats()
        bc = self.broadcasters.get(name)
        broadcast = self._broadcast_view(name, bc) if bc else None
        usage = shutil.disk_usage(self.data_dirs[name])
        return web.json_response({
            "profile": {"name": name, "label": cfg.get("account_label") or name},
            "catalog": stats, "broadcast": broadcast,
            "archive": {
                "paused": bool(cfg.get("paused", False)),
                "media_types": cfg.get("archive_media_types", []),
                "target_chat_id": cfg.get("target_chat_id", 0),
            },
            "disk": {"total": usage.total, "free": usage.free},
        })

    async def settings(self, request):
        name = request.match_info["name"]
        self._get_profile(name)
        cfg = self.configs[name]
        if request.method == "GET":
            return web.json_response(self._safe_config(cfg))
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not payload:
                raise ValueError("Передай объект с изменяемыми настройками")
            changed = set(payload)
            allowed = BOOL_SETTINGS | set(NUMBER_SETTINGS) | {"archive_media_types", "target_chat_id"}
            unknown = changed - allowed
            if unknown:
                raise ValueError("Недоступные настройки: " + ", ".join(sorted(unknown)))
            for key in BOOL_SETTINGS.intersection(changed):
                if not isinstance(payload[key], bool):
                    raise ValueError(f"{key}: ожидается true или false")
                cfg[key] = payload[key]
            for key in NUMBER_SETTINGS.keys() & changed:
                lo, hi, conv = NUMBER_SETTINGS[key]
                raw = payload[key]
                if isinstance(raw, bool):
                    raise ValueError(f"{key}: ожидается число")
                value = conv(raw)
                if not math.isfinite(float(value)) or not lo <= value <= hi:
                    raise ValueError(f"{key}: значение должно быть от {lo} до {hi}")
                cfg[key] = value
            if "archive_media_types" in changed:
                value = payload["archive_media_types"]
                if not isinstance(value, list) or not value or any(x not in MEDIA_TYPES for x in value):
                    raise ValueError("Выбери хотя бы один тип медиа: round_video, photo, video")
                cfg["archive_media_types"] = list(dict.fromkeys(value))
            if "target_chat_id" in changed:
                raw = payload["target_chat_id"]
                if isinstance(raw, bool):
                    raise ValueError("target_chat_id должен быть числом")
                target = int(raw)
                if target != 0 and not re.fullmatch(r"-100\d+", str(target)):
                    raise ValueError("ID группы архива должен быть 0 или иметь формат -100...")
                cfg["target_chat_id"] = target
            if float(cfg.get("broadcast_delay_min", 0.6)) > float(cfg.get("broadcast_delay_max", 1.2)):
                raise ValueError("Минимальная пауза не может быть больше максимальной")
            self._save_config(cfg)
            bc = self.broadcasters.get(name)
            if bc:
                bc._apply_delays()
                if "broadcast_interval_min" in changed:
                    bc.set_interval(cfg["broadcast_interval_min"])
                if "broadcast_hours" in changed and bc.running:
                    bc.set_duration(cfg["broadcast_hours"])
                if "broadcast_auto_subscribe" in changed and cfg["broadcast_auto_subscribe"]:
                    bc._ensure_autosub_worker()
                if "broadcast_auto_subscribe" in changed and not cfg["broadcast_auto_subscribe"]:
                    bc._autosub_wakeup.set()
            return web.json_response({"ok": True, "settings": self._safe_config(cfg)})
        except (ValueError, TypeError, OSError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    def _archive_connection(self, name: str):
        import sqlite3
        self._get_profile(name)
        path = self.data_dirs[name] / "circles.db"
        if not path.is_file():
            raise web.HTTPNotFound(text="База архива ещё не создана")
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    async def archive_summary(self, request):
        name = request.match_info["name"]
        try:
            conn = self._archive_connection(name)
            try:
                topics = int(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
                total, sent, last = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(sent=1),0), MAX(date_utc) FROM circles"
                ).fetchone()
                columns = {row[1] for row in conn.execute("PRAGMA table_info(circles)")}
                media_col = "media_type" if "media_type" in columns else "'round_video'"
                by_type = {str(row[0]): int(row[1]) for row in conn.execute(
                    f"SELECT COALESCE({media_col},'round_video'), COUNT(*) FROM circles GROUP BY 1"
                ).fetchall()}
                backfill = {}
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backfill_state'").fetchone():
                    backfill = {
                        str(row[0]): {"scanned": int(row[1] or 0), "archived": int(row[2] or 0),
                                      "completed_chats": int(row[3] or 0), "chats": int(row[4] or 0)}
                        for row in conn.execute(
                            """SELECT CASE
                                WHEN media_type='photo' OR media_type LIKE 'photo:%' THEN 'photo'
                                WHEN media_type='video' OR media_type LIKE 'video:%' THEN 'video'
                                ELSE media_type END,
                                SUM(scanned), SUM(archived), SUM(completed=1), COUNT(*)
                                FROM backfill_state GROUP BY 1"""
                        ).fetchall()
                    }
                return web.json_response({
                    "topics": topics, "total": int(total or 0), "sent": int(sent or 0),
                    "pending": int((total or 0) - (sent or 0)), "last": last,
                    "by_type": by_type, "backfill": backfill,
                })
            finally:
                conn.close()
        except (OSError, ValueError) as exc:
            raise web.HTTPServiceUnavailable(text=f"База архива недоступна: {type(exc).__name__}")

    async def archive_people(self, request):
        name = request.match_info["name"]
        try:
            limit = max(1, min(200, int(request.query.get("limit", "50"))))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректная пагинация")
        query = request.query.get("query", "").strip()[:120]
        try:
            conn = self._archive_connection(name)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(circles)")}
                join = "c.topic_id=t.topic_id" if "topic_id" in columns else "c.user_id=t.user_id"
                where = ""
                params: list[Any] = []
                if query:
                    where = "WHERE (t.title LIKE ? OR CAST(t.user_id AS TEXT) LIKE ?)"
                    params = [f"%{query}%", f"%{query}%"]
                total = int(conn.execute(
                    f"SELECT COUNT(*) FROM topics t {where}", params
                ).fetchone()[0])
                rows = conn.execute(
                    f"""SELECT t.user_id,t.title,t.topic_id,
                        COUNT(c.msg_id) AS media_count,
                        COALESCE(SUM(CASE WHEN c.sent=1 THEN 1 ELSE 0 END),0) AS sent_count,
                        COALESCE(SUM(CASE WHEN c.sent=0 THEN 1 ELSE 0 END),0) AS pending_count,
                        MAX(c.date_utc) AS last_media,
                        MIN(c.date_utc) AS first_media,
                        COALESCE(SUM(c.duration),0) AS total_duration
                        FROM topics t LEFT JOIN circles c ON {join}
                        {where}
                        GROUP BY t.user_id,t.title,t.topic_id
                        ORDER BY MAX(c.date_utc) DESC,t.title COLLATE NOCASE
                        LIMIT ? OFFSET ?""", [*params, limit, offset]
                ).fetchall()
                return web.json_response({
                    "total": total, "limit": limit, "offset": offset,
                    "items": [dict(row) for row in rows],
                })
            finally:
                conn.close()
        except (OSError, ValueError) as exc:
            raise web.HTTPServiceUnavailable(text=f"Каталог архива недоступен: {type(exc).__name__}")

    async def archive_person_media(self, request):
        name = request.match_info["name"]
        try:
            user_id = int(request.match_info["user_id"])
            limit = max(1, min(100, int(request.query.get("limit", "30"))))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректный ID или пагинация")
        try:
            conn = self._archive_connection(name)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(circles)")}
                media_col = "c.media_type" if "media_type" in columns else "'round_video'"
                mime_col = "c.mime_type" if "mime_type" in columns else "''"
                size_col = "c.file_size" if "file_size" in columns else "0"
                rows = conn.execute(
                    f"""SELECT c.msg_id,c.date_utc,c.duration,c.outgoing,c.sent,c.attempts,
                        {media_col} AS media_type,{mime_col} AS mime_type,{size_col} AS file_size,
                        c.file_path
                        FROM circles c WHERE c.user_id=?
                        ORDER BY c.date_utc DESC,c.msg_id DESC LIMIT ? OFFSET ?""",
                    (user_id, limit, offset)
                ).fetchall()
                total = int(conn.execute(
                    "SELECT COUNT(*) FROM circles WHERE user_id=?", (user_id,)
                ).fetchone()[0])
                items = []
                for row in rows:
                    item = dict(row)
                    raw_path = str(item.pop("file_path") or "")
                    item["file_name"] = Path(raw_path).name if raw_path else ""
                    item["file_available"] = bool(raw_path and Path(raw_path).is_file())
                    item["outgoing"] = bool(item["outgoing"])
                    item["sent"] = bool(item["sent"])
                    items.append(item)
                return web.json_response({"user_id": user_id, "total": total,
                                          "limit": limit, "offset": offset, "items": items})
            finally:
                conn.close()
        except (OSError, ValueError) as exc:
            raise web.HTTPServiceUnavailable(text=f"Записи медиа недоступны: {type(exc).__name__}")

    @staticmethod
    def _tail_log_lines(path: Path, limit: int, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
        """Read only the tail of a log; never load an unbounded log file into RAM."""
        if not path.is_file():
            return []
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            remaining = fh.tell()
            chunks = bytearray()
            while remaining > 0 and chunks.count(b"\n") <= limit and len(chunks) < max_bytes:
                size = min(65536, remaining, max_bytes - len(chunks))
                remaining -= size
                fh.seek(remaining)
                chunks[:0] = fh.read(size)
        lines = bytes(chunks).decode("utf-8", errors="replace").splitlines()
        return lines[-limit:]

    @staticmethod
    def _log_entry(raw: str) -> dict:
        match = re.search(r"\b(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\b", raw, re.IGNORECASE)
        level = (match.group(1).upper() if match else "INFO")
        if level == "WARN":
            level = "WARNING"
        timestamp = ""
        stamp = re.match(r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", raw)
        if stamp:
            timestamp = stamp.group(1)
        message = raw[match.end():].strip(" :-|") if match else raw
        return {"raw": raw, "timestamp": timestamp, "level": level, "message": message}

    async def logs(self, request):
        name = request.match_info["name"]
        self._get_profile(name)
        kind = request.query.get("kind", "app")
        if kind not in {"app", "broadcast"}:
            raise web.HTTPBadRequest(text="Неизвестный тип журнала")
        try:
            limit = max(20, min(1000, int(request.query.get("lines", "200"))))
        except ValueError:
            raise web.HTTPBadRequest(text="lines должен быть числом")
        filename = "circles.log" if kind == "app" else "broadcast.log"
        path = self.data_dirs[name] / filename
        try:
            lines = await asyncio.to_thread(self._tail_log_lines, path, limit)
        except OSError as exc:
            raise web.HTTPServiceUnavailable(text=f"Не удалось прочитать журнал: {type(exc).__name__}")
        all_entries = [self._log_entry(line) for line in lines]
        counts = {key: sum(1 for entry in all_entries if entry["level"] == key)
                  for key in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")}
        query = request.query.get("q", "").strip().casefold()[:160]
        level = request.query.get("level", "ALL").upper()
        if level not in {"ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise web.HTTPBadRequest(text="Неизвестный уровень журнала")
        entries = [entry for entry in all_entries
                   if (level == "ALL" or entry["level"] == level)
                   and (not query or query in entry["raw"].casefold())]
        return web.json_response({"kind": kind, "path": filename, "lines": [x["raw"] for x in entries],
                                  "entries": entries, "count": len(entries),
                                  "total_count": len(all_entries), "counts": counts})

    def _media_inventory_sync(self, name: str) -> dict:
        """Inventory only files under videos/; keep every path referenced by circles.db."""
        import sqlite3
        import stat as stat_module

        data_dir = self.data_dirs[name]
        root = (data_dir / "videos").resolve()
        if not root.exists() or not root.is_dir():
            return {"root": str(root), "total_files": 0, "total_bytes": 0,
                    "referenced_files": 0, "candidate_count": 0, "candidate_bytes": 0,
                    "orphan_count": 0, "temporary_count": 0, "samples": [], "candidates": []}

        db_path = data_dir / "circles.db"
        referenced: set[str] = set()
        has_catalog = db_path.is_file()
        if has_catalog:
            conn = None
            try:
                conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(circles)")}
                if "file_path" in columns:
                    for (raw_path,) in conn.execute(
                        "SELECT file_path FROM circles WHERE file_path IS NOT NULL AND file_path<>''"
                    ):
                        try:
                            referenced.add(os.path.normcase(os.path.realpath(str(raw_path))))
                        except (OSError, ValueError):
                            continue
            except sqlite3.Error as exc:
                raise RuntimeError(f"Не удалось проверить ссылки в circles.db: {type(exc).__name__}") from exc
            finally:
                if conn is not None:
                    conn.close()

        total_files = total_bytes = referenced_files = 0
        candidates = []
        now = time.time()
        temporary_suffixes = {".part", ".tmp", ".temp", ".download", ".partial"}
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
            for filename in files:
                path = Path(current) / filename
                try:
                    if path.is_symlink():
                        continue
                    st = path.stat(follow_symlinks=False)
                    if not stat_module.S_ISREG(st.st_mode):
                        continue
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                total_files += 1
                total_bytes += int(st.st_size)
                normalized = os.path.normcase(os.path.realpath(str(resolved)))
                is_referenced = normalized in referenced
                if is_referenced:
                    referenced_files += 1
                    continue
                age = now - st.st_mtime
                temporary = path.suffix.lower() in temporary_suffixes
                orphan = has_catalog
                # Do not touch recent files; the archiver may still be writing them.
                if age < 86400 or not (temporary or orphan):
                    continue
                reason = "Временный / незавершённый файл" if temporary else "Нет ссылки в circles.db"
                candidates.append({"path": str(resolved), "name": str(path.relative_to(root)),
                                   "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns),
                                   "reason": reason, "kind": "temporary" if temporary else "orphan"})
        candidates.sort(key=lambda x: (x["kind"] != "temporary", -x["size"], x["name"].casefold()))
        return {"root": str(root), "total_files": total_files, "total_bytes": total_bytes,
                "referenced_files": referenced_files, "candidate_count": len(candidates),
                "candidate_bytes": sum(x["size"] for x in candidates),
                "orphan_count": sum(x["kind"] == "orphan" for x in candidates),
                "temporary_count": sum(x["kind"] == "temporary" for x in candidates),
                "samples": [{k: item[k] for k in ("name", "size", "reason", "kind")}
                            for item in candidates[:30]],
                "candidates": candidates}

    async def media_cleanup_scan(self, request):
        name = request.match_info["name"]
        self._get_profile(name)
        try:
            inventory = await asyncio.to_thread(self._media_inventory_sync, name)
        except (OSError, RuntimeError) as exc:
            raise web.HTTPServiceUnavailable(text=str(exc))
        preview_id = secrets.token_urlsafe(18)
        self.media_cleanup_previews[preview_id] = {
            "profile": name, "created_at": time.time(), "candidates": inventory.pop("candidates", [])
        }
        # Expired previews are discarded opportunistically.
        cutoff = time.time() - 900
        self.media_cleanup_previews = {
            key: value for key, value in self.media_cleanup_previews.items()
            if value.get("created_at", 0) >= cutoff
        }
        inventory["preview_id"] = preview_id
        inventory["expires_in_seconds"] = 900
        inventory["confirmation"] = f"DELETE_ORPHANS {inventory['candidate_count']}"
        return web.json_response(inventory)

    def _media_cleanup_delete_sync(self, name: str, snapshots: list[dict]) -> dict:
        """Re-scan and compare inode metadata before deleting previewed orphan/temp files."""
        current = self._media_inventory_sync(name)
        current_by_path = {item["path"]: item for item in current["candidates"]}
        root = (self.data_dirs[name] / "videos").resolve()
        deleted = failed = deleted_bytes = 0
        for snapshot in snapshots:
            candidate = current_by_path.get(snapshot.get("path"))
            if not candidate or candidate["size"] != snapshot.get("size") or candidate["mtime_ns"] != snapshot.get("mtime_ns"):
                continue
            path = Path(candidate["path"])
            try:
                if path.is_symlink():
                    continue
                path.resolve(strict=True).relative_to(root)
                st = path.stat(follow_symlinks=False)
                if int(st.st_size) != int(snapshot["size"]) or int(st.st_mtime_ns) != int(snapshot["mtime_ns"]):
                    continue
                path.unlink()
                deleted += 1
                deleted_bytes += int(st.st_size)
                parent = path.parent
                while parent != root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except (OSError, ValueError, KeyError):
                failed += 1
        return {"deleted": deleted, "failed": failed, "deleted_bytes": deleted_bytes,
                "remaining_candidates": max(0, len(snapshots) - deleted - failed)}

    async def media_cleanup_execute(self, request):
        name = request.match_info["name"]
        self._get_profile(name)
        try:
            payload = await request.json()
            preview_id = str(payload.get("preview_id", ""))
            preview = self.media_cleanup_previews.get(preview_id)
            if not preview or preview.get("profile") != name:
                raise ValueError("Предпросмотр очистки не найден или уже использован. Запусти сканирование заново.")
            if time.time() - float(preview.get("created_at", 0)) > 900:
                self.media_cleanup_previews.pop(preview_id, None)
                raise ValueError("Предпросмотр истёк. Запусти сканирование заново.")
            snapshots = preview.get("candidates", [])
            if not snapshots:
                raise ValueError("Нет безопасных файлов для очистки")
            expected = f"DELETE_ORPHANS {len(snapshots)}"
            confirmation = str(payload.get("confirmation", "")).strip()
            if not secrets.compare_digest(confirmation, expected):
                raise ValueError(f"Для подтверждения введи точно: {expected}")
            result = await asyncio.to_thread(self._media_cleanup_delete_sync, name, snapshots)
            self.media_cleanup_previews.pop(preview_id, None)
            log.info("Media cleanup profile=%s deleted=%s failed=%s bytes=%s",
                     name, result["deleted"], result["failed"], result["deleted_bytes"])
            return web.json_response({"ok": True, **result})
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except (OSError, RuntimeError) as exc:
            raise web.HTTPServiceUnavailable(text=str(exc))

    async def broadcast_status(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        post = None
        try:
            msg = await bc.preview()
            if msg:
                post = {"id": int(msg["id"]), "text": msg.get("text", "")[:3000],
                        "media": bool(msg.get("media")), "pinned": bool(msg.get("pinned"))}
        except Exception as exc:
            log.debug("Broadcast preview unavailable for %s: %s", name, type(exc).__name__)
        return web.json_response(self._broadcast_view(name, bc, post))

    async def saved_posts(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        posts = await bc.saved_posts(limit=20)
        return web.json_response({"posts": [
            {"id": int(m.id), "text": str(getattr(m, "message", "") or "")[:1200],
             "media": bool(getattr(m, "media", None)),
             "date": getattr(getattr(m, "date", None), "isoformat", lambda: None)()}
            for m in posts
        ]})

    async def select_broadcast_post(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        try:
            payload = await request.json()
            post_id = int(payload.get("post_id", 0))
            if post_id <= 0:
                raise ValueError("Некорректный ID поста")
            post = await bc.select_post(post_id)
            if not post:
                raise ValueError("Сообщение не найдено в Избранном этого аккаунта")
            return web.json_response({"ok": True, "post": post})
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def recipients(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        refresh = request.query.get("refresh") == "1"
        chats = await bc.chat_list(refresh=refresh)
        try:
            offset = max(0, int(request.query.get("offset", "0")))
            limit = max(1, min(3000, int(request.query.get("limit", "100"))))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректная пагинация")
        cfg = self.configs[name]
        allowlist = set()
        for raw_id in cfg.get("broadcast_allowlist", []):
            try:
                if isinstance(raw_id, bool):
                    continue
                value = int(raw_id)
                if value:
                    allowlist.add(value)
            except (TypeError, ValueError, OverflowError):
                continue
        items = []
        for cid, title in chats[offset:offset + limit]:
            cid = int(cid)
            items.append({"id": cid, "title": title, "status": bc.chat_status(cid),
                          "selected": cid in allowlist})
        return web.json_response({"total": len(chats), "items": items, "offset": offset, "limit": limit,
                                  "allowlist_only": bool(cfg.get("broadcast_allowlist_only")),
                                  "selected_count": len(allowlist), "selected_ids": sorted(allowlist)})

    async def set_recipients(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        try:
            payload = await request.json()
            if payload.get("select_all") is True:
                live_chats = await bc.chat_list(refresh=True)
                ids = [int(cid) for cid, _ in live_chats if bc.chat_status(int(cid)) == "ok"]
                if len(ids) > 3000:
                    raise ValueError("В аккаунте больше 3000 доступных чатов. Используй фильтры и выбор страницами.")
                live = {int(cid) for cid, _ in live_chats}
            else:
                raw_ids = payload.get("chat_ids")
                if not isinstance(raw_ids, list) or len(raw_ids) > 3000:
                    raise ValueError("chat_ids должен быть списком максимум из 3000 чатов")
                ids = []
                for raw in raw_ids:
                    if isinstance(raw, bool):
                        raise ValueError("Некорректный ID чата")
                    cid = int(raw)
                    if cid == 0:
                        raise ValueError("Некорректный ID чата")
                    ids.append(cid)
                ids = list(dict.fromkeys(ids))
                live = {int(cid) for cid, _ in await bc.chat_list(refresh=True)}
            unknown = [cid for cid in ids if cid not in live]
            if unknown:
                raise ValueError(f"Среди выбранных есть чаты, которых нет в текущем аккаунте: {len(unknown)}")
            cfg = self.configs[name]
            cfg["broadcast_allowlist"] = ids
            cfg["broadcast_allowlist_only"] = True
            self._save_config(cfg)
            return web.json_response({"ok": True, "selected_count": len(ids), "selected_ids": ids,
                                      "available_count": sum(1 for cid in live if bc.chat_status(cid) == "ok"),
                                      "excluded_count": len(live) - sum(1 for cid in live if bc.chat_status(cid) == "ok"),
                                      "allowlist_only": True})
        except (ValueError, TypeError, OSError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    def _prune_left_recipients(self, name: str, chat_ids) -> None:
        """Keep the explicit recipient allowlist synchronized after a successful leave."""
        ids = {int(x) for x in (chat_ids or [])}
        if not ids:
            return
        cfg = self.configs.get(name)
        if cfg is None:
            return
        current = cfg.get("broadcast_allowlist") or []
        kept = []
        for raw_id in current:
            try:
                cid = int(raw_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if cid not in ids:
                kept.append(cid)
        if len(kept) != len(current):
            cfg["broadcast_allowlist"] = kept
            self._save_config(cfg)
        bc = self.broadcasters.get(name)
        if bc is not None and hasattr(bc, "chat_cache"):
            bc.chat_cache = [(int(cid), title) for cid, title in bc.chat_cache if int(cid) not in ids]

    async def start_broadcast(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        cfg = self.configs[name]
        try:
            payload = await request.json()
            selected = cfg.get("broadcast_allowlist") or []
            count = len(selected) if isinstance(selected, list) else 0
            expected = f"START {count}"
            confirmation = str(payload.get("confirmation", "")).strip()
            if not count or not cfg.get("broadcast_allowlist_only"):
                raise ValueError("Сначала выбери конкретные чаты-получатели и сохрани список")
            if not secrets.compare_digest(confirmation, expected):
                raise ValueError(f"Для запуска подтверди действие текстом: {expected}")
            post = await bc.preview()
            if not post:
                raise ValueError("В Избранном нет доступного сообщения для рассылки")
            live = {int(cid) for cid, _ in await bc.chat_list(refresh=True)}
            selected_ids = {int(x) for x in selected}
            if not selected_ids.issubset(live):
                raise ValueError("Список получателей изменился. Обнови чаты и сохрани выбор заново")
            pending = bc.state.get("pending_wave") if isinstance(bc.state.get("pending_wave"), dict) else None
            if bc.state.get("active") and pending:
                pending_ids = {int(x) for x in pending.get("chat_ids", [])}
                if not pending_ids.issubset(selected_ids):
                    raise ValueError("Есть незавершённая очередь с другими получателями. Не запускаю её с новым списком — сначала заверши или останови прежнюю кампанию.")
            ok = bc.start(cfg.get("broadcast_interval_min", 60), cfg.get("broadcast_hours", 12))
            if not ok:
                raise ValueError("Не удалось запустить кампанию. Проверь её текущее состояние")
            return web.json_response({"ok": True, "message": "Кампания запущена", "selected_count": count}, status=202)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def stop_broadcast(self, request):
        bc = self._get_broadcaster(request.match_info["name"])
        if not bc.running:
            raise web.HTTPConflict(text="Рассылка уже остановлена")
        bc.stop()
        return web.json_response({"ok": True, "message": "Запрос на остановку принят"}, status=202)

    async def start_scan(self, request):
        name = request.match_info["name"]
        client, cleaner = self._get_profile(name)
        scan_id = secrets.token_urlsafe(12)
        async def work():
            try:
                self.scan_results[scan_id] = {"status": "running"}
                result = await cleaner.scan(client)
                self.scan_results[scan_id] = {"status": "completed", "result": result}
            except asyncio.CancelledError:
                self.scan_results[scan_id] = {"status": "cancelled"}
                raise
            except Exception as exc:
                log.exception("Smart Cleaner scan failed for profile %s", name)
                self.scan_results[scan_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        task = asyncio.create_task(work(), name=f"smart-cleaner-scan:{name}")
        self.scan_tasks[scan_id] = task
        self._track_profile_task(name, task)
        return web.json_response({"scan_id": scan_id, "status": "running"}, status=202)

    async def scan_status(self, request):
        scan_id = request.match_info["scan_id"]
        return web.json_response(self.scan_results.get(scan_id, {"status": "unknown"}))

    async def dialogs(self, request):
        _, cleaner = self._get_profile(request.match_info["name"])
        try:
            peer_type = request.query.get("peer_type") or None
            if peer_type not in {None, "group", "channel"}:
                raise ValueError("Неизвестный тип чата")
            result = cleaner.list_catalog(
                category=request.query.get("category") or None, query=request.query.get("query", ""),
                peer_type=peer_type, include_protected=True,
                limit=int(request.query.get("limit", 100)), offset=int(request.query.get("offset", 0)))
            return web.json_response(result)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def summary(self, request):
        _, cleaner = self._get_profile(request.match_info["name"])
        return web.json_response(cleaner.catalog_stats())

    async def leave_one(self, request):
        name = request.match_info["name"]
        client, cleaner = self._get_profile(name)
        try:
            payload = await request.json()
            raw_id = payload.get("chat_id")
            if isinstance(raw_id, bool):
                raise ValueError("Некорректный ID чата")
            chat_id = int(raw_id)
            if chat_id == 0:
                raise ValueError("Некорректный ID чата")
            preview = cleaner.preview(category="single", chat_ids=[chat_id], allow_scam=False)
            if preview.get("count") != 1 or len(preview.get("targets", [])) != 1:
                raise ValueError("Чат не найден или не разрешён для выхода")
            title = str(preview["targets"][0].get("title") or chat_id)
            preview_id = str(preview["preview_id"])
            task_id = secrets.token_urlsafe(12)
            async def work():
                self.scan_results[task_id] = {"status": "running", "chat_id": chat_id, "title": title}
                try:
                    result = await cleaner.execute(client, preview_id=preview_id,
                                                   confirmation="LEAVE 1", delay_seconds=2, max_actions=1)
                    self._prune_left_recipients(name, result.get("done_ids", []))
                    self.scan_results[task_id] = {"status": "completed", "result": result,
                                                  "chat_id": chat_id, "title": title}
                except asyncio.CancelledError:
                    self.scan_results[task_id] = {"status": "cancelled", "chat_id": chat_id, "title": title}
                    raise
                except Exception as exc:
                    saved = cleaner.job(preview_id)
                    if saved and saved["status"] == "paused":
                        processed = len(saved["done_ids"]) + len(saved["failed_items"])
                        remaining = max(0, len(saved["target_ids"]) - processed)
                        result = {"job_id": preview_id, "status": "paused",
                                  "completed": len(saved["done_ids"]), "failed": len(saved["failed_items"]),
                                  "remaining": remaining, "stop_reason": saved.get("last_error") or str(exc)}
                        self.scan_results[task_id] = {"status": "completed", "result": result}
                    else:
                        self.scan_results[task_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            task = asyncio.create_task(work(), name=f"smart-cleaner-leave-one:{name}:{chat_id}")
            self.job_tasks[task_id] = task
            self._track_profile_task(name, task)
            return web.json_response({"job_id": task_id, "status": "running"}, status=202)
        except (ValueError, TypeError, KeyError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def preview(self, request):
        _, cleaner = self._get_profile(request.match_info["name"])
        try:
            payload = await request.json()
            result = cleaner.preview(category=str(payload.get("category", "all")),
                                     chat_ids=payload.get("chat_ids"),
                                     allow_scam=bool(payload.get("allow_scam", False)))
            return web.json_response(result)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def execute(self, request):
        name = request.match_info["name"]
        client, cleaner = self._get_profile(name)
        try:
            payload = await request.json()
            preview_id = str(payload.get("preview_id", ""))
            confirmation = str(payload.get("confirmation", ""))
            pending = cleaner.job(preview_id)
            if not pending or pending["status"] != "preview":
                raise ValueError("Предпросмотр не найден, уже использован или истёк")
            expected = f"LEAVE {len(pending['target_ids'])}"
            if not secrets.compare_digest(confirmation.strip(), expected):
                raise ValueError(f"Для подтверждения введи точно: {expected}")
            task_id = secrets.token_urlsafe(12)
            async def work():
                try:
                    self.scan_results[task_id] = {"status": "running"}
                    result = await cleaner.execute(client, preview_id=preview_id, confirmation=confirmation)
                    self._prune_left_recipients(name, result.get("done_ids", []))
                    self.scan_results[task_id] = {"status": "completed", "result": result}
                except Exception as exc:
                    saved = cleaner.job(preview_id)
                    if saved and saved["status"] == "paused":
                        processed = len(saved["done_ids"]) + len(saved["failed_items"])
                        remaining = max(0, len(saved["target_ids"]) - processed)
                        self.scan_results[task_id] = {"status": "completed", "result": {
                            "job_id": preview_id, "status": "paused", "completed": len(saved["done_ids"]),
                            "failed": len(saved["failed_items"]), "remaining": remaining,
                            "stop_reason": saved.get("last_error") or f"{type(exc).__name__}: {exc}"}}
                    else:
                        self.scan_results[task_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            task = asyncio.create_task(work(), name=f"smart-cleaner-job:{name}")
            self.job_tasks[task_id] = task
            self._track_profile_task(name, task)
            return web.json_response({"job_id": task_id, "status": "running"}, status=202)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def job_status(self, request):
        name = request.match_info["name"]
        _, cleaner = self._get_profile(name)
        job_id = request.match_info["job_id"]
        task_result = self.scan_results.get(job_id)
        if task_result:
            return web.json_response(task_result)
        if job_id in self.job_tasks and not self.job_tasks[job_id].done():
            return web.json_response({"status": "running"})
        job = cleaner.job(job_id)
        if not job:
            raise web.HTTPNotFound(text="Задача не найдена")
        return web.json_response(job)

    async def resume(self, request):
        name = request.match_info["name"]
        _, cleaner = self._get_profile(name)
        try:
            payload = await request.json()
            preview = cleaner.resume(str(payload.get("job_id", "")))
            return web.json_response(preview)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
