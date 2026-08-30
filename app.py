#!/usr/bin/env python3
"""Local UI application for Bilibili danmaku queue management."""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import mimetypes
import os
import platform
import subprocess
import sys
import sqlite3
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from danmu_queue import (
    OP_AUTH,
    OP_AUTH_REPLY,
    OP_HEARTBEAT,
    OP_HEARTBEAT_REPLY,
    OP_MESSAGE,
    DanmuMessage,
    build_packet,
    build_auth_payload,
    danmu_fingerprint,
    decode_json_body,
    extract_danmu,
    extract_guard_buy,
    get_danmu_history,
    get_danmu_info,
    http_headers,
    keyword_matches,
    normalize_keyword_text,
    iter_packets,
    local_now,
    normalize_unix_timestamp,
    open_websocket,
    resolve_room_id,
    safe_int,
)


DEFAULT_SETTINGS = {
    "room": "",
    "keyword": "排队",
    "eligibility_mode": "historical",
    "required_guard_level": 3,
    "allow_repeat": False,
}

ELIGIBILITY_MODES = {"all", "historical", "current"}
GUARD_NAMES = {1: "总督", 2: "提督", 3: "舰长"}
APP_NAME = "DanmuQueue"
CONFIG_FILENAME = "config.local.json"
CONFIG_EXAMPLE_FILENAME = "config.local.example.json"
FORMER_CAPTAIN_MEDAL_LEVEL = 21


@dataclass(frozen=True)
class ListenerStatus:
    running: bool = False
    connected: bool = False
    room: str = ""
    real_room_id: int = 0
    started_at: str = ""
    last_error: str = ""


def guard_name(level: int) -> str:
    return GUARD_NAMES.get(level, "无")


def guard_allowed(level: int, required_guard_level: int) -> bool:
    return 0 < level <= required_guard_level


def normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    settings.update(raw)

    settings["room"] = str(settings.get("room") or "").strip()
    settings["keyword"] = normalize_keyword_text(settings.get("keyword")) or "排队"

    mode = str(settings.get("eligibility_mode") or "historical")
    settings["eligibility_mode"] = mode if mode in ELIGIBILITY_MODES else "historical"

    required = safe_int(settings.get("required_guard_level"), default=3) or 3
    settings["required_guard_level"] = max(1, min(3, required))
    settings["allow_repeat"] = bool(settings.get("allow_repeat"))
    return settings


def parse_danmu_time(unix_ts: int | None) -> str:
    normalized_ts = normalize_unix_timestamp(unix_ts)
    if not normalized_ts:
        return ""
    return datetime.fromtimestamp(normalized_ts).astimezone().isoformat(timespec="seconds")


def friendly_ws_error(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    if "no close frame received or sent" in message:
        return "连接被弹幕服务器直接断开"
    if "timed out" in message.lower() or "timeout" in message.lower():
        return "连接超时"
    return message


def resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent


def default_user_data_dir() -> Path:
    override = os.environ.get("DANMUQUEUE_HOME")
    if override:
        return Path(override).expanduser()

    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def default_db_path() -> Path:
    if getattr(sys, "frozen", False) or os.environ.get("DANMUQUEUE_HOME"):
        return default_user_data_dir() / "danmu_queue.db"
    return Path("danmu_queue.db")


def default_config_path() -> Path:
    override = os.environ.get("DANMUQUEUE_CONFIG")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False) or os.environ.get("DANMUQUEUE_HOME"):
        return default_user_data_dir() / CONFIG_FILENAME
    return resource_root() / CONFIG_FILENAME


def load_local_config(config_path: Path) -> dict[str, Any] | None:
    if not config_path.exists():
        return None
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是一个 JSON 对象。")
    return {
        "settings": normalize_settings(raw),
        "cookie": str(raw.get("cookie") or "").strip(),
    }


