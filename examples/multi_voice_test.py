"""Send one microphone recording to many Gemini Live sessions concurrently."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import time
from array import array
from collections import deque
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set

from dotenv import load_dotenv
from websockets.asyncio.client import ClientConnection, connect

from load_test import find_proxy_pid, process_sample


MODEL = "models/gemini-3.1-flash-live-preview"
MIC_CHUNK_BYTES = 1280  # 40 ms, 16 kHz, mono, signed 16-bit PCM.

load_dotenv(".orbit-token.env", override=True)


@dataclass
class AssistantResponse:
    assistant_id: int
    transcript: List[str] = field(default_factory=list)
    audio: bytearray = field(default_factory=bytearray)
    error: Optional[str] = None


class SharedAudioMixer:
    """Mix all assistant PCM streams into one audio-device connection."""

    PERIOD_BYTES = 960  # 20 ms at 24 kHz, mono, signed 16-bit PCM.

    def __init__(self) -> None:
        self.buffers: Dict[int, bytearray] = {}
        self.announced: Set[int] = set()
        self.event = asyncio.Event()
        self.closing = False
        self.process: Optional[asyncio.subprocess.Process] = None
        self.task = asyncio.create_task(self._run())

    def submit(self, assistant_id: int, audio: bytes) -> None:
        self.buffers.setdefault(assistant_id, bytearray()).extend(audio)
        if assistant_id not in self.announced:
            self.announced.add(assistant_id)
            print(f"Asistan {assistant_id + 1} konuşmaya başladı")
        self.event.set()

    async def _start_player(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            "aplay",
            "-q",
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-c",
            "1",
            "-r",
            "24000",
            "--buffer-time=100000",
            "--period-time=20000",
            stdin=asyncio.subprocess.PIPE,
        )

    def _mixed_period(self) -> Optional[bytes]:
        source_periods: List[array] = []
        empty_ids: List[int] = []
        for assistant_id, buffer in self.buffers.items():
            if not buffer:
                empty_ids.append(assistant_id)
                continue
            take = min(len(buffer), self.PERIOD_BYTES)
            period = bytes(buffer[:take])
            del buffer[:take]
            if len(period) < self.PERIOD_BYTES:
                period += bytes(self.PERIOD_BYTES - len(period))
            samples = array("h")
            samples.frombytes(period)
            source_periods.append(samples)

        for assistant_id in empty_ids:
            self.buffers.pop(assistant_id, None)

        if not source_periods:
            return None

        # Average active voices to avoid severe clipping with dozens of streams.
        source_count = len(source_periods)
        mixed = array(
            "h",
            (
                max(
                    -32768,
                    min(
                        32767,
                        sum(source[index] for source in source_periods) // source_count,
                    ),
                )
                for index in range(self.PERIOD_BYTES // 2)
            ),
        )
        return mixed.tobytes()

    async def _run(self) -> None:
        next_write = time.monotonic()
        while True:
            period = self._mixed_period()
            if period is None:
                if self.closing:
                    break
                self.event.clear()
                await self.event.wait()
                continue

            if self.process is None:
                await self._start_player()
                next_write = time.monotonic()
            assert self.process.stdin is not None
            self.process.stdin.write(period)
            await self.process.stdin.drain()
            next_write += 0.02
            await asyncio.sleep(max(0, next_write - time.monotonic()))

        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await self.process.stdin.wait_closed()
            await self.process.wait()

    async def close(self) -> None:
        self.closing = True
        self.event.set()
        await self.task


def setup_message() -> str:
    return json.dumps(
        {
            "setup": {
                "model": MODEL,
                "generationConfig": {"responseModalities": ["AUDIO"]},
                "outputAudioTranscription": {},
                "inputAudioTranscription": {},
                "realtimeInputConfig": {
                    "automaticActivityDetection": {"disabled": True}
                },
                "systemInstruction": {
                    "parts": [
                        {
                            "text": (
                                "Kullanıcıya yalnızca Türkçe, doğal ve tek kısa "
                                "cümleyle cevap ver."
                            )
                        }
                    ]
                },
            }
        }
    )


def pcm_rms(chunk: bytes) -> int:
    samples = array("h")
    samples.frombytes(chunk)
    if not samples:
        return 0
    return math.isqrt(sum(sample * sample for sample in samples) // len(samples))


async def record_one_sentence(
    threshold: int,
    silence_ms: int,
    wait_seconds: float,
    max_speech_seconds: float,
) -> bytes:
    print(
        "Dinleniyor... Tek cümlenizi söyleyin; sustuğunuzda otomatik gönderilecek."
    )
    process = await asyncio.create_subprocess_exec(
        "arecord",
        "-q",
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-c",
        "1",
        "-r",
        "16000",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None
    pre_roll: Deque[bytes] = deque(maxlen=5)
    recording = bytearray()
    speech_started = False
    loud_chunks = 0
    silent_chunks = 0
    speech_chunks = 0
    silence_target = max(3, math.ceil(silence_ms / 40))
    wait_deadline = time.monotonic() + wait_seconds
    speech_deadline = float("inf")

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    process.stdout.readexactly(MIC_CHUNK_BYTES), timeout=2
                )
            except asyncio.IncompleteReadError as exc:
                chunk = exc.partial
                if not chunk:
                    raise RuntimeError("Mikrofon ses akışı beklenmedik şekilde kapandı")

            level = pcm_rms(chunk)
            if not speech_started:
                pre_roll.append(chunk)
                loud_chunks = loud_chunks + 1 if level >= threshold else 0
                if loud_chunks >= 3:
                    speech_started = True
                    speech_deadline = time.monotonic() + max_speech_seconds
                    recording.extend(b"".join(pre_roll))
                    speech_chunks = len(pre_roll)
                    print(f"Konuşma algılandı (ses seviyesi={level}).")
                elif time.monotonic() >= wait_deadline:
                    raise RuntimeError(
                        "Konuşma algılanmadı; mikrofonu veya --vad-threshold değerini kontrol edin"
                    )
                continue

            recording.extend(chunk)
            speech_chunks += 1
            silent_chunks = silent_chunks + 1 if level < threshold else 0
            if silent_chunks >= silence_target and speech_chunks >= 10:
                print("Cümle sonu algılandı.")
                break
            if time.monotonic() >= speech_deadline:
                print("Azami konuşma süresine ulaşıldı; kayıt gönderiliyor.")
                break
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()

    audio = bytes(recording)
    print(f"Kayıt tamamlandı ({len(audio) / 32000:.1f} saniye).")
    return audio


async def send_recording(
    websocket: ClientConnection, recording: bytes, send_speed: float
) -> None:
    chunks = [
        recording[index : index + MIC_CHUNK_BYTES]
        for index in range(0, len(recording), MIC_CHUNK_BYTES)
    ]
    await websocket.send(json.dumps({"realtimeInput": {"activityStart": {}}}))
    next_send = time.monotonic()
    for chunk in chunks:
        message = json.dumps(
            {
                "realtimeInput": {
                    "audio": {
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64.b64encode(chunk).decode(),
                    }
                }
            }
        )
        await websocket.send(message)
        next_send += 0.04 / send_speed
        await asyncio.sleep(max(0, next_send - time.monotonic()))

    await websocket.send(json.dumps({"realtimeInput": {"activityEnd": {}}}))


async def receive_response(
    assistant_id: int,
    websocket: ClientConnection,
    timeout: float,
    mixer: Optional[SharedAudioMixer],
) -> AssistantResponse:
    result = AssistantResponse(assistant_id=assistant_id)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            message = json.loads(raw)
            server_content = message.get("serverContent", {})
            transcription = server_content.get("outputTranscription", {}).get("text")
            if transcription:
                result.transcript.append(transcription)

            for part in server_content.get("modelTurn", {}).get("parts", []):
                inline_data = part.get("inlineData", {})
                if inline_data.get("data"):
                    audio = base64.b64decode(inline_data["data"])
                    result.audio.extend(audio)
                    if mixer is not None:
                        mixer.submit(assistant_id, audio)

            if server_content.get("turnComplete"):
                return result
        result.error = "response timeout"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


async def monitor_proxy(pid: Optional[int], stop: asyncio.Event) -> None:
    if pid is None:
        print("Proxy PID bulunamadı; CPU/RAM ölçümü gösterilmeyecek.")
        return
    previous = process_sample(pid)
    previous_time = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
            break
        except TimeoutError:
            current_time = time.monotonic()
            current = process_sample(pid)
            cpu = 100 * (current.cpu_seconds - previous.cpu_seconds) / (
                current_time - previous_time
            )
            print(
                f"proxy_cpu={cpu:.1f}% "
                f"proxy_rss={current.rss_bytes / 1024 / 1024:.1f} MiB"
            )
            previous = current
            previous_time = current_time


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--silence-ms", type=int, default=900)
    parser.add_argument("--vad-threshold", type=int, default=400)
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument("--max-speech-seconds", type=float, default=15)
    parser.add_argument(
        "--send-speed",
        type=float,
        default=4,
        help="Recorded audio upload speed; 4 means four times real time",
    )
    parser.add_argument("--response-timeout", type=float, default=30)
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get(
            "PROXY_URL", "wss://realtime.orbitkidslab.com/ws/live"
        ),
    )
    parser.add_argument(
        "--no-play",
        action="store_true",
        help="Print transcriptions without playing response audio",
    )
    args = parser.parse_args()
    if (
        args.clients <= 0
        or args.silence_ms <= 0
        or args.vad_threshold <= 0
        or args.wait_seconds <= 0
        or args.max_speech_seconds <= 0
        or args.send_speed <= 0
    ):
        parser.error("numeric options must be positive")

    token = os.environ.get("ORBIT_USER_TOKEN")
    if not token:
        parser.error("ORBIT_USER_TOKEN is missing from .orbit-token.env")

    stop_monitor = asyncio.Event()
    monitor = asyncio.create_task(monitor_proxy(find_proxy_pid(), stop_monitor))
    mixer = None if args.no_play else SharedAudioMixer()
    responses: List[AssistantResponse] = []

    try:
        async with AsyncExitStack() as stack:
            print(f"{args.clients} Gemini Live bağlantısı açılıyor...")
            connections = []
            for client_id in range(args.clients):
                try:
                    websocket = await stack.enter_async_context(
                        connect(
                            args.proxy_url,
                            additional_headers={"Authorization": f"Bearer {token}"},
                            open_timeout=20,
                            ping_timeout=60,
                            max_size=16 * 1024 * 1024,
                            compression=None,
                        )
                    )
                    connections.append((client_id, websocket))
                except Exception as exc:
                    responses.append(
                        AssistantResponse(
                            assistant_id=client_id,
                            error=f"connection: {type(exc).__name__}: {exc}",
                        )
                    )

            setup_send_results = await asyncio.gather(
                *(websocket.send(setup_message()) for _, websocket in connections),
                return_exceptions=True,
            )
            setup_connections = []
            for connection, send_result in zip(connections, setup_send_results):
                if isinstance(send_result, BaseException):
                    responses.append(
                        AssistantResponse(
                            assistant_id=connection[0],
                            error=(
                                "setup send: "
                                f"{type(send_result).__name__}: {send_result}"
                            ),
                        )
                    )
                else:
                    setup_connections.append(connection)

            setup_results = await asyncio.gather(
                *(
                    asyncio.wait_for(websocket.recv(), timeout=20)
                    for _, websocket in setup_connections
                ),
                return_exceptions=True,
            )
            ready = []
            for connection, setup_result in zip(setup_connections, setup_results):
                setup_valid = False
                if not isinstance(setup_result, BaseException):
                    try:
                        setup_valid = "setupComplete" in json.loads(setup_result)
                    except (TypeError, json.JSONDecodeError):
                        setup_valid = False
                if not setup_valid:
                    responses.append(
                        AssistantResponse(
                            assistant_id=connection[0],
                            error=f"setup failed: {setup_result}",
                        )
                    )
                else:
                    ready.append(connection)
            print(f"Hazır asistan: {len(ready)}/{args.clients}")
            if not ready:
                raise RuntimeError("Hiçbir Gemini Live oturumu kurulamadı")

            recording = await record_one_sentence(
                threshold=args.vad_threshold,
                silence_ms=args.silence_ms,
                wait_seconds=args.wait_seconds,
                max_speech_seconds=args.max_speech_seconds,
            )
            receivers = {
                client_id: asyncio.create_task(
                    receive_response(
                        client_id,
                        websocket,
                        args.response_timeout,
                        mixer=mixer,
                    )
                )
                for client_id, websocket in ready
            }
            print("Aynı konuşma tüm asistanlara gönderiliyor...")
            send_results = await asyncio.gather(
                *(
                    send_recording(websocket, recording, args.send_speed)
                    for _, websocket in ready
                ),
                return_exceptions=True,
            )
            failed_sends: Set[int] = set()
            for (client_id, _), send_result in zip(ready, send_results):
                if isinstance(send_result, BaseException):
                    failed_sends.add(client_id)
                    receiver = receivers[client_id]
                    receiver.cancel()
                    responses.append(
                        AssistantResponse(
                            assistant_id=client_id,
                            error=(
                                "audio send: "
                                f"{type(send_result).__name__}: {send_result}"
                            ),
                        )
                    )

            await asyncio.gather(
                *(receivers[client_id] for client_id in failed_sends),
                return_exceptions=True,
            )
            print("Cevaplar bekleniyor...")
            received = await asyncio.gather(
                *(
                    receiver
                    for client_id, receiver in receivers.items()
                    if client_id not in failed_sends
                )
            )
            responses.extend(received)
    finally:
        stop_monitor.set()
        await asyncio.gather(monitor, return_exceptions=True)
        if mixer is not None:
            await mixer.close()

    responses.sort(key=lambda item: item.assistant_id)
    successful = 0
    for response in responses:
        transcript = "".join(response.transcript).strip()
        if response.error:
            print(f"Asistan {response.assistant_id + 1}: HATA — {response.error}")
        else:
            successful += 1
            print(
                f"Asistan {response.assistant_id + 1}: "
                f"{transcript or '[transkripsiyon yok]'}"
            )

    print(f"Başarılı cevap: {successful}/{args.clients}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDurduruldu")
