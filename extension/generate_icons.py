#!/usr/bin/env python3
"""Generate simple extension icons."""

import struct
import zlib

def create_png(width, height, rgb_color):
    """Create a simple solid color PNG."""
    r, g, b = rgb_color

    def png_chunk(chunk_type, data):
        chunk = chunk_type + data
        return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)

    # PNG signature
    signature = b'\x89PNG\r\n\x1a\n'

    # IHDR chunk
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    ihdr = png_chunk(b'IHDR', ihdr_data)

    # IDAT chunk (image data)
    raw_data = b''
    for _ in range(height):
        raw_data += b'\x00'  # filter byte
        for _ in range(width):
            raw_data += bytes([r, g, b])

    compressed = zlib.compress(raw_data, 9)
    idat = png_chunk(b'IDAT', compressed)

    # IEND chunk
    iend = png_chunk(b'IEND', b'')

    return signature + ihdr + idat + iend


def create_circle_png(size, bg_color, circle_color):
    """Create a PNG with a colored circle on background."""

    def png_chunk(chunk_type, data):
        chunk = chunk_type + data
        return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)

    # PNG signature
    signature = b'\x89PNG\r\n\x1a\n'

    # IHDR chunk (RGBA)
    ihdr_data = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    ihdr = png_chunk(b'IHDR', ihdr_data)

    # Generate pixel data
    center = size / 2
    radius = size / 2 - 1

    raw_data = b''
    for y in range(size):
        raw_data += b'\x00'  # filter byte
        for x in range(size):
            dx = x - center + 0.5
            dy = y - center + 0.5
            dist = (dx * dx + dy * dy) ** 0.5

            if dist <= radius:
                # Inside circle
                r, g, b = circle_color
                a = 255
            else:
                # Outside circle (transparent)
                r, g, b = bg_color
                a = 0

            raw_data += bytes([r, g, b, a])

    compressed = zlib.compress(raw_data, 9)
    idat = png_chunk(b'IDAT', compressed)

    # IEND chunk
    iend = png_chunk(b'IEND', b'')

    return signature + ihdr + idat + iend


def main():
    # YouTube red color
    yt_red = (255, 0, 0)
    bg = (255, 255, 255)

    sizes = [16, 48, 128]

    for size in sizes:
        png_data = create_circle_png(size, bg, yt_red)
        with open(f'icons/icon{size}.png', 'wb') as f:
            f.write(png_data)
        print(f'Created icons/icon{size}.png')


if __name__ == '__main__':
    main()
