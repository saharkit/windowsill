# rvc/ — RVC voice-conversion operator tooling

## What this is

**Operator tooling, not part of the plugin's runtime.** The voice-loop plugin needs only a URL —
`VOICE_LOOP_RVC_URL` points at a running conversion service. This directory is how you *produce* the
thing that URL points at: the pipeline that trains an RVC voice-conversion model from a speaker corpus,
and the micro-service that serves the trained model over HTTP.

Nothing here is imported by the plugin, run by a hook, or exercised in CI. Everything here runs on a
GPU machine, manually, by an operator following the runbooks.

## How it fits the voice contour

```
VOICE_LOOP_CORPUS_DIR   accumulates audio of one speaker (the recolor stage records here)
        │
        ▼
rvc/train/*.sh          the training pipeline: preprocess → extract → train → finish → smoke
        │
        ▼
rvc/serve/rvc_server.py serves the trained model as an HTTP conversion service (loopback, :8358)
        │
        ▼
VOICE_LOOP_RVC_URL      the plugin's recolor stage POSTs synthesized audio here for voice conversion
```

The pipeline is idempotent: the corpus is read-only, the dataset is regenerable, and only the trained
`.pth` weights and `.index` are the output you keep.

## Hardware reality

**You need a GPU.** The training pipeline runs on CUDA and will not start without it. The serving step
can fall back to CPU, but at ~30–90× slower latency — the GPU path is the whole point.

**You need roughly 30+ minutes of clean, single-speaker corpus audio.** The shipped example configs
were trained on **37 minutes** of one female voice (40 mp3 files). Less audio works but produces a
weaker model; 15–20 minutes is a realistic floor.

The example run consumed ~3.4 GiB of VRAM during training and took **~5 hours** for 70 epochs at batch
size 2 on a 6 GiB card. Your numbers will differ — the runbooks walk through the measurement commands
so you can profile your own card before committing a night to it.

## Layout

```
rvc/
├── train/                  the training pipeline (12 bash scripts)
│   ├── install.sh          pip-installs torch and Applio deps into an already-present venv
│   ├── prereq.sh           downloads pretrained weights
│   ├── preprocess.sh       slices the corpus into training audio
│   ├── extract.sh          extracts pitch and content features
│   ├── train.sh            LAUNCHER — the long-running training job
│   ├── train-status.sh     RUNNING / FINISHED / DIED — one-line poll
│   ├── stop-train.sh       stops the training job, reports VRAM freed
│   ├── finish.sh           watcher — builds the faiss index when training exits
│   ├── losses.sh           dumps TensorBoard loss curves as text
│   ├── smoke.sh            manual inference smoke — converts one clip
│   ├── auto-smoke.sh       unattended smoke — runs once when training finishes
│   └── vram-sampler.sh     logs VRAM at 5-second intervals for profiling
├── serve/
│   └── rvc_server.py       the conversion micro-service (FastAPI, :8358 by convention)
├── config/
│   ├── training-config.example.json   hyper-parameters that produced a working voice
│   └── model-info.example.json        corpus metadata (speaker name, language, description)
└── ../docs/                (runbooks live at the plugin level, not under rvc/)
    ├── rvc-training.md     RUNBOOK — the training pipeline, end to end
    └── rvc-serving.md      RUNBOOK — the conversion service, start to operate
└── README.md               this file
```

## Runbooks

- **[`docs/rvc-training.md`](../docs/rvc-training.md)** — the training runbook: the stack, the launch
  command, how to poll progress, how to stop/resume, troubleshooting (CUDA OOM, the completion-detection
  trap, the silent `assets/config.json` bug), the morning pipeline, and the demo.
- **[`docs/rvc-serving.md`](../docs/rvc-serving.md)** — the serving runbook: start/stop, the API
  (`/health`, `POST /convert`), the VRAM admission gate, measured latency, the CPU overflow lane, and
  known v0 gaps.

## Example configs

The two files in `config/` are a **record of what worked** on one specific run — not a required
setting. They are useful as a starting point: copy them, adjust `batch_size` and `total_epoch` for
your card, and keep your own training log alongside them. The hyper-parameters that matter most for
VRAM (batch size, checkpointing, sample rate) are explained in the training runbook's troubleshooting
section.

## License

The training pipeline uses [Applio](https://github.com/IAHispano/Applio) (MIT). The trained model
carries the license of the corpus it was trained on, not of the tool — treat it accordingly.
