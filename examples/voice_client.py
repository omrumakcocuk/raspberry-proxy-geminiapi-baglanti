"""Full-duplex microphone client for the local Gemini Live proxy."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import unicodedata
from contextlib import suppress
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed


load_dotenv(".orbit-token.env", override=True)

PROXY_URL = os.environ.get(
    "PROXY_URL", "ws://server.orbitkidslab.com:8001/ws/live"
)
MODEL = "models/gemini-3.1-flash-live-preview"
MIC_CHUNK_BYTES = 1280  # 40 ms of mono, signed 16-bit, 16 kHz PCM.
AUDIO_OUTPUT_DEVICE = os.environ.get("AUDIO_OUTPUT_DEVICE", "pipewire")
TURKISH_INSTRUCTION = (
    "Bu oturumun tek dili Türkçedir. Kullanıcının bütün ses girdilerini yalnızca "
    "Türkçe konuşma olarak dinle, Türkçe kelimeler olarak çözümle ve Türkçe yorumla. "
    "Benzer sesli yabancı kelimeler tahmin etme. Kullanıcı başka dil istese bile "
    "dil değiştirme. Her zaman yalnızca doğal, akıcı Türkçe konuş. Yanıtlarını kısa, "
    "net ve günlük konuşma dilinde ver."
)


def setup_message() -> str:
    return json.dumps(
        {
            "setup": {
                "model": MODEL,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "languageCode": "tr-TR",
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": "Aoede"}
                        }
                    },
                },
                "systemInstruction": {"parts": [{"text": TURKISH_INSTRUCTION}]},
                "realtimeInputConfig": {
                    "automaticActivityDetection": {
                        "disabled": False,
                        "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                        "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                        "prefixPaddingMs": 100,
                        "silenceDurationMs": 700,
                    }
                },
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
            }
        }
    )


async def start_microphone() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
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
        "--buffer-time=100000",
        "--period-time=20000",
        stdout=asyncio.subprocess.PIPE,
    )


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
    if process.returncode is None:
        await process.wait()


class LiveAudioPlayer:
    def __init__(self) -> None:
        self.process: Optional[asyncio.subprocess.Process] = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            "aplay",
            "-D",
            AUDIO_OUTPUT_DEVICE,
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
        await asyncio.sleep(0.01)
        if self.process.returncode is not None:
            code = self.process.returncode
            self.process = None
            raise RuntimeError(
                f"Ses cihazı açılamadı: {AUDIO_OUTPUT_DEVICE!r}, aplay code={code}"
            )

    async def write(self, audio: bytes) -> None:
        if self.process is None or self.process.returncode is not None:
            await self.start()
        assert self.process is not None
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(audio)
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError(
                f"Ses cihazına yazılamadı: {AUDIO_OUTPUT_DEVICE!r}"
            ) from exc

    async def interrupt(self) -> None:
        """Drop queued output immediately when the user interrupts Gemini."""
        if self.process is not None:
            await stop_process(self.process)
            self.process = None

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await self.process.stdin.wait_closed()
        await self.process.wait()
        self.process = None


async def send_microphone(
    websocket: ClientConnection,
    microphone: asyncio.subprocess.Process,
    microphone_enabled: asyncio.Event,
) -> None:
    assert microphone.stdout is not None
    while chunk := await microphone.stdout.read(MIC_CHUNK_BYTES):
        if not microphone_enabled.is_set():
            continue
        await websocket.send(
            json.dumps(
                {
                    "realtimeInput": {
                        "audio": {
                            "data": base64.b64encode(chunk).decode("ascii"),
                            "mimeType": "audio/pcm;rate=16000",
                        }
                    }
                }
            )
        )

    await websocket.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))


def transcription(message: Dict[str, Any], field: str) -> Optional[str]:
    return message.get("serverContent", {}).get(field, {}).get("text")


def uses_latin_script(text: str) -> bool:
    """Hide clearly misdetected Korean/Cyrillic/etc. input transcription."""
    return all(
        not character.isalpha()
        or "LATIN" in unicodedata.name(character, "")
        for character in text
    )


async def receive_gemini(
    websocket: ClientConnection,
    player: LiveAudioPlayer,
    microphone_enabled: asyncio.Event,
) -> None:
    output_started = False
    audio_started = False
    async for raw_message in websocket:
        message = json.loads(raw_message)
        server_content = message.get("serverContent", {})

        user_text = transcription(message, "inputTranscription")
        if user_text and uses_latin_script(user_text):
            print(f"\rSen: {user_text}", end="", flush=True)

        gemini_text = transcription(message, "outputTranscription")
        if gemini_text:
            if not output_started:
                print("\nGemini: ", end="", flush=True)
                output_started = True
            print(gemini_text, end="", flush=True)

        for part in server_content.get("modelTurn", {}).get("parts", []):
            inline_data = part.get("inlineData")
            if inline_data and inline_data.get("data"):
                # Prevent Gemini's speaker output from returning through the mic
                # and being mistaken for a user interruption.
                microphone_enabled.clear()
                if not audio_started:
                    print(
                        f"\n[Ses çıkışı başladı: {AUDIO_OUTPUT_DEVICE}]",
                        flush=True,
                    )
                    audio_started = True
                await player.write(base64.b64decode(inline_data["data"]))

        if server_content.get("interrupted"):
            await player.interrupt()
            microphone_enabled.set()
            output_started = False
            print("\n[Gemini kesildi; seni dinliyor]")
        elif server_content.get("turnComplete"):
            microphone_enabled.set()
            output_started = False
            audio_started = False
            print("\n[Dinleniyor]")


async def main() -> None:
    microphone: Optional[asyncio.subprocess.Process] = None
    player = LiveAudioPlayer()

    orbit_token = os.environ.get("ORBIT_USER_TOKEN")
    if not orbit_token:
        raise SystemExit("ORBIT_USER_TOKEN ortam değişkeni gerekli")
    async with connect(
        PROXY_URL,
        max_size=16 * 1024 * 1024,
        additional_headers={"Authorization": f"Bearer {orbit_token}"},
    ) as websocket:
        await websocket.send(setup_message())

        setup_response = json.loads(await websocket.recv())
        if "setupComplete" not in setup_response:
            raise RuntimeError(f"Gemini setup failed: {setup_response}")

        print("Bağlandı. Konuşabilirsin; çıkmak için klavyeden Ctrl ve C'ye bas.")
        print("[Dinleniyor]")
        microphone = await start_microphone()
        microphone_enabled = asyncio.Event()
        microphone_enabled.set()

        sender = asyncio.create_task(
            send_microphone(websocket, microphone, microphone_enabled)
        )
        receiver = asyncio.create_task(
            receive_gemini(websocket, player, microphone_enabled)
        )
        try:
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            if microphone is not None:
                await stop_process(microphone)
            await player.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBağlantı kapatıldı.")
    except ConnectionClosed as exc:
        print(f"\nBağlantı kapandı: code={exc.code} reason={exc.reason!r}")
