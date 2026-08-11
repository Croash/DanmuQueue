#!/usr/bin/env python3
"""Record Bilibili live danmaku queue requests into a CSV file."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    import websockets
except ImportError:  # pragma: no cover - handled at runtime with a friendly message.
    websockets = None


HEADER_LEN = 16
HEADER = struct.Struct(">IHHII")

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

PROTO_JSON = 0
PROTO_HEARTBEAT = 1
PROTO_ZLIB = 2
PROTO_BROTLI = 3

ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"
DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
DANMU_HISTORY_URL = "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory"
WBI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]

_wbi_cache: tuple[str, float] | None = None


@dataclass(frozen=True)
class Packet:
    op: int
    version: int
    body: bytes


@dataclass(frozen=True)
class DanmuMessage:
    uid: int
    uname: str
    message: str
    danmu_unix_ts: int | None
    guard_level: int = 0
    medal_level: int = 0


@dataclass(frozen=True)
class GuardBuyEvent:
    uid: int
    uname: str
    guard_level: int
    num: int
    price: int


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_unix_timestamp(value: int | None) -> float | None:
    if not value:
        return None
    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return timestamp


def log(message: str) -> None:
    print(f"[{local_now()}] {message}", flush=True)


def build_packet(body: bytes | str, op: int, version: int = PROTO_HEARTBEAT) -> bytes:
    if isinstance(body, str):
        body = body.encode("utf-8")
    packet_len = HEADER_LEN + len(body)
    return HEADER.pack(packet_len, HEADER_LEN, version, op, 1) + body


def iter_packets(data: bytes) -> Iterable[Packet]:
    offset = 0
    while offset + HEADER_LEN <= len(data):
        packet_len, header_len, version, op, _sequence = HEADER.unpack_from(data, offset)
        if packet_len < header_len or header_len < HEADER_LEN:
            raise ValueError(f"Invalid packet header: packet_len={packet_len}, header_len={header_len}")
        packet_end = offset + packet_len
        if packet_end > len(data):
            raise ValueError("Incomplete Bilibili packet")

        body = data[offset + header_len : packet_end]
        if op == OP_MESSAGE and version == PROTO_ZLIB:
            yield from iter_packets(zlib.decompress(body))
        elif op == OP_MESSAGE and version == PROTO_BROTLI:
            try:
                import brotli
            except ImportError as exc:
                raise RuntimeError(
                    "收到 Brotli 压缩弹幕，但未安装 brotli；请执行 `pip install brotli`，"
                    "或用默认的 --protover 2 连接。"
                ) from exc
            yield from iter_packets(brotli.decompress(body))
        else:
            yield Packet(op=op, version=version, body=body)

        offset = packet_end


def decode_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    text = body.decode("utf-8", errors="replace").strip("\x00")
    if not text:
        return {}
    return json.loads(text)


def extract_danmu_guard_level(info: list[Any]) -> int:
    # Bilibili commonly exposes guard level in the fan medal block:
    # 1=总督, 2=提督, 3=舰长, 0=无.
    fan_medal = info[3] if len(info) > 3 and isinstance(info[3], list) else []
    guard_level = safe_int(fan_medal[10], default=0) if len(fan_medal) > 10 else 0
    if guard_level:
        return guard_level

    old_guard_level = safe_int(info[6], default=0) if len(info) > 6 else 0
    if old_guard_level:
        return old_guard_level

    extra = info[0][15] if len(info) > 0 and isinstance(info[0], list) and len(info[0]) > 15 else None
    if isinstance(extra, dict):
        candidates = [
            extra.get("guard_level"),
            extra.get("guardLevel"),
            extra.get("user", {}).get("guard", {}).get("level")
            if isinstance(extra.get("user"), dict)
            else None,
        ]
        for candidate in candidates:
            parsed = safe_int(candidate, default=0) or 0
            if parsed:
                return parsed

    return 0


def extract_danmu_medal_level(info: list[Any]) -> int:
    fan_medal = info[3] if len(info) > 3 and isinstance(info[3], list) else []
    if not fan_medal:
        return 0
    return safe_int(fan_medal[0], default=0) or 0


def extract_danmu(payload: dict[str, Any]) -> DanmuMessage | None:
    cmd = str(payload.get("cmd", "")).split(":", 1)[0]
    if cmd != "DANMU_MSG":
        return None

    info = payload.get("info")
    if not isinstance(info, list) or len(info) < 3:
        return None

    message = str(info[1])
    user_info = info[2] if isinstance(info[2], list) else []
    uid = safe_int(user_info[0]) if len(user_info) > 0 else 0
    uname = str(user_info[1]) if len(user_info) > 1 else ""

    danmu_unix_ts = None
    meta = info[0] if isinstance(info[0], list) else []
    if len(meta) > 4:
        danmu_unix_ts = safe_int(meta[4], default=None)

    guard_level = extract_danmu_guard_level(info)
    medal_level = extract_danmu_medal_level(info)

    return DanmuMessage(
        uid=uid,
        uname=uname,
        message=message,
        danmu_unix_ts=danmu_unix_ts,
        guard_level=guard_level or 0,
        medal_level=medal_level,
    )


def parse_history_timeline(value: Any) -> int | None:
    if not value:
        return None
    text = str(value)
    try:
        return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone().timestamp())
    except ValueError:
        return None


def extract_history_danmu(item: dict[str, Any]) -> DanmuMessage | None:
    message = str(item.get("text") or "")
    if not message:
        return None

    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    user_base = user.get("base") if isinstance(user.get("base"), dict) else {}
    uid = safe_int(item.get("uid"), default=0) or 0
    uname = str(item.get("nickname") or item.get("uname") or user_base.get("name") or "")

    check_info = item.get("check_info") if isinstance(item.get("check_info"), dict) else {}
    danmu_unix_ts = safe_int(check_info.get("ts"), default=None)
    if danmu_unix_ts is None:
        danmu_unix_ts = parse_history_timeline(item.get("timeline"))

    medal = item.get("medal") if isinstance(item.get("medal"), list) else []
    user_medal = user.get("medal") if isinstance(user.get("medal"), dict) else {}
    medal_level = (
        safe_int(user_medal.get("level"), default=0)
        or (safe_int(medal[0], default=0) if medal else 0)
        or 0
    )
    guard_level = (
        safe_int(item.get("guard_level"), default=0)
        or safe_int(user_medal.get("guard_level"), default=0)
        or (safe_int(medal[10], default=0) if len(medal) > 10 else 0)
        or 0
    )

    return DanmuMessage(
        uid=uid,
        uname=uname,
        message=message,
        danmu_unix_ts=danmu_unix_ts,
        guard_level=guard_level,
        medal_level=medal_level,
    )


def danmu_fingerprint(danmu: DanmuMessage) -> str:
    normalized_ts = normalize_unix_timestamp(danmu.danmu_unix_ts)
    timestamp = int(normalized_ts) if normalized_ts else 0
    return f"{danmu.uid}|{danmu.uname}|{danmu.message}|{timestamp}"


def history_danmu_key(item: dict[str, Any], danmu: DanmuMessage) -> str:
    id_str = str(item.get("id_str") or "").strip()
    if id_str:
        return f"id:{id_str}"

    check_info = item.get("check_info") if isinstance(item.get("check_info"), dict) else {}
    ct = str(check_info.get("ct") or "").strip()
    ts = safe_int(check_info.get("ts"), default=0) or 0
    if ct or ts:
        return f"check:{ts}:{ct}:{danmu.uid}:{danmu.message}"

    return f"fp:{danmu_fingerprint(danmu)}"


def extract_guard_buy(payload: dict[str, Any]) -> GuardBuyEvent | None:
    cmd = str(payload.get("cmd", "")).split(":", 1)[0]
    if cmd != "GUARD_BUY":
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    uid = safe_int(data.get("uid"), default=0) or 0
    uname = str(data.get("username") or data.get("uname") or "")
    guard_level = safe_int(data.get("guard_level"), default=0) or 0
    num = safe_int(data.get("num"), default=0) or 0
    price = safe_int(data.get("price"), default=0) or 0
    if uid <= 0 or guard_level <= 0:
        return None

    return GuardBuyEvent(uid=uid, uname=uname, guard_level=guard_level, num=num, price=price)


def safe_int(value: Any, default: int | None = 0) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class QueueRecorder:
    fieldnames = ["queue_no", "queued_at", "danmu_time", "uid", "uname", "message"]

    def __init__(self, output: Path, allow_repeat: bool) -> None:
        self.output = output
        self.allow_repeat = allow_repeat
        self.seen: set[str] = set()
        self.next_no = 1
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.output.exists() or self.output.stat().st_size == 0:
            return

        with self.output.open("r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                queue_no = safe_int(row.get("queue_no"), default=None)
                if queue_no is not None:
                    self.next_no = max(self.next_no, queue_no + 1)
                key = self._identity_key(row.get("uid", ""), row.get("uname", ""))
                if key:
                    self.seen.add(key)

    def should_record(self, danmu: DanmuMessage, keyword: str) -> bool:
        return keyword in danmu.message

    def append(self, danmu: DanmuMessage) -> tuple[bool, int | None]:
        key = self._identity_key(danmu.uid, danmu.uname)
        if not self.allow_repeat and key and key in self.seen:
            return False, None

        self.output.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.output.exists() or self.output.stat().st_size == 0
        queue_no = self.next_no
        self.next_no += 1

        danmu_time = ""
        normalized_ts = normalize_unix_timestamp(danmu.danmu_unix_ts)
        if normalized_ts:
            danmu_time = datetime.fromtimestamp(normalized_ts).astimezone().isoformat(timespec="seconds")

        with self.output.open("a", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=self.fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "queue_no": queue_no,
                    "queued_at": local_now(),
                    "danmu_time": danmu_time,
                    "uid": danmu.uid,
                    "uname": danmu.uname,
                    "message": danmu.message,
                }
            )

        if key:
            self.seen.add(key)
        return True, queue_no

    @staticmethod
    def _identity_key(uid: Any, uname: Any) -> str | None:
        uid_int = safe_int(uid, default=0) or 0
        uname_text = str(uname or "").strip()
        if uid_int > 0:
            return f"uid:{uid_int}"
        if uname_text:
            return f"uname:{uname_text}"
        return None


def request_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    query = urlencode(params)
    full_url = f"{url}?{query}" if query else url
    req = Request(full_url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=10) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {full_url}") from exc
    except URLError as exc:
        raise RuntimeError(f"请求失败：{exc.reason}") from exc


def get_wbi_mixin_key(cookie: str | None) -> str:
    global _wbi_cache

    now = time.time()
    if _wbi_cache is not None:
        mixin_key, cached_at = _wbi_cache
        if now - cached_at < 6 * 60 * 60:
            return mixin_key

    headers = http_headers(0, cookie)
    headers["Referer"] = "https://www.bilibili.com/"
    data = request_json(WBI_NAV_URL, {}, headers)
    if data.get("code") != 0:
        raise RuntimeError(f"获取 WBI 签名 key 失败：{data.get('message') or data}")

    wbi_img = data.get("data", {}).get("wbi_img", {})
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    img_key = Path(urlparse(img_url).path).stem
    sub_key = Path(urlparse(sub_url).path).stem
    raw_key = img_key + sub_key
    if len(raw_key) < max(MIXIN_KEY_ENC_TAB) + 1:
        raise RuntimeError("获取 WBI 签名 key 失败：B 站返回的 key 不完整。")

    mixin_key = "".join(raw_key[index] for index in MIXIN_KEY_ENC_TAB)[:32]
    _wbi_cache = (mixin_key, now)
    return mixin_key


def sign_wbi_params(params: dict[str, Any], mixin_key: str, wts: int | None = None) -> dict[str, str]:
    signed = {str(key): str(value) for key, value in params.items() if value is not None}
    signed["wts"] = str(int(time.time()) if wts is None else wts)

    filtered = {
        key: "".join(char for char in value if char not in "!'()*")
        for key, value in signed.items()
    }
    query = urlencode(sorted(filtered.items()), quote_via=quote)
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return filtered


def parse_cookie(cookie: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not cookie:
        return result
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def build_auth_payload(room_id: int, token: str, cookie: str | None, protover: int = 2) -> dict[str, Any]:
    cookie_values = parse_cookie(cookie)
    uid = safe_int(cookie_values.get("DedeUserID"), default=0) or 0
    buvid = cookie_values.get("buvid3") or cookie_values.get("buvid4") or ""
    return {
        "uid": uid,
        "roomid": room_id,
        "protover": protover,
        "buvid": buvid,
        "platform": "web",
        "type": 2,
        "key": token,
    }


def http_headers(room_id: int, cookie: str | None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Referer": f"https://live.bilibili.com/{room_id}",
        "Origin": "https://live.bilibili.com",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def resolve_room_id(room_id: int, cookie: str | None) -> int:
    data = request_json(ROOM_INIT_URL, {"id": room_id}, http_headers(room_id, cookie))
    if data.get("code") != 0:
        raise RuntimeError(f"直播间不存在或不可访问：{data.get('message') or data.get('msg') or data}")
    real_room_id = safe_int(data.get("data", {}).get("room_id"), default=None)
    if real_room_id is None:
        raise RuntimeError(f"room_init 返回缺少 room_id：{data}")
    return real_room_id


def get_danmu_info(room_id: int, cookie: str | None) -> dict[str, Any]:
    params = {
        "id": room_id,
        "type": 0,
        "web_location": "444.8",
    }
    signed_params = sign_wbi_params(params, get_wbi_mixin_key(cookie))
    data = request_json(DANMU_INFO_URL, signed_params, http_headers(room_id, cookie))
    if data.get("code") != 0:
        message = data.get("message") or data.get("msg") or data.get("code") or data
        if data.get("code") == -352:
            message = (
                "-352（B 站拒绝了弹幕服务器请求；请确认 Cookie 来自已登录的 B 站页面，"
                "并包含 SESSDATA、buvid3 等字段）"
            )
        raise RuntimeError(f"获取弹幕服务器失败：{message}")
    return data.get("data", {})


def get_danmu_history(room_id: int, cookie: str | None) -> list[tuple[str, DanmuMessage]]:
    data = request_json(DANMU_HISTORY_URL, {"roomid": room_id}, http_headers(room_id, cookie))
    if data.get("code") != 0:
        raise RuntimeError(f"获取历史弹幕失败：{data.get('message') or data.get('msg') or data}")

    payload = data.get("data", {})
    if not isinstance(payload, dict):
        return []

    result: list[tuple[str, DanmuMessage]] = []
    for bucket in ("admin", "room"):
        rows = payload.get(bucket)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            danmu = extract_history_danmu(item)
            if danmu is None:
                continue
            result.append((history_danmu_key(item, danmu), danmu))

    return sorted(
        result,
        key=lambda pair: (
            normalize_unix_timestamp(pair[1].danmu_unix_ts) or 0,
            pair[0],
        ),
    )


async def open_websocket(uri: str, headers: dict[str, str]) -> Any:
    if websockets is None:
        raise RuntimeError("缺少依赖 websockets；请先执行 `pip install -r requirements.txt`。")

    options = {"ping_interval": None, "max_size": None, "compression": None, "open_timeout": 10, "close_timeout": 2}
    try:
        return await websockets.connect(uri, additional_headers=headers, **options)
    except TypeError:
        return await websockets.connect(uri, extra_headers=headers, **options)


async def heartbeat(websocket: Any) -> None:
    while True:
        await asyncio.sleep(30)
        await websocket.send(build_packet(b"", OP_HEARTBEAT))


async def listen_once(args: argparse.Namespace, recorder: QueueRecorder, real_room_id: int) -> None:
    danmu_info = get_danmu_info(real_room_id, args.cookie)
    host_list = danmu_info.get("host_list") or []
    if not host_list:
        raise RuntimeError(f"getDanmuInfo 返回缺少 host_list：{danmu_info}")

    host = host_list[0]
    port = safe_int(host.get("wss_port"), default=443) or 443
    uri = f"wss://{host['host']}:{port}/sub"
    headers = http_headers(real_room_id, args.cookie)

    log(f"连接弹幕服务器：{uri}")
    websocket = await open_websocket(uri, headers)
    heartbeat_task: asyncio.Task[None] | None = None

    try:
        auth = build_auth_payload(real_room_id, str(danmu_info.get("token") or ""), args.cookie, args.protover)
        await websocket.send(build_packet(json.dumps(auth, separators=(",", ":")), OP_AUTH))

        async for frame in websocket:
            if isinstance(frame, str):
                frame = frame.encode("utf-8")
            for packet in iter_packets(frame):
                if packet.op == OP_AUTH_REPLY:
                    body = decode_json_body(packet.body)
                    if body.get("code") != 0:
                        raise RuntimeError(f"弹幕服务器认证失败：{body}")
                    log("弹幕服务器认证成功，开始监听。")
                    if heartbeat_task is None:
                        heartbeat_task = asyncio.create_task(heartbeat(websocket))
                elif packet.op == OP_HEARTBEAT_REPLY and len(packet.body) >= 4:
                    online = struct.unpack(">I", packet.body[:4])[0]
                    if args.verbose:
                        log(f"直播间人气：{online}")
                elif packet.op == OP_MESSAGE:
                    payload = decode_json_body(packet.body)
                    danmu = extract_danmu(payload)
                    if danmu is None:
                        continue

                    if args.verbose:
                        log(f"弹幕 {danmu.uname}({danmu.uid})：{danmu.message}")

                    if recorder.should_record(danmu, args.keyword):
                        saved, queue_no = recorder.append(danmu)
                        if saved:
                            log(f"排队成功 #{queue_no}: {danmu.uname}({danmu.uid}) - {danmu.message}")
                        elif args.verbose:
                            log(f"已在队列中，跳过：{danmu.uname}({danmu.uid})")
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
        await websocket.close()


async def run(args: argparse.Namespace) -> None:
    recorder = QueueRecorder(Path(args.output), args.allow_repeat)
    real_room_id = resolve_room_id(args.room, args.cookie)
    log(f"直播间 {args.room} 的真实 room_id：{real_room_id}")
    log(f"队列记录文件：{Path(args.output).resolve()}")

    attempt = 0
    while True:
        try:
            await listen_once(args, recorder, real_room_id)
            attempt = 0
            log("弹幕连接已关闭，准备重连。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            delay = min(60, 2**min(attempt, 5)) + random.uniform(0, 1.5)
            log(f"监听出错：{exc}；{delay:.1f} 秒后重试。")
            await asyncio.sleep(delay)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="监听 B 站直播弹幕，命中关键词后写入排队 CSV。")
    parser.add_argument("-r", "--room", type=int, required=True, help="B 站直播间号，支持短号。")
    parser.add_argument("-o", "--output", default="queue.csv", help="队列记录文件，默认 queue.csv。")
    parser.add_argument("-k", "--keyword", default="排队", help="触发排队的关键词，默认“排队”。")
    parser.add_argument("--allow-repeat", action="store_true", help="允许同一用户重复排队。")
    parser.add_argument(
        "--cookie",
        default=os.environ.get("BILI_COOKIE"),
        help="可选，B 站 Cookie；也可以通过 BILI_COOKIE 环境变量传入。",
    )
    parser.add_argument("--protover", type=int, choices=[2, 3], default=2, help="弹幕压缩协议，默认 2(zlib)。")
    parser.add_argument("--verbose", action="store_true", help="打印所有收到的弹幕和心跳信息。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log("已停止。")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
