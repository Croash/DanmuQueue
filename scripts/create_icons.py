#!/usr/bin/env python3
"""Generate placeholder app icons for DanmuQueue without third-party libraries."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "packaging"
ICONSET = OUT / "icon.iconset"

D = [
    "11110",
    "10001",
    "10001",
    "10001",
    "10001",
    "10001",
    "11110",
]

Q = [
    "01110",
    "10001",
    "10001",
    "10001",
    "10101",
    "10010",
    "01101",
]


def rgba_png(size: int) -> bytes:
    pixels = bytearray()
    radius = size * 0.22
    bg = (251, 114, 153, 255)
    shadow = (185, 55, 96, 255)
    fg = (255, 255, 255, 255)

    for y in range(size):
        row = bytearray()
        for x in range(size):
            alpha = rounded_alpha(x, y, size, radius)
            color = bg
            if alpha > 0 and y > size * 0.72:
                color = blend(bg, shadow, 0.18)
            row.extend((color[0], color[1], color[2], min(alpha, color[3])))
        pixels.extend(b"\x00" + row)

    canvas = bytearray(pixels)
    draw_letters(canvas, size, fg)
    return encode_png(size, size, bytes(canvas))


def rounded_alpha(x: int, y: int, size: int, radius: float) -> int:
    inner_left = radius
    inner_right = size - radius - 1
    inner_top = radius
    inner_bottom = size - radius - 1
    cx = min(max(x, inner_left), inner_right)
    cy = min(max(y, inner_top), inner_bottom)
    distance = math.hypot(x - cx, y - cy)
    if distance <= radius - 1:
        return 255
    if distance >= radius:
        return 0
    return int((radius - distance) * 255)


def blend(a: tuple[int, int, int, int], b: tuple[int, int, int, int], amount: float) -> tuple[int, int, int, int]:
    return tuple(int(a[index] * (1 - amount) + b[index] * amount) for index in range(4))  # type: ignore[return-value]


def draw_letters(canvas: bytearray, size: int, color: tuple[int, int, int, int]) -> None:
    cell = max(1, size // 15)
    gap = max(1, cell)
    letter_w = 5 * cell
    total_w = letter_w * 2 + gap
    x0 = (size - total_w) // 2
    y0 = (size - 7 * cell) // 2
    draw_bitmap(canvas, size, D, x0, y0, cell, color)
    draw_bitmap(canvas, size, Q, x0 + letter_w + gap, y0, cell, color)


def draw_bitmap(
    canvas: bytearray,
    size: int,
    rows: list[str],
    x0: int,
    y0: int,
    cell: int,
    color: tuple[int, int, int, int],
) -> None:
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            if value != "1":
                continue
            for y in range(y0 + row_index * cell, y0 + (row_index + 1) * cell):
                for x in range(x0 + col_index * cell, x0 + (col_index + 1) * cell):
                    set_pixel(canvas, size, x, y, color)


def set_pixel(canvas: bytearray, size: int, x: int, y: int, color: tuple[int, int, int, int]) -> None:
    if x < 0 or y < 0 or x >= size or y >= size:
        return
    index = y * (1 + size * 4) + 1 + x * 4
    canvas[index : index + 4] = bytes(color)


def encode_png(width: int, height: int, raw_rows: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw_rows, 9)) + chunk(b"IEND", b"")


def write_ico(images: list[tuple[int, bytes]], path: Path) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    directory = bytearray()
    payload = bytearray()
    offset = 6 + len(images) * 16
    for size, png in images:
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                0 if size >= 256 else size,
                0 if size >= 256 else size,
                0,
                0,
                1,
                32,
                len(png),
                offset,
            )
        )
        payload.extend(png)
        offset += len(png)
    path.write_bytes(header + directory + payload)


def write_icns(pngs: dict[int, bytes], path: Path) -> None:
    icon_types = {
        16: b"icp4",
        32: b"icp5",
        64: b"icp6",
        128: b"ic07",
        256: b"ic08",
        512: b"ic09",
        1024: b"ic10",
    }
    chunks = bytearray()
    for size, kind in icon_types.items():
        data = pngs[size]
        chunks.extend(kind + struct.pack(">I", len(data) + 8) + data)
    path.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    pngs = {size: rgba_png(size) for size in (16, 32, 64, 128, 256, 512, 1024)}
    (OUT / "icon.png").write_bytes(pngs[1024])
    write_ico([(16, pngs[16]), (32, pngs[32]), (64, pngs[64]), (256, pngs[256])], OUT / "icon.ico")
    write_icns(pngs, OUT / "icon.icns")

    ICONSET.mkdir(exist_ok=True)
    names = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in names.items():
        (ICONSET / name).write_bytes(pngs[size])

    print(f"Wrote icons to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
