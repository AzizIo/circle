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
        return web.json_response({"ok": True, "version": "3.8.0"})

    async def system(self, request):
        disk_path = self.base_dir
        usage = shutil.disk_usage(disk_path)
        return web.json_response({
            "ok": True, "version": "3.8.0", "uptime_seconds": int(time.time() - self.started_at),
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
                if "broadcast_interval_min" in changed and bc.running:
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
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:] if path.exists() else []
        except OSError as exc:
            raise web.HTTPServiceUnavailable(text=f"Не удалось прочитать журнал: {type(exc).__name__}")
        return web.json_response({"kind": kind, "path": filename, "lines": lines, "count": len(lines)})

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
            limit = max(1, min(300, int(request.query.get("limit", "100"))))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректная пагинация")
        cfg = self.configs[name]
        allowlist = {int(x) for x in cfg.get("broadcast_allowlist", []) if str(x).lstrip("-").isdigit()}
        items = []
        for cid, title in chats[offset:offset + limit]:
            cid = int(cid)
            items.append({"id": cid, "title": title, "status": bc.chat_status(cid),
                          "selected": cid in allowlist})
        return web.json_response({"total": len(chats), "items": items, "offset": offset, "limit": limit,
                                  "allowlist_only": bool(cfg.get("broadcast_allowlist_only")),
                                  "selected_count": len(allowlist)})

    async def set_recipients(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        try:
            payload = await request.json()
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
            return web.json_response({"ok": True, "selected_count": len(ids), "allowlist_only": True})
        except (ValueError, TypeError, OSError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

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
            if not set(int(x) for x in selected).issubset(live):
                raise ValueError("Список получателей изменился. Обнови чаты и сохрани выбор заново")
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
