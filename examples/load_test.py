"""Concurrent connection load test for the Gemini Live proxy.

By default clients complete Gemini setup and then remain idle. Pass --prompt to
send one text turn per client; doing so consumes additional Gemini quota.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from websockets.asyncio.client import connect


MODEL = "models/gemini-3.1-flash-live-preview"

load_dotenv(".orbit-token.env", override=True)


@dataclass
class Stats:
    active: int = 0
    connected: int = 0
    failed: int = 0
    received_messages: int = 0
    received_bytes: int = 0
    sent_messages: int = 0
    sent_bytes: int = 0
    connection_times_ms: List[float] = field(default_factory=list)


@dataclass(frozen=True)
class ProcessSample:
    cpu_seconds: float
    rss_bytes: int


def process_sample(pid: int) -> ProcessSample:
    stat = Path(f"/proc/{pid}/stat").read_text()
    # Everything after the final ')' starts with field 3 (process state).
    fields = stat[stat.rfind(")") + 2 :].split()
    clock_ticks = os.sysconf("SC_CLK_TCK")
    page_size = os.sysconf("SC_PAGE_SIZE")
    cpu_seconds = (int(fields[11]) + int(fields[12])) / clock_ticks
    rss_bytes = int(fields[21]) * page_size
    return ProcessSample(cpu_seconds=cpu_seconds, rss_bytes=rss_bytes)


def find_proxy_pid() -> Optional[int]:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"gemini-live-proxy" in command or b"gemini_live_proxy.main:app" in command:
            return int(entry.name)
    return None


async def stream_silence(websocket, deadline: float, stats: Stats) -> None:
    # 40 ms, 16 kHz, mono, signed 16-bit PCM.
    audio = base64.b64encode(bytes(1280)).decode()
    message = json.dumps(
        {
            "realtimeInput": {
                "audio": {"mimeType": "audio/pcm;rate=16000", "data": audio}
            }
        }
    )
    next_send = time.monotonic()
    while time.monotonic() < deadline:
        await websocket.send(message)
        stats.sent_messages += 1
        stats.sent_bytes += len(message)
        next_send += 0.04
        await asyncio.sleep(max(0, next_send - time.monotonic()))


async def run_client(
    client_id: int,
    proxy_url: str,
    token: str,
    deadline: float,
    prompt: Optional[str],
    audio: bool,
    stats: Stats,
) -> None:
    started = time.monotonic()
    try:
        async with connect(
            proxy_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=15,
            max_size=16 * 1024 * 1024,
            compression=None,
        ) as websocket:
            setup_message = json.dumps(
                {
                    "setup": {
                        "model": MODEL,
                        "generationConfig": {"responseModalities": ["AUDIO"]},
                        "outputAudioTranscription": {},
                    }
                }
            )
            await websocket.send(setup_message)
            stats.sent_messages += 1
            stats.sent_bytes += len(setup_message)
            setup_response = await asyncio.wait_for(websocket.recv(), timeout=20)
            stats.received_messages += 1
            stats.received_bytes += len(setup_response)
            stats.connected += 1
            stats.active += 1
            stats.connection_times_ms.append((time.monotonic() - started) * 1000)

            if prompt:
                prompt_message = json.dumps({"realtimeInput": {"text": prompt}})
                await websocket.send(prompt_message)
                stats.sent_messages += 1
                stats.sent_bytes += len(prompt_message)

            audio_task = (
                asyncio.create_task(stream_silence(websocket, deadline, stats))
                if audio
                else None
            )

            try:
                while time.monotonic() < deadline:
                    timeout = min(1.0, max(0.01, deadline - time.monotonic()))
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout)
                    except TimeoutError:
                        continue
                    stats.received_messages += 1
                    stats.received_bytes += len(message)
            finally:
                if audio_task is not None:
                    audio_task.cancel()
                    await asyncio.gather(audio_task, return_exceptions=True)
                stats.active -= 1
    except Exception as exc:
        stats.failed += 1
        print(f"client={client_id} error={type(exc).__name__}: {exc}")


async def report(
    stats: Stats, deadline: float, proxy_pid: Optional[int]
) -> None:
    previous_sample: Optional[ProcessSample] = None
    previous_time = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        cpu_text = "n/a"
        rss_text = "n/a"
        if proxy_pid is not None:
            try:
                current_time = time.monotonic()
                sample = process_sample(proxy_pid)
                if previous_sample is not None:
                    elapsed = current_time - previous_time
                    cpu = 100 * (sample.cpu_seconds - previous_sample.cpu_seconds) / elapsed
                    cpu_text = f"{cpu:.1f}%"
                rss_text = f"{sample.rss_bytes / 1024 / 1024:.1f} MiB"
                previous_sample = sample
                previous_time = current_time
            except (FileNotFoundError, ProcessLookupError, ValueError):
                cpu_text = "process-ended"

        print(
            f"active={stats.active} connected={stats.connected} "
            f"failed={stats.failed} tx={stats.sent_messages} "
            f"rx={stats.received_messages} "
            f"proxy_cpu={cpu_text} proxy_rss={rss_text}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=5)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--ramp-ms", type=float, default=200)
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get(
            "PROXY_URL", " "
        ),
    )
    parser.add_argument("--proxy-pid", type=int)
    parser.add_argument(
        "--prompt",
        help="Send one text turn per client (uses additional Gemini quota)",
    )
    parser.add_argument(
        "--audio",
        action="store_true",
        help="Stream silent 16 kHz PCM at real-time speed",
    )
    args = parser.parse_args()

    if args.clients <= 0 or args.duration <= 0 or args.ramp_ms < 0:
        parser.error("clients/duration must be positive and ramp-ms cannot be negative")

    token = os.environ.get("ORBIT_USER_TOKEN")
    if not token:
        parser.error("ORBIT_USER_TOKEN environment variable is required")

    if args.proxy_pid is None:
        args.proxy_pid = find_proxy_pid()
        if args.proxy_pid is None:
            print("Warning: proxy process not found; CPU/RAM will show as n/a")
        else:
            print(f"Proxy process found pid={args.proxy_pid}")

    ramp_seconds = args.ramp_ms / 1000
    deadline = time.monotonic() + args.duration + (args.clients - 1) * ramp_seconds
    print(
        f"Starting clients={args.clients} duration={args.duration}s "
        f"ramp={args.ramp_ms}ms prompt={'yes' if args.prompt else 'no'} "
        f"audio={'yes' if args.audio else 'no'}"
    )

    stats = Stats()

    async def delayed_client(client_id: int) -> None:
        await asyncio.sleep(client_id * ramp_seconds)
        await run_client(
            client_id,
            args.proxy_url,
            token,
            deadline,
            args.prompt,
            args.audio,
            stats,
        )

    clients = [asyncio.create_task(delayed_client(i)) for i in range(args.clients)]
    reporter = asyncio.create_task(report(stats, deadline, args.proxy_pid))
    await asyncio.gather(*clients)
    if not reporter.done():
        reporter.cancel()
    await asyncio.gather(reporter, return_exceptions=True)

    timings = stats.connection_times_ms
    median = statistics.median(timings) if timings else 0
    p95 = sorted(timings)[min(len(timings) - 1, int(len(timings) * 0.95))] if timings else 0
    print(
        f"Done connected={stats.connected} failed={stats.failed} "
        f"connect_p50={median:.0f}ms connect_p95={p95:.0f}ms "
        f"tx_messages={stats.sent_messages} "
        f"tx_mib={stats.sent_bytes / 1024 / 1024:.2f} "
        f"rx_messages={stats.received_messages} "
        f"rx_mib={stats.received_bytes / 1024 / 1024:.2f}"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user")
