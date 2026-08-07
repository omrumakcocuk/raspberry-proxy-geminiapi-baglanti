"""Minimal text client for manually testing the local proxy."""

import asyncio
import base64
import json
import os
from contextlib import suppress

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


load_dotenv(".orbit-token.env", override=True)


async def start_audio_player() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
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


async def stop_audio_player(player: asyncio.subprocess.Process) -> None:
    if player.stdin is not None:
        player.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await player.stdin.wait_closed()
    await player.wait()


async def main() -> None:
    orbit_token = os.environ.get("ORBIT_USER_TOKEN")
    if not orbit_token:
        raise SystemExit("ORBIT_USER_TOKEN ortam değişkeni gerekli")
    async with connect(
        os.environ.get(
            "PROXY_URL", "ws://server.orbitkidslab.com:8001/ws/live"
        ),
        additional_headers={"Authorization": f"Bearer {orbit_token}"},
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "setup": {
                        "model": "models/gemini-3.1-flash-live-preview",
                        "generationConfig": {"responseModalities": ["AUDIO"]},
                        "outputAudioTranscription": {},
                    }
                }
            )
        )

        setup_response = json.loads(await websocket.recv())
        print("setup:", setup_response)

        await websocket.send(
            json.dumps(
                {
                    "realtimeInput": {
                        "text": "Merhaba! Türkçe ve tek cümleyle cevap ver."
                    }
                }
            )
        )

        audio_chunks = 0
        printed_label = False
        player = await start_audio_player()
        try:
            async for response in websocket:
                message = json.loads(response)
                server_content = message.get("serverContent", {})
                transcription = server_content.get("outputTranscription", {}).get(
                    "text"
                )
                if transcription:
                    if not printed_label:
                        print("Gemini: ", end="", flush=True)
                        printed_label = True
                    print(transcription, end="", flush=True)

                model_turn = server_content.get("modelTurn", {})
                for part in model_turn.get("parts", []):
                    inline_data = part.get("inlineData")
                    if inline_data and inline_data.get("data"):
                        audio_chunks += 1
                        if player.stdin is not None:
                            player.stdin.write(base64.b64decode(inline_data["data"]))
                            await player.stdin.drain()

                if server_content.get("turnComplete"):
                    print(f"\nTamamlandı (ses parçası: {audio_chunks})")
                    break
        except ConnectionClosed as exc:
            print(f"Gemini bağlantısı kapandı: code={exc.code} reason={exc.reason!r}")
        finally:
            await stop_audio_player(player)


if __name__ == "__main__":
    asyncio.run(main())
