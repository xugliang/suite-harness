"""Incremental bounded SSE decoding shared by both HTTP transports."""

from __future__ import annotations

from .models import McpProtocolError


async def read_bounded(chunks, maximum):
    data = bytearray()
    async for chunk in chunks:
        if len(data) + len(chunk) > maximum:
            raise McpProtocolError("MCP HTTP response exceeds max_message_bytes")
        data.extend(chunk)
    return bytes(data)


async def iter_sse(chunks, *, max_message_bytes, max_events):
    from .transports import parse_sse

    line = bytearray()
    event = bytearray()
    count = 0
    after_cr = False
    async for chunk in chunks:
        for byte in chunk:
            if after_cr and byte == 10:
                after_cr = False
                continue
            after_cr = byte == 13
            if byte in (10, 13):
                if not line:
                    if event:
                        parsed = parse_sse(
                            bytes(event) + b"\n", max_message_bytes=max_message_bytes, max_events=1
                        )
                        event.clear()
                        for item in parsed:
                            count += 1
                            if count > max_events:
                                raise McpProtocolError("MCP SSE stream exceeds max_stream_events")
                            yield item
                else:
                    event.extend(line)
                    event.append(10)
                    line.clear()
            else:
                line.append(byte)
            if len(event) + len(line) > max_message_bytes:
                raise McpProtocolError("MCP SSE event exceeds max_message_bytes")
    # An incomplete final event is not dispatched (SSE event delimiter required).
