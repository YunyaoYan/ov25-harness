"""The smallest offline subtask: say the question back, with the number of frames it came with.

    [subtask]
    factory = "echo_subtask:factory"

    PYTHONPATH=examples ov25-harness serve --config my.toml

Open a live session with "offline_subtask": true and ask a question about the scene, or open an
offline session and upload a recording; the answer row has task "offline_echo".
"""
from ov25_harness.subtask import OfflineRequest, OfflineResult


class EchoSubtask:
    async def run(self, request: OfflineRequest) -> OfflineResult:
        if request.input_mode == "offline":
            frames = len(request.recording.timestamps)
        else:
            frames = len(request.frames)
        return OfflineResult(f"You asked: {request.question} ({frames} frames)", "ok", "offline_echo",
                             {"echo_frames": frames})


def factory():
    return EchoSubtask()