def save_local_config(config_path: Path, settings: dict[str, Any], cookie: str | None = None) -> None:
    payload = dict(settings)
    if cookie is None and config_path.exists():
        try:
            existing = load_local_config(config_path)
        except Exception:
            existing = None
        cookie = str((existing or {}).get("cookie") or "")
    payload["cookie"] = str(cookie or "").strip()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def local_ui_url(host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{display_host}:{port}/"


def open_management_page(url: str) -> None:
    system = platform.system()
    if system == "Darwin":
        try:
            completed = subprocess.run(
                ["open", "-a", "Google Chrome", url],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
            )
            if completed.returncode == 0:
                return
        except Exception:
            pass

    try:
        chrome = webbrowser.get("chrome")
    except webbrowser.Error:
        chrome = None
    if chrome is not None:
        chrome.open(url)
        return

    webbrowser.open(url)


def open_management_page_later(url: str, delay: float = 0.8) -> None:
    def worker() -> None:
        time.sleep(delay)
        open_management_page(url)

    threading.Thread(target=worker, name="open-browser", daemon=True).start()


class QueueStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_no INTEGER NOT NULL,
                    queued_at TEXT NOT NULL,
                    danmu_time TEXT NOT NULL,
                    uid INTEGER NOT NULL,
                    uname TEXT NOT NULL,
                    message TEXT NOT NULL,
                    guard_level INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    overlay_hidden_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            queue_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(queue)").fetchall()
            }
            if "note" not in queue_columns:
                conn.execute("ALTER TABLE queue ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            if "overlay_hidden_at" not in queue_columns:
                conn.execute("ALTER TABLE queue ADD COLUMN overlay_hidden_at TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guards (
                    uid INTEGER PRIMARY KEY,
                    uname TEXT NOT NULL,
                    best_guard_level INTEGER NOT NULL,
                    last_guard_level INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    source TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                )
                """
            )
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )

    def get_settings(self) -> dict[str, Any]:
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        raw: dict[str, Any] = {}
        for row in rows:
            try:
                raw[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                raw[row["key"]] = row["value"]
        return normalize_settings(raw)

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        settings = self.get_settings()
        for key in DEFAULT_SETTINGS:
            if key in updates:
                settings[key] = updates[key]
        settings = normalize_settings(settings)
        with self.lock, self._connect() as conn:
            for key, value in settings.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return settings

    def upsert_guard(self, uid: int, uname: str, guard_level: int, source: str) -> None:
        if uid <= 0 or guard_level <= 0:
            return
        now = local_now()
        uname = uname.strip() or f"UID {uid}"
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT best_guard_level, first_seen_at FROM guards WHERE uid = ?",
                (uid,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO guards(
                        uid, uname, best_guard_level, last_guard_level,
                        first_seen_at, last_seen_at, source
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (uid, uname, guard_level, guard_level, now, now, source),
                )
            else:
                best_guard_level = min(row["best_guard_level"], guard_level)
                conn.execute(
                    """
                    UPDATE guards
                    SET uname = ?, best_guard_level = ?, last_guard_level = ?,
                        last_seen_at = ?, source = ?
                    WHERE uid = ?
                    """,
                    (uname, best_guard_level, guard_level, now, source, uid),
                )

    def import_guards(self, text: str, default_level: int) -> int:
        count = 0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("uid"):
                continue
            parts = [part.strip() for part in line.replace("\t", ",").split(",") if part.strip()]
            if not parts:
                continue
            uid = safe_int(parts[0], default=0) or 0
            if uid <= 0:
                continue
            uname = parts[1] if len(parts) > 1 and not parts[1].isdigit() else ""
            level_part = parts[2] if len(parts) > 2 else parts[1] if len(parts) > 1 else default_level
            level = safe_int(level_part, default=default_level) or default_level
            self.upsert_guard(uid, uname, max(1, min(3, level)), "import")
            count += 1
        return count

    def guard_member_allowed(self, uid: int, required_guard_level: int) -> bool:
        if uid <= 0:
            return False
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT best_guard_level FROM guards WHERE uid = ?",
                (uid,),
            ).fetchone()
        return row is not None and guard_allowed(row["best_guard_level"], required_guard_level)

    def has_guard_record(self, uid: int) -> bool:
        if uid <= 0:
            return False
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM guards WHERE uid = ? LIMIT 1",
                (uid,),
            ).fetchone()
        return row is not None

    def enqueue_if_allowed(self, danmu: DanmuMessage, settings: dict[str, Any]) -> tuple[bool, int | None, str]:
        if not keyword_matches(danmu.message, settings["keyword"]):
            return False, None, "keyword_miss"

        mode = settings["eligibility_mode"]
        required_level = settings["required_guard_level"]
        if mode == "current":
            eligible = guard_allowed(danmu.guard_level, required_level)
        elif mode == "historical":
            eligible = self.has_guard_record(danmu.uid)
        else:
            eligible = True

        if not eligible:
            return False, None, "not_eligible"

        with self.lock, self._connect() as conn:
            if not settings["allow_repeat"]:
                duplicate = conn.execute(
                    """
                    SELECT 1
                    FROM queue
                    WHERE (uid > 0 AND uid = ?) OR (uid = 0 AND uname != '' AND uname = ?)
                    LIMIT 1
                    """,
                    (danmu.uid, danmu.uname),
                ).fetchone()
                if duplicate is not None:
                    return False, None, "duplicate"

            row = conn.execute("SELECT COALESCE(MAX(queue_no), 0) + 1 AS next_no FROM queue").fetchone()
            queue_no = int(row["next_no"])
            conn.execute(
                """
                INSERT INTO queue(queue_no, queued_at, danmu_time, uid, uname, message, guard_level, note)
                VALUES(?, ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    queue_no,
                    local_now(),
                    parse_danmu_time(danmu.danmu_unix_ts),
                    danmu.uid,
                    danmu.uname,
                    danmu.message,
                    danmu.guard_level,
                ),
            )
        return True, queue_no, "queued"

    def clear_queue(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM queue")

    def hide_overlay_queue_item(self, queue_no: int) -> bool:
        if queue_no <= 0:
            return False
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE queue
                SET overlay_hidden_at = ?
                WHERE queue_no = ?
                """,
                (local_now(), queue_no),
            )
            return cursor.rowcount > 0

    def reset_overlay_queue(self) -> int:
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE queue
                SET overlay_hidden_at = ''
                WHERE COALESCE(overlay_hidden_at, '') != ''
                """
            )
            return cursor.rowcount

    def update_queue_note(self, queue_no: int, note: str) -> bool:
        if queue_no <= 0:
            return False
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE queue
                SET note = ?
                WHERE queue_no = ?
                """,
                (note.strip(), queue_no),
            )
            return cursor.rowcount > 0

    def log_event(self, level: str, message: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO logs(created_at, level, message) VALUES(?, ?, ?)",
                (local_now(), level, message),
            )
            conn.execute(
                """
                DELETE FROM logs
                WHERE id NOT IN (
                    SELECT id FROM logs ORDER BY id DESC LIMIT 500
                )
                """
            )

    def list_queue(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT queue_no, queued_at, danmu_time, uid, uname, message, guard_level, note, overlay_hidden_at
                FROM queue
                ORDER BY queue_no ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_with_guard_name(row) for row in rows]

    def list_overlay_queue(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT queue_no, queued_at, danmu_time, uid, uname, message, guard_level, note, overlay_hidden_at
                FROM queue
                WHERE COALESCE(overlay_hidden_at, '') = ''
                ORDER BY queue_no ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_with_guard_name(row) for row in rows]

    def list_guards(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT uid, uname, best_guard_level, last_guard_level, first_seen_at, last_seen_at, source
                FROM guards
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["best_guard_name"] = guard_name(row["best_guard_level"])
            item["last_guard_name"] = guard_name(row["last_guard_level"])
            result.append(item)
        return result

    def list_logs(self, limit: int = 160) -> list[dict[str, Any]]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, level, message
                FROM logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def counts(self) -> dict[str, int]:
        with self.lock, self._connect() as conn:
            queue_count = conn.execute("SELECT COUNT(*) AS total FROM queue").fetchone()["total"]
            guard_count = conn.execute("SELECT COUNT(*) AS total FROM guards").fetchone()["total"]
        return {"queue": int(queue_count), "guards": int(guard_count)}

    def export_queue_csv(self) -> str:
        rows = self.list_queue(limit=100000)
        output = io.StringIO()
        fieldnames = ["queue_no", "queued_at", "danmu_time", "uid", "uname", "guard_name", "message", "note"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
        return output.getvalue()

    def export_queue_txt(self) -> str:
        lines = []
        for row in self.list_queue(limit=100000):
            name = str(row.get("uname") or f"UID {row.get('uid') or '未知'}").strip()
            note = str(row.get("note") or "").strip()
            line = f"{row.get('queue_no')}. {name}"
            if note:
                line = f"{line} {note}"
            lines.append(line)
        return "\n".join(lines) + ("\n" if lines else "")

    def export_guards_csv(self) -> str:
        rows = self.list_guards(limit=100000)
        output = io.StringIO()
        fieldnames = [
            "uid",
            "uname",
            "best_guard_name",
            "best_guard_level",
            "last_guard_name",
            "last_guard_level",
            "first_seen_at",
            "last_seen_at",
            "source",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
        return output.getvalue()

    def _row_with_guard_name(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        guard_level = safe_int(row["guard_level"], default=0) or 0
        if guard_level > 0:
            item["guard_name"] = guard_name(guard_level)
        else:
            uid = safe_int(item.get("uid"), default=0) or 0
            item["guard_name"] = "曾在舰" if self.has_guard_record(uid) else "无"
        return item


class LocalDanmuQueueApp:
    def __init__(self, db_path: Path, config_path: Path | None = None) -> None:
        self.store = QueueStore(db_path)
        self.status_lock = threading.RLock()
        self.status = ListenerStatus()
        self.cookie_lock = threading.RLock()
        self.cookie = os.environ.get("BILI_COOKIE")
        self.config_path = config_path or default_config_path()
        self.stop_event = threading.Event()
        self.listener_thread: threading.Thread | None = None
        self.history_lock = threading.RLock()
        self.seen_danmu_keys: dict[str, float] = {}
        self._load_local_config()

    def get_state(self) -> dict[str, Any]:
        with self.status_lock:
            status = asdict(self.status)
        state = {
            "settings": self.store.get_settings(),
            "cookie": self.get_cookie() or "",
            "status": status,
            "counts": self.store.counts(),
            "queue": self.store.list_queue(),
            "overlay_queue": self.store.list_overlay_queue(),
            "guards": self.store.list_guards(),
            "logs": self.store.list_logs(),
        }
        return state

    def _load_local_config(self) -> None:
        try:
            payload = load_local_config(self.config_path)
        except Exception as exc:
            self.store.log_event("warn", f"本地 config 读取失败：{exc}")
            return
        if not payload:
            return
        self.store.update_settings(payload["settings"])
        if payload.get("cookie"):
            self.set_cookie(str(payload["cookie"]))
        self.store.log_event("info", f"已载入本地 config：{self.config_path}")

    def persist_local_config(self) -> None:
        try:
            save_local_config(self.config_path, self.store.get_settings(), self.get_cookie())
        except Exception as exc:
            self.store.log_event("warn", f"本地 config 保存失败：{exc}")

    def start(self, room: str | None = None) -> None:
        settings = self.store.get_settings()
        if room is not None:
            settings = self.store.update_settings({"room": room})
        if not settings["room"]:
            raise ValueError("请先填写直播间号。")

        with self.status_lock:
            if self.status.running or (self.listener_thread is not None and self.listener_thread.is_alive()):
                return
            self.stop_event.clear()
            self.status = ListenerStatus(
                running=True,
                connected=False,
                room=settings["room"],
                started_at=local_now(),
            )

        self.listener_thread = threading.Thread(
            target=self._thread_entry,
            args=(settings["room"],),
            name="bilibili-danmu-listener",
            daemon=True,
        )
        self.listener_thread.start()
        self.store.log_event("info", f"开始监听直播间 {settings['room']}")

    def stop(self) -> None:
        self.stop_event.set()
        with self.status_lock:
            if self.status.running:
                self.status = ListenerStatus(
                    running=True,
                    connected=False,
                    room=self.status.room,
                    real_room_id=self.status.real_room_id,
                    started_at=self.status.started_at,
                    last_error="正在停止监听",
                )
        self.store.log_event("info", "已请求停止监听")

    def set_cookie(self, cookie: str | None) -> None:
        with self.cookie_lock:
            self.cookie = cookie.strip() if cookie else os.environ.get("BILI_COOKIE")

    def get_cookie(self) -> str | None:
        with self.cookie_lock:
            return self.cookie

    def _thread_entry(self, room: str) -> None:
        try:
            asyncio.run(self._listen_forever(room))
        finally:
            with self.status_lock:
                self.status = ListenerStatus(
                    running=False,
                    connected=False,
                    room=self.status.room,
                    real_room_id=self.status.real_room_id,
                    started_at=self.status.started_at,
                    last_error=self.status.last_error,
                )

    async def _listen_forever(self, room: str) -> None:
        cookie = self.get_cookie()
        try:
            real_room_id = resolve_room_id(int(room), cookie)
        except ValueError as exc:
            raise RuntimeError("直播间号需要是数字。") from exc

        with self.status_lock:
            self.status = ListenerStatus(
                running=True,
                connected=False,
                room=room,
                real_room_id=real_room_id,
                started_at=self.status.started_at,
            )
        self.store.log_event("info", f"真实 room_id：{real_room_id}")
        await self._prime_history_cache(real_room_id, cookie)

        attempt = 0
        while not self.stop_event.is_set():
            try:
                await self._listen_once(real_room_id, cookie)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                attempt += 1
                message = str(exc)
                with self.status_lock:
                    self.status = ListenerStatus(
                        running=True,
                        connected=False,
                        room=room,
                        real_room_id=real_room_id,
                        started_at=self.status.started_at,
                        last_error=message,
                    )
                delay = min(45, 2**min(attempt, 5))
                self.store.log_event("error", f"{message}；{delay} 秒后重连")
                await self._poll_history_until_stop(real_room_id, cookie, delay)

    async def _listen_once(self, real_room_id: int, cookie: str | None) -> None:
        danmu_info = get_danmu_info(real_room_id, cookie)
        host_list = danmu_info.get("host_list") or []
        if not host_list:
            raise RuntimeError("B 站未返回弹幕服务器列表。")
        errors: list[str] = []
        for host in host_list:
            if self.stop_event.is_set():
                return
            try:
                await self._listen_host(real_room_id, cookie, danmu_info, host)
                return
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                message = friendly_ws_error(exc)
                host_name = str(host.get("host") or "unknown")
                errors.append(f"{host_name}: {message}")
                self.store.log_event("warn", f"弹幕线路断开，切换下一条：{host_name} - {message}")
                await self._poll_history_safely(real_room_id, cookie)

        detail = "；".join(errors[-3:]) if errors else "没有可用线路"
        raise RuntimeError(f"弹幕线路都连接失败：{detail}")

    async def _listen_host(
        self,
        real_room_id: int,
        cookie: str | None,
        danmu_info: dict[str, Any],
        host: dict[str, Any],
    ) -> None:
        port = safe_int(host.get("wss_port"), default=443) or 443
        uri = f"wss://{host['host']}:{port}/sub"

        self.store.log_event("info", f"连接弹幕服务器 {host['host']}")
        websocket = await open_websocket(uri, http_headers(real_room_id, cookie))
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            auth = build_auth_payload(real_room_id, str(danmu_info.get("token") or ""), cookie, 2)
            await websocket.send(build_packet(json.dumps(auth, separators=(",", ":")), OP_AUTH))

            while not self.stop_event.is_set():
                try:
                    frame = await asyncio.wait_for(websocket.recv(), timeout=1)
                except TimeoutError:
                    continue
                if isinstance(frame, str):
                    frame = frame.encode("utf-8")
                for packet in iter_packets(frame):
                    if packet.op == OP_AUTH_REPLY:
                        body = decode_json_body(packet.body)
                        if body.get("code") != 0:
                            raise RuntimeError(f"弹幕服务器认证失败：{body}")
                        with self.status_lock:
                            self.status = ListenerStatus(
                                running=True,
                                connected=True,
                                room=self.status.room,
                                real_room_id=real_room_id,
                                started_at=self.status.started_at,
                            )
                        self.store.log_event("info", "弹幕服务器已连接")
                        if heartbeat_task is None:
                            heartbeat_task = asyncio.create_task(self._heartbeat(websocket))
                    elif packet.op == OP_HEARTBEAT_REPLY and len(packet.body) >= 4:
                        continue
                    elif packet.op == OP_MESSAGE:
                        payload = decode_json_body(packet.body)
                        self._handle_payload(payload)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            try:
                await websocket.close()
            except Exception:
                pass
            with self.status_lock:
                if self.status.running:
                    self.status = ListenerStatus(
                        running=True,
                        connected=False,
                        room=self.status.room,
                        real_room_id=real_room_id,
                        started_at=self.status.started_at,
                        last_error=self.status.last_error,
                    )

    async def _heartbeat(self, websocket: Any) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(30)
            await websocket.send(build_packet(b"", OP_HEARTBEAT))

    async def _sleep_until_stop(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        end_at = loop.time() + delay
        while not self.stop_event.is_set():
            remaining = end_at - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.5, remaining))

    async def _prime_history_cache(self, real_room_id: int, cookie: str | None) -> None:
        try:
            history = await asyncio.to_thread(get_danmu_history, real_room_id, cookie)
        except Exception as exc:
            self.store.log_event("warn", f"历史弹幕缓存初始化失败：{friendly_ws_error(exc)}")
            return

        for key, danmu in history:
            self._remember_danmu(danmu, key)

    async def _poll_history_until_stop(self, real_room_id: int, cookie: str | None, delay: float) -> None:
        loop = asyncio.get_running_loop()
        end_at = loop.time() + delay
        next_poll_at = 0.0
        last_error_log_at = 0.0
        while not self.stop_event.is_set():
            now = loop.time()
            if now >= end_at:
                return
            if now >= next_poll_at:
                try:
                    await asyncio.to_thread(self._poll_history_once, real_room_id, cookie)
                except Exception as exc:
                    if now - last_error_log_at >= 10:
                        self.store.log_event("warn", f"历史弹幕补漏失败：{friendly_ws_error(exc)}")
                        last_error_log_at = now
                next_poll_at = now + 1

            await asyncio.sleep(min(0.25, max(0.05, min(end_at, next_poll_at) - loop.time())))

    async def _poll_history_safely(self, real_room_id: int, cookie: str | None) -> None:
        try:
            await asyncio.to_thread(self._poll_history_once, real_room_id, cookie)
        except Exception as exc:
            self.store.log_event("warn", f"历史弹幕补漏失败：{friendly_ws_error(exc)}")

    def _poll_history_once(self, real_room_id: int, cookie: str | None) -> int:
        saved_count = 0
        for key, danmu in get_danmu_history(real_room_id, cookie):
            if not self._remember_danmu(danmu, key):
                continue
            if self._record_danmu(danmu, "history"):
                saved_count += 1
        return saved_count

    def _remember_danmu(self, danmu: DanmuMessage, source_key: str | None = None) -> bool:
        now = time.monotonic()
        keys = [f"fp:{danmu_fingerprint(danmu)}"]
        if source_key:
            keys.append(source_key)

        with self.history_lock:
            expired = [
                key
                for key, seen_at in self.seen_danmu_keys.items()
                if now - seen_at > 10 * 60
            ]
            for key in expired:
                self.seen_danmu_keys.pop(key, None)

            if any(key in self.seen_danmu_keys for key in keys):
                return False
            for key in keys:
                self.seen_danmu_keys[key] = now
            return True

    def _record_danmu(self, danmu: DanmuMessage, source: str) -> bool:
        if danmu.guard_level > 0:
            self.store.upsert_guard(danmu.uid, danmu.uname, danmu.guard_level, source)
        elif danmu.medal_level >= FORMER_CAPTAIN_MEDAL_LEVEL:
            self.store.upsert_guard(danmu.uid, danmu.uname, 3, "former_captain_medal")

        settings = self.store.get_settings()
        saved, queue_no, reason = self.store.enqueue_if_allowed(danmu, settings)
        if saved:
            label = "历史补漏" if source == "history" else "排队"
            self.store.log_event(
                "success",
                f"{label} #{queue_no}：{danmu.uname}({danmu.uid}) {guard_name(danmu.guard_level)}",
            )
            return True
        if reason == "not_eligible":
            self.store.log_event(
                "warn",
                f"未满足资格：{danmu.uname}({danmu.uid}) - {danmu.message}",
            )
        elif reason == "duplicate":
            self.store.log_event(
                "warn",
                f"重复排队：{danmu.uname}({danmu.uid})",
            )
        return False

    def _handle_payload(self, payload: dict[str, Any]) -> None:
        guard_buy = extract_guard_buy(payload)
        if guard_buy is not None:
            self.store.upsert_guard(
                guard_buy.uid,
                guard_buy.uname,
                guard_buy.guard_level,
                "guard_buy",
            )
            self.store.log_event(
                "info",
                f"记录上舰：{guard_buy.uname}({guard_buy.uid}) {guard_name(guard_buy.guard_level)}",
            )
            return

        danmu = extract_danmu(payload)
        if danmu is None:
            return
        if not self._remember_danmu(danmu):
            return
        self._record_danmu(danmu, "danmu")


class ApiHandler(BaseHTTPRequestHandler):
    app: LocalDanmuQueueApp
    web_root: Path

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_head_response("application/json; charset=utf-8")
            return
        if parsed.path in {"/api/export/queue.csv", "/api/export/guards.csv"}:
            self.send_head_response("text/csv; charset=utf-8")
            return
        if parsed.path == "/api/export/queue.txt":
            self.send_head_response("text/plain; charset=utf-8")
            return
        self.serve_static(parsed.path, head_only=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json(self.app.get_state())
            return
        if parsed.path == "/api/export/queue.csv":
            self.send_csv("danmu_queue.csv", self.app.store.export_queue_csv())
            return
        if parsed.path == "/api/export/queue.txt":
            self.send_text("danmu_queue.txt", self.app.store.export_queue_txt())
            return
        if parsed.path == "/api/export/guards.csv":
            self.send_csv("danmu_guard_members.csv", self.app.store.export_guards_csv())
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self.read_json_body()
            if parsed.path == "/api/settings":
                settings = self.app.store.update_settings(body)
                if "cookie" in body:
                    self.app.set_cookie(str(body.get("cookie") or ""))
                self.app.persist_local_config()
                self.app.store.log_event("info", "设置已保存")
                self.send_json({"ok": True, "settings": settings})
            elif parsed.path == "/api/connect":
                if body:
                    if "cookie" in body:
                        self.app.set_cookie(str(body.get("cookie") or ""))
                    self.app.store.update_settings(body)
                    self.app.persist_local_config()
                self.app.start()
                self.send_json({"ok": True})
            elif parsed.path == "/api/disconnect":
                self.app.stop()
                self.send_json({"ok": True})
            elif parsed.path == "/api/shutdown":
                self.app.stop()
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, name="shutdown-server", daemon=True).start()
            elif parsed.path == "/api/queue/clear":
                self.app.store.clear_queue()
                self.app.store.log_event("info", "队列已清空")
                self.send_json({"ok": True})
            elif parsed.path == "/api/queue/overlay/hide":
                queue_no = safe_int(body.get("queue_no"), default=0) or 0
                if not self.app.store.hide_overlay_queue_item(queue_no):
                    raise ValueError("未找到这个排队序号。")
                self.app.store.log_event("info", f"overlay 已隐藏 #{queue_no}")
                self.send_json({"ok": True})
            elif parsed.path == "/api/queue/overlay/reset":
                restored = self.app.store.reset_overlay_queue()
                self.app.store.log_event("info", f"overlay 已恢复展示 {restored} 条")
                self.send_json({"ok": True, "restored": restored})
            elif parsed.path == "/api/queue/note":
                queue_no = safe_int(body.get("queue_no"), default=0) or 0
                note = str(body.get("note") or "")
                if not self.app.store.update_queue_note(queue_no, note):
                    raise ValueError("未找到这个排队序号。")
                self.app.store.log_event("info", f"已更新 #{queue_no} 备注")
                self.send_json({"ok": True})
            elif parsed.path == "/api/guards/import":
                settings = self.app.store.get_settings()
                imported = self.app.store.import_guards(
                    str(body.get("text") or ""),
                    settings["required_guard_level"],
                )
                self.app.store.log_event("info", f"已导入 {imported} 个舰队成员")
                self.send_json({"ok": True, "imported": imported})
            elif parsed.path == "/api/config/clear":
                if self.app.config_path.exists():
                    self.app.config_path.unlink()
                self.app.set_cookie("")
                self.app.store.log_event("info", "本地 config 已清除")
                self.send_json({"ok": True})
            else:
                self.send_error(404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)

    def read_json_body(self) -> dict[str, Any]:
        length = safe_int(self.headers.get("Content-Length"), default=0) or 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_csv(self, filename: str, text: str) -> None:
        raw = text.encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, filename: str, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_head_response(self, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_static(self, request_path: str, head_only: bool = False) -> None:
        path = unquote(request_path)
        if path == "/":
            path = "/index.html"
        elif path == "/overlay":
            path = "/overlay.html"
        file_path = (self.web_root / path.lstrip("/")).resolve()
        try:
            file_path.relative_to(self.web_root.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return

        raw = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or file_path.suffix in {".js", ".css"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if not head_only:
            self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        return


def run_server(host: str, port: int, db_path: Path, web_root: Path, open_browser: bool = False) -> None:
    app = LocalDanmuQueueApp(db_path)
    app.store.log_event("info", "本地应用已启动")

    handler = type(
        "DanmuQueueApiHandler",
        (ApiHandler,),
        {"app": app, "web_root": web_root},
    )
    server = ThreadingHTTPServer((host, port), handler)
    url = local_ui_url(host, port)
    print(f"[{local_now()}] DanmuQueue UI: {url}", flush=True)
    print(f"[{local_now()}] 数据目录: {db_path.resolve().parent}", flush=True)
    if open_browser:
        open_management_page_later(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.stop()
        server.shutdown()
        print(f"[{local_now()}] 已停止。", flush=True)
    finally:
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DanmuQueue 本地 UI 应用。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1。")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765。")
    parser.add_argument("--db", default=None, help="SQLite 数据库文件；打包后默认保存到用户数据目录。")
    parser.add_argument(
        "--open-browser",
        action="store_true",
        default=bool(getattr(sys, "frozen", False)),
        help="启动后自动打开管理页；打包应用默认开启。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = resource_root()
    db_path = Path(args.db) if args.db else default_db_path()
    run_server(args.host, args.port, db_path, root / "web", open_browser=args.open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
