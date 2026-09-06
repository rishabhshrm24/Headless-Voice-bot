"""
Continuous (live) voice call against the /ws/voice realtime bridge.

Captures mic audio, streams it to the bot as PCM16 24kHz base64 chunks, and plays
back the bot's spoken replies as they arrive - the whole call is one open
WebSocket, no reconnects between turns (server VAD detects end-of-speech).

Requires: pip install sounddevice websockets

Usage:
    python scripts/live_voice_client.py
    python scripts/live_voice_client.py --url ws://localhost:8000/ws/voice
"""
import argparse
import asyncio
import base64
import json
import queue

import sounddevice as sd
import websockets

SAMPLE_RATE = 24000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 100  # size of each mic chunk sent upstream
BLOCK_SIZE = SAMPLE_RATE * BLOCK_MS // 1000

DEFAULT_URL = "wss://headless-voicebot-poc.onrender.com/ws/voice"


async def run_call(url: str) -> None:
    mic_queue: "queue.Queue[bytes]" = queue.Queue()
    playback_queue: "queue.Queue[bytes]" = queue.Queue()

    def mic_callback(indata, frames, time_info, status):
        if status:
            print("mic status:", status)
        mic_queue.put(bytes(indata))

    def playback_callback(outdata, frames, time_info, status):
        needed = frames * 2  # int16 = 2 bytes/sample
        chunk = bytearray()
        while len(chunk) < needed:
            try:
                chunk += playback_queue.get_nowait()
            except queue.Empty:
                break
        chunk = chunk[:needed].ljust(needed, b"\x00")
        outdata[:] = bytes(chunk)

    print(f"Connecting to {url} ... (first request may take 30-60s if server is cold)")
    async with websockets.connect(url, open_timeout=60) as ws:
        print("Connected. Speak into your mic. Ctrl+C to end the call.")

        async def sender():
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
                blocksize=BLOCK_SIZE, callback=mic_callback,
            ):
                while True:
                    chunk = await asyncio.to_thread(mic_queue.get)
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode(),
                    }))

        async def receiver():
            with sd.RawOutputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
                blocksize=BLOCK_SIZE, callback=playback_callback,
            ):
                async for raw in ws:
                    event = json.loads(raw)
                    etype = event.get("type")
                    if etype == "response.output_audio.delta":
                        playback_queue.put(base64.b64decode(event["audio"]))
                    elif etype == "response.output_audio_transcript.done":
                        print("Bot:", event.get("transcript", ""))
                    elif etype == "conversation.item.input_audio_transcription.completed":
                        print("You:", event.get("transcript", ""))
                    elif etype == "error":
                        print("Error:", event.get("error"))

        await asyncio.gather(sender(), receiver())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL, help="ws:// or wss:// endpoint for /ws/voice")
    args = parser.parse_args()

    try:
        asyncio.run(run_call(args.url))
    except KeyboardInterrupt:
        print("\nCall ended.")


if __name__ == "__main__":
    main()
