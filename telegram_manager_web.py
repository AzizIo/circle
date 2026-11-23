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
APP_VERSION = "3.9.18"
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
        self.broadcast_post_previews: dict[str, dict] = {}
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
        self.api_latency_samples: list[tuple[float, float, int]] = []
        self._cpu_sample: tuple[int, int] | None = None

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
            self.broadcast_post_previews.pop(name, None)
            post_id = int((getattr(broadcaster, "state", {}) or {}).get("post_id") or 0)
            if post_id:
                self.broadcast_post_previews[name] = {
                    "id": post_id, "text": "", "media": None, "pinned": True,
                    "preview_available": False,
                }

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
        self.broadcast_post_previews.pop(name, None)
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
    async def metrics_middleware(self, request: web.Request, handler):
        track = request.path.startswith("/api/") and not request.path.endswith("/metrics")
        started = time.perf_counter()
        status = 200
        try:
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status = exc.status
            raise
        except Exception:
            status = 500
            raise
        finally:
            if track:
                now = time.time()
                self.api_latency_samples.append((now, (time.perf_counter() - started) * 1000.0, status))
                cutoff = now - 300.0
                self.api_latency_samples = [sample for sample in self.api_latency_samples if sample[0] >= cutoff][-1000:]

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
        app = web.Application(middlewares=[self.metrics_middleware, self.auth_middleware], client_max_size=128 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/system", self.system)
        app.router.add_get("/api/profiles/{name}/metrics", self.metrics)
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
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                                     "Pragma": "no-cache", "Expires": "0"})

    async def health(self, request):
        return web.json_response({"ok": True, "version": APP_VERSION})

    async def system(self, request):
        disk_path = self.base_dir
        usage = shutil.disk_usage(disk_path)
        return web.json_response({
            "ok": True, "version": APP_VERSION, "uptime_seconds": int(time.time() - self.started_at),
            "disk_total": usage.total, "disk_free": usage.free,
            "profiles_online": len(self.clients),
        })

    @staticmethod
    def _host_cpu_percent(previous: tuple[int, int] | None) -> tuple[float | None, tuple[int, int] | None]:
        try:
            parts = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
            values = [int(value) for value in parts[1:]]
            total = sum(values)
            idle = sum(values[3:5]) if len(values) >= 5 else values[3]
            current = (total, idle)
            if previous is None:
                return None, current
            delta_total = total - previous[0]
            delta_idle = idle - previous[1]
            if delta_total <= 0:
                return None, current
            return round(max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total)), 1), current
        except (OSError, IndexError, ValueError):
            return None, previous

    @staticmethod
    def _memory_snapshot() -> dict[str, int | None]:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, raw = line.split(":", 1)
                parts = raw.strip().split()
                if parts:
                    values[key] = int(parts[0]) * 1024
        except (OSError, ValueError):
            pass
        total = values.get("MemTotal")
        available = values.get("MemAvailable", values.get("MemFree"))
        rss = None
        try:
            for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            pass
        used_percent = round(100.0 * (total - available) / total, 1) if total and available is not None else None
        return {"total_bytes": total, "available_bytes": available,
                "used_percent": used_percent, "process_rss_bytes": rss}

    @staticmethod
    def _sqlite_sizes(data_dir: Path) -> dict[str, int]:
        def size_for(stem: str) -> int:
            total = 0
            for suffix in ("", "-wal", "-shm"):
                try:
                    total += (data_dir / f"{stem}{suffix}").stat().st_size
                except OSError:
                    continue
            return total
        return {"archive_bytes": size_for("circles.db"),
                "catalog_bytes": size_for("smart_cleaner.sqlite3")}

    async def metrics(self, request):
        name = request.match_info["name"]
        self._get_profile(name)
        now = time.time()
        cpu_percent, self._cpu_sample = self._host_cpu_percent(self._cpu_sample)
        memory = self._memory_snapshot()
        sqlite = self._sqlite_sizes(self.data_dirs[name])
        recent = [sample for sample in self.api_latency_samples if sample[0] >= now - 60.0]
        latencies = sorted(sample[1] for sample in recent)
        p95 = latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None
        api = {
            "requests_60s": len(recent),
            "errors_60s": sum(1 for sample in recent if sample[2] >= 500),
            "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "p95_ms": round(p95, 1) if p95 is not None else None,
            "last_ms": round(recent[-1][1], 1) if recent else None,
        }
        disk = shutil.disk_usage(self.data_dirs[name])
        return web.json_response({
            "sampled_at": now, "cpu_percent": cpu_percent, "memory": memory,
            "sqlite": {**sqlite, "total_bytes": sum(sqlite.values())},
            "api": api, "disk": {"total_bytes": disk.total, "free_bytes": disk.free},
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
        profile_cfg = self.configs[name]
        return {
            "profile": name,
            "account_label": str(profile_cfg.get("account_label") or name),
            "account_id": int(profile_cfg.get("owner_id") or 0),
            "running": bool(bc.running), "status": bc.short(),
            "active": bool(state.get("active")), "end": state.get("end", 0),
            "next": state.get("next", 0), "interval": state.get("interval", 0),
            "wave": state.get("wave", 0), "run_ok": state.get("run_ok", 0),
            "run_fail": state.get("run_fail", 0), "life_ok": state.get("life_ok", 0),
            "life_fail": state.get("life_fail", 0), "life_waves": state.get("life_waves", 0),
            "last_error": state.get("last_error", ""),
            "pending_total": total, "pending_completed": completed,
            "pending_remaining": max(0, total - completed), "progress": progress,
            "history": state.get("history", [])[-30:],
            "post": post or self.broadcast_post_previews.get(name),
            "allowlist_only": bool(self.configs[name].get("broadcast_allowlist_only")),
            "selected_recipients": len(self.configs[name].get("broadcast_allowlist") or []),
        }

    async def dashboard(self, request):
        name = request.match_info["name"]
        _, cleaner = self._get_profile(name)
        cfg = self.configs[name]
        stats = await asyncio.to_thread(cleaner.catalog_stats)
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

    def _archive_summary_sync(self, name: str) -> dict:
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
            return {
                "topics": topics, "total": int(total or 0), "sent": int(sent or 0),
                "pending": int((total or 0) - (sent or 0)), "last": last,
                "by_type": by_type, "backfill": backfill,
            }
        finally:
            conn.close()

    async def archive_summary(self, request):
        name = request.match_info["name"]
        try:
            result = await asyncio.to_thread(self._archive_summary_sync, name)
            return web.json_response(result)
        except (OSError, ValueError) as exc:
            raise web.HTTPServiceUnavailable(text=f"База архива недоступна: {type(exc).__name__}")

    def _archive_people_sync(self, name: str, query: str, limit: int, offset: int,
                             compact: bool = False) -> dict:
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
            optional_fields = "" if compact else (
                ", MIN(c.date_utc) AS first_media, COALESCE(SUM(c.duration),0) AS total_duration"
            )
            topic_field = "" if compact else "t.topic_id,"
            rows = conn.execute(
                f"""SELECT t.user_id,t.title,{topic_field}
                    COUNT(c.msg_id) AS media_count,
                    COALESCE(SUM(CASE WHEN c.sent=1 THEN 1 ELSE 0 END),0) AS sent_count,
                    COALESCE(SUM(CASE WHEN c.sent=0 THEN 1 ELSE 0 END),0) AS pending_count,
                    MAX(c.date_utc) AS last_media{optional_fields}
                    FROM topics t LEFT JOIN circles c ON {join}
                    {where}
                    GROUP BY t.user_id,t.title,t.topic_id
                    ORDER BY MAX(c.date_utc) DESC,t.title COLLATE NOCASE
                    LIMIT ? OFFSET ?""", [*params, limit, offset]
            ).fetchall()
            return {
                "total": total, "limit": limit, "offset": offset,
                "items": [dict(row) for row in rows],
            }
        finally:
            conn.close()

    async def archive_people(self, request):
        name = request.match_info["name"]
        try:
            limit = max(1, min(200, int(request.query.get("limit", "50"))))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректная пагинация")
        query = request.query.get("query", "").strip()[:120]
        try:
            result = await asyncio.to_thread(
                self._archive_people_sync, name, query, limit, offset,
                request.query.get("compact") == "1"
            )
            return web.json_response(result)
        except (OSError, ValueError) as exc:
            raise web.HTTPServiceUnavailable(text=f"Каталог архива недоступен: {type(exc).__name__}")

    def _archive_person_media_sync(self, name: str, user_id: int, limit: int, offset: int,
                                   compact: bool = False) -> dict:
        conn = self._archive_connection(name)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(circles)")}
            media_col = "c.media_type" if "media_type" in columns else "'round_video'"
            mime_col = "c.mime_type" if "mime_type" in columns else "''"
            size_col = "c.file_size" if "file_size" in columns else "0"
            if compact:
                select_fields = (
                    f"c.msg_id,c.date_utc,c.outgoing,c.sent,{media_col} AS media_type,"
                    f"{size_col} AS file_size,c.file_path"
                )
            else:
                select_fields = (
                    f"c.msg_id,c.date_utc,c.duration,c.outgoing,c.sent,c.attempts,"
                    f"{media_col} AS media_type,{mime_col} AS mime_type,{size_col} AS file_size,c.file_path"
                )
            rows = conn.execute(
                f"""SELECT {select_fields}
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
            return {"user_id": user_id, "total": total, "limit": limit,
                    "offset": offset, "items": items}
        finally:
            conn.close()

    async def archive_person_media(self, request):
        name = request.match_info["name"]
        try:
            user_id = int(request.match_info["user_id"])
            limit = max(1, min(100, int(request.query.get("limit", "30"))))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректный ID или пагинация")
        try:
            result = await asyncio.to_thread(
                self._archive_person_media_sync, name, user_id, limit, offset,
                request.query.get("compact") == "1"
            )
            return web.json_response(result)
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
        # The panel uses timestamp/level/message only. Keep the legacy response
        # shape for other clients, while avoiding duplicate raw text in compact mode.
        compact = request.query.get("compact") == "1"
        if compact:
            entries = [{key: entry[key] for key in ("timestamp", "level", "message")}
                       for entry in entries]
        payload = {"kind": kind, "path": filename, "entries": entries, "count": len(entries),
                   "total_count": len(all_entries), "counts": counts}
        if not compact:
            payload["lines"] = [x["raw"] for x in entries]
        return web.json_response(payload)

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
        # This endpoint is polled frequently by the UI. It must be a local state read,
        # never a Telegram get_messages() call through bc.preview().
        post = self.broadcast_post_previews.get(name)
        if post is None:
            post_id = int((getattr(bc, "state", {}) or {}).get("post_id") or 0)
            if post_id:
                post = {"id": post_id, "text": "", "media": None,
                        "pinned": True, "preview_available": False}
                self.broadcast_post_previews[name] = post
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
            cached = {"id": int(post["id"]), "text": str(post.get("text", "") or "")[:3000],
                      "media": bool(post.get("media")), "pinned": bool(post.get("pinned")),
                      "preview_available": True}
            self.broadcast_post_previews[name] = cached
            return web.json_response({"ok": True, "post": cached})
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def recipients(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        refresh = request.query.get("refresh") == "1"
        chats = (await bc.chat_list(refresh=True) if refresh
                 else await self._validated_broadcast_chats(bc))
        try:
            offset = max(0, int(request.query.get("offset", "0")))
            limit = max(1, min(3000, int(request.query.get("limit", "100"))))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректная пагинация")

        cfg = self.configs[name]
        live_ids = set()
        for raw_id, _title in chats:
            try:
                chat_id = int(raw_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if chat_id:
                live_ids.add(chat_id)

        # The persisted allowlist can outlive Telegram memberships. Never send
        # IDs absent from this profile's current dialog snapshot back to the UI.
        raw_allowlist = cfg.get("broadcast_allowlist", [])
        parsed_allowlist = set()
        malformed_count = 0
        if isinstance(raw_allowlist, list):
            for raw_id in raw_allowlist:
                try:
                    if isinstance(raw_id, bool):
                        malformed_count += 1
                        continue
                    chat_id = int(raw_id)
                    if chat_id:
                        parsed_allowlist.add(chat_id)
                    else:
                        malformed_count += 1
                except (TypeError, ValueError, OverflowError):
                    malformed_count += 1
        elif raw_allowlist not in (None, ""):
            malformed_count = 1

        selected_ids = sorted(parsed_allowlist.intersection(live_ids))
        selected_id_set = set(selected_ids)
        removed_stale_count = len(parsed_allowlist.difference(live_ids)) + malformed_count
        normalized_allowlist = selected_ids
        if raw_allowlist != normalized_allowlist:
            cfg["broadcast_allowlist"] = normalized_allowlist
            self._save_config(cfg)

        items = []
        for cid, title in chats[offset:offset + limit]:
            cid = int(cid)
            items.append({"id": cid, "title": title, "status": bc.chat_status(cid),
                          "selected": cid in selected_id_set})
        return web.json_response({
            "profile": name,
            "account_label": str(cfg.get("account_label") or name),
            "account_id": int(cfg.get("owner_id") or 0),
            "total": len(chats), "items": items, "offset": offset, "limit": limit,
            "allowlist_only": bool(cfg.get("broadcast_allowlist_only")),
            "selected_count": len(selected_ids), "selected_ids": selected_ids,
            "removed_stale_count": removed_stale_count,
            "available_count": sum(1 for cid in live_ids if bc.chat_status(cid) == "ok"),
            "excluded_count": sum(1 for cid in live_ids if bc.chat_status(cid) != "ok"),
        })

    async def set_recipients(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Ожидался объект со списком получателей")

            if payload.get("select_all") is True:
                live_chats = await bc.chat_list(refresh=True)
                live = {int(cid) for cid, _ in live_chats}
                ids = [int(cid) for cid, _ in live_chats if bc.chat_status(int(cid)) == "ok"]
                requested_count = len(ids)
                removed_stale_count = 0
                if len(ids) > 3000:
                    raise ValueError("В аккаунте больше 3000 доступных чатов. Используй фильтры и выбор страницами.")
            else:
                raw_ids = payload.get("chat_ids")
                if not isinstance(raw_ids, list) or len(raw_ids) > 3000:
                    raise ValueError("chat_ids должен быть списком максимум из 3000 чатов")
                requested_ids = []
                for raw in raw_ids:
                    if isinstance(raw, bool):
                        raise ValueError("Некорректный ID чата")
                    cid = int(raw)
                    if cid == 0:
                        raise ValueError("Некорректный ID чата")
                    requested_ids.append(cid)
                requested_ids = list(dict.fromkeys(requested_ids))
                requested_count = len(requested_ids)
                live_chats = await self._validated_broadcast_chats(bc)
                live = {int(cid) for cid, _ in live_chats}
                # Stale or foreign-profile IDs are dropped, not allowed to block
                # saving the valid part of the user's selection.
                ids = [cid for cid in requested_ids if cid in live]
                removed_stale_count = len(requested_ids) - len(ids)

            cfg = self.configs[name]
            cfg["broadcast_allowlist"] = ids
            cfg["broadcast_allowlist_only"] = True
            self._save_config(cfg)
            available_count = sum(1 for cid in live if bc.chat_status(cid) == "ok")
            return web.json_response({
                "ok": True, "profile": name,
                "account_label": str(cfg.get("account_label") or name),
                "account_id": int(cfg.get("owner_id") or 0),
                "selected_count": len(ids), "selected_ids": ids,
                "requested_count": requested_count,
                "removed_stale_count": removed_stale_count,
                "available_count": available_count,
                "excluded_count": len(live) - available_count,
                "allowlist_only": True,
            })
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

    async def _validated_broadcast_chats(self, bc, *, max_age_seconds: float = 45.0):
        """Reuse a just-refreshed profile-local dialog snapshot for launch validation."""
        scanned_at = getattr(bc, "_dialogs_scanned_at", 0.0)
        try:
            fresh = scanned_at and 0 <= time.time() - float(scanned_at) <= max_age_seconds
        except (TypeError, ValueError, OverflowError):
            fresh = False
        return await bc.chat_list(refresh=not bool(fresh))

    async def start_broadcast(self, request):
        name = request.match_info["name"]
        bc = self._get_broadcaster(name)
        cfg = self.configs[name]
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                raise ValueError("Нужно подтвердить запуск в панели управления")
            if not cfg.get("broadcast_allowlist_only"):
                raise ValueError("Сначала выбери и сохрани конкретных получателей")
            raw_requested = payload.get("recipient_ids")
            if not isinstance(raw_requested, list) or not raw_requested or len(raw_requested) > 3000:
                raise ValueError("Не передан сохранённый список получателей. Обнови список и повтори запуск")
            requested_ids = []
            for raw in raw_requested:
                if isinstance(raw, bool):
                    raise ValueError("Некорректный ID получателя")
                chat_id = int(raw)
                if not chat_id:
                    raise ValueError("Некорректный ID получателя")
                requested_ids.append(chat_id)
            requested_ids = list(dict.fromkeys(requested_ids))
            if not requested_ids:
                raise ValueError("Нет получателей для запуска")
            post = await bc.preview()
            if post:
                self.broadcast_post_previews[name] = {
                    "id": int(post["id"]), "text": str(post.get("text", "") or "")[:3000],
                    "media": bool(post.get("media")), "pinned": bool(post.get("pinned")),
                    "preview_available": True,
                }
            if not post:
                raise ValueError("В Избранном нет доступного сообщения для кампании")
            # Refreshing dialogs may prune stale IDs from the saved allowlist.
            # Intersect with the exact list the user confirmed so a concurrent or
            # previously saved extra recipient can never be added to this launch.
            live = {int(cid) for cid, _ in await self._validated_broadcast_chats(bc)}
            current_allowlist = set()
            for raw in cfg.get("broadcast_allowlist") or []:
                try:
                    current_allowlist.add(int(raw))
                except (TypeError, ValueError, OverflowError):
                    continue
            selected_ids = sorted(set(requested_ids).intersection(current_allowlist, live))
            if not selected_ids:
                raise ValueError("Все выбранные группы устарели или больше недоступны. Обнови список получателей")
            if selected_ids != sorted(current_allowlist):
                cfg["broadcast_allowlist"] = selected_ids
                cfg["broadcast_allowlist_only"] = True
                self._save_config(cfg)
            pending = bc.state.get("pending_wave") if isinstance(bc.state.get("pending_wave"), dict) else None
            if bc.state.get("active") and pending:
                pending_ids = {int(x) for x in pending.get("chat_ids", [])}
                if not pending_ids.issubset(set(selected_ids)):
                    raise ValueError("Есть незавершённая очередь с другим списком получателей. Сначала заверши или останови прежнюю кампанию")
            ok = bc.start(cfg.get("broadcast_interval_min", 60), cfg.get("broadcast_hours", 12))
            if not ok:
                raise ValueError("Не удалось запустить кампанию. Проверь её текущее состояние")
            return web.json_response({
                "ok": True, "message": "Кампания запущена",
                "selected_count": len(selected_ids), "selected_ids": selected_ids,
            }, status=202)
        except (ValueError, TypeError, OSError) as exc:
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
        started_at = time.time()
        self.scan_results[scan_id] = {
            "status": "running", "dialogs_seen": 0, "scanned": 0,
            "started_at": started_at, "updated_at": started_at,
        }

        async def work():
            try:
                def report_progress(dialogs_seen: int, scanned: int) -> None:
                    self.scan_results[scan_id] = {
                        "status": "running", "dialogs_seen": dialogs_seen,
                        "scanned": scanned, "started_at": started_at,
                        "updated_at": time.time(),
                        "elapsed_seconds": round(time.time() - started_at, 1),
                    }

                result = await cleaner.scan(client, progress=report_progress)
                self.scan_results[scan_id] = {
                    "status": "completed", "result": result,
                    "dialogs_seen": result.get("dialogs_seen", 0),
                    "scanned": result.get("scanned", 0),
                    "elapsed_seconds": round(time.time() - started_at, 1),
                }
            except asyncio.CancelledError:
                self.scan_results[scan_id] = {
                    "status": "cancelled", "dialogs_seen": self.scan_results[scan_id].get("dialogs_seen", 0),
                    "scanned": self.scan_results[scan_id].get("scanned", 0),
                }
                raise
            except Exception as exc:
                log.exception("Smart Cleaner scan failed for profile %s", name)
                self.scan_results[scan_id] = {
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                    "dialogs_seen": self.scan_results[scan_id].get("dialogs_seen", 0),
                    "scanned": self.scan_results[scan_id].get("scanned", 0),
                }

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
            result = await asyncio.to_thread(
                cleaner.list_catalog,
                category=request.query.get("category") or None, query=request.query.get("query", ""),
                peer_type=peer_type, include_protected=True,
                limit=int(request.query.get("limit", 100)), offset=int(request.query.get("offset", 0)),
                compact=request.query.get("compact") == "1")
            return web.json_response(result)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def summary(self, request):
        _, cleaner = self._get_profile(request.match_info["name"])
        stats = await asyncio.to_thread(cleaner.catalog_stats)
        return web.json_response(stats)

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
