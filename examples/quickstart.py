"""Open a live session, send a picture as the camera, ask about it, print the answer, close.

    python examples/quickstart.py --url http://127.0.0.1:8100 --image tests/data/frame.jpg

Needs only httpx. The picture is sent as one frame a second, the rate the models work at; frames
keep going while the answer is awaited, because a live session without frames is closed.
"""
import argparse
import asyncio
from pathlib import Path
import sys
import time

import httpx


async def push(client, frame_url, image, start):
    """One frame a second, each stamped with its capture time in session seconds."""
    t = 0
    media_type = "image/png" if image.startswith(b"\x89PNG") else "image/jpeg"
    while True:
        await asyncio.sleep(max(0., start + t - time.monotonic()))
        reply = (await client.post(frame_url, params={"t": t}, content=image,
                                   headers={"Content-Type": media_type})).json()
        print(f"frame t={t} accepted={reply.get('accepted')} health={reply.get('health', {}).get('status')}")
        t += 1


async def main(url, image, question, timeout):
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=70, trust_env=False) as client:
        response = await client.post("/v1/sessions", json={"input_mode": "live", "mode": "qa", "fps": 1.0})
        response.raise_for_status()
        session = response.json()
        root = f"/v1/sessions/{session['session_id']}"
        print(f"session {session['session_id']}")
        start = time.monotonic()
        pusher = asyncio.create_task(push(client, session["frame_url"], image, start))
        try:
            await asyncio.sleep(3.5)   # frames at t = 0, 1, 2, 3
            asked_at = round(time.monotonic() - start, 2)
            response = await client.post(f"{root}/text", json={"text": question, "t": asked_at,
                                                              "time_scope": "current"})
            response.raise_for_status()
            print(f"asked at t={asked_at}: {question}")
            cursor, deadline = 0, time.monotonic() + timeout
            while time.monotonic() < deadline:
                response = await client.get(f"{root}/results", params={"after": cursor, "wait": 10})
                response.raise_for_status()
                for row in response.json()["results"]:
                    cursor = max(cursor, row["seq"])
                    if row["type"] == "answer":
                        print(f"answer [{row.get('status')}, route {row.get('route')}, "
                              f"frames {row.get('source_window_s')}]: {row.get('text')}")
                        return 0
                    print(f"{row['type']}: {row.get('text')}")
            print("no answer in time", file=sys.stderr)
            return 1
        finally:
            pusher.cancel()
            await asyncio.gather(pusher, return_exceptions=True)
            closed = await client.delete(root)
            print(f"closed: {closed.json()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8100", help="the harness's base URL")
    parser.add_argument("--image", type=Path, required=True, help="a JPEG or PNG picture")
    parser.add_argument("--question", default="What is in front of the camera now?")
    parser.add_argument("--timeout", type=float, default=30., help="seconds to wait for the answer")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.url, args.image.read_bytes(), args.question, args.timeout)))
