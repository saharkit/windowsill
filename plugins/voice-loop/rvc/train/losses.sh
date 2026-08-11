#!/bin/bash
# Print the scalar loss curves Applio writes to TensorBoard for "scheherazade".
$HOME/voice/rvc/venv/bin/python - <<"PY"
import glob, os
from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
d = os.path.expanduser("~/voice/rvc/Applio/logs/scheherazade")
files = sorted(glob.glob(os.path.join(d, "events.out.tfevents.*")))
if not files:
    files = sorted(glob.glob(os.path.join(d, "eval", "events.out.tfevents.*")))
ea = EventAccumulator(files[-1], size_guidance={"scalars": 100000})
ea.Reload()
tags = ea.Tags()["scalars"]
print("event file:", files[-1])
print("tags:", tags)
for t in tags:
    ev = ea.Scalars(t)
    if not ev:
        continue
    pts = [(e.step, round(e.value, 4)) for e in ev]
    step = max(1, len(pts) // 8)
    print(f"{t}: n={len(pts)} first={pts[0]} last={pts[-1]} sample={pts[::step][:9]}")
PY
