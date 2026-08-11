# RUNBOOK — RVC voice-conversion training ("scheherazade")

> **Placeholders:** `<gpu-host>` is the training machine, `<user>` is the operator account on it, and `<path>` is a local directory path on either machine. Replace them with your own values.

**Owner:** Sahar (SRE role) · **Node:** `<gpu-host>` (user `<user>`, no sudo)
**Last verified:** 2026-08-02 (stood up and run on this date)
**Node clock is UTC.** All timestamps below are UTC; convert to your local time as needed.

> ## STATUS 2026-08-02 06:30 UTC — **COMPLETE. All 70 epochs trained; model works; demo rendered.**
>
> Attempts 1–3 died of CUDA OOM at their first checkpoint save (§10). Attempt 4 applied **§11 option (b)**
> and ran clean: **70/70 epochs, 32 270 steps, 01:18:42 → 06:08:13 UTC (4h50m)**, six weights files, seven
> resume checkpoints, index present, inference verified end-to-end.
>
> **Use this model:** `logs/scheherazade/scheherazade_70e_32270s.pth` + `logs/scheherazade/scheherazade.index`.
> Alternatives at 20/30/40/50/60 epochs are on disk. Best `lowest_value` was **5.838 at epoch 51**, flat
> from there to 69 — but note §11(b) made that metric a *training* sample, not held-out, so it is a
> plateau signal, not a ranking. Compare 50/60/70 by ear.
>
> **It did NOT "die at 69/70", and nothing was lost.** A completed run prints its final lines into a
> block-buffered stdout and then calls `os._exit()`, which does not flush — so the last epoch line and the
> success marker are simply **discarded**, while every file lands on disk. My own `train-status.sh` and
> `finish.sh` keyed on that marker and cried DIED/NO_INDEX over a healthy run. `train-status.sh` is fixed
> (§5); it now reports FINISHED off the on-disk final weights.
>
> **Two real bugs were found and fixed on the way** — both documented: the missing `assets/config.json`
> that silently suppressed weights extraction (§10), and `pedalboard` SIGILL-ing on this AVX2-less CPU,
> which made inference impossible until shimmed (§8). **Training is NOT relaunched**; a 1-epoch delta is
> not worth a night.

---

## 0. What this is, and what it is NOT

A **training** workload, not a service. It has no port, no health endpoint, and nothing depends on it being
up. It produces files: an RVC voice-conversion model for the ElevenLabs-cloned female voice in
`~/voice/corpus/eleven/`.

It is **additive**. Everything it created lives under `~/voice/rvc/`. Nothing outside that tree was
modified — with exactly one deliberate, operator-authorized exception recorded in §9: the **xtts server
(8356) was stopped** to free the VRAM this run needs.

| | LIVE — do not touch | xtts (8356) | THIS run |
|---|---|---|---|
| what | `~/voice/voice_server.py`, port 8355 | `~/voice/xtts/xtts_server.py` | `~/voice/rvc/` (batch job) |
| venv | `~/voice/venv` | `~/voice/xtts/venv` | `~/voice/rvc/venv` |
| VRAM | ~1.82 GiB, always resident | ~2.2 GiB (STOPPED for the night) | ~3.36 GiB while training |
| state | the working voice contour | experiment | experiment |

**The 8355 server always wins.** If the GPU is contended, this run is the thing that gets killed (§6a),
never the live contour.

---

## 1. The stack, and why each piece is pinned

| piece | value | why |
|---|---|---|
| tool | **Applio 3.6.4**, commit `da174445ec98d15d006175ac99db3879bd3a73d1` | maintained RVC fork with a real headless CLI (`core.py preprocess/extract/train/index/infer`); **no fairseq**, so it installs on Python 3.12; its own installer targets `--python 3.12` and the `cu128` torch index, which is exactly this node |
| license | **MIT** (source + model weights) + Applio's Terms of Use | see §11 |
| python | 3.12.3 (`/usr/bin/python3.12`), venv at `~/voice/rvc/venv` | Applio's own `run-install.sh` uses 3.12; no 3.10 needed, none installed |
| torch | **2.11.0+cu128** / torchaudio **2.11.0+cu128** | the node's driver supports CUDA 12.8 (not 13.0), so the default PyPI torch (cu130) reports `cuda available: False` here. Installed from `--index-url https://download.pytorch.org/whl/cu128`. **This is the same trap XTTS hit** (`~/voice/xtts/RUNBOOK.md` §7.1) — root cause is the old driver, whose fix needs root. |
| pretrained | RVC **v2 HiFi-GAN 40k**, `f0G40k.pth` / `f0D40k.pth` | 40 kHz is the right size/quality trade for a 6 GiB card |
| f0 | **rmvpe** (`rvc/models/predictors/rmvpe.pt`), GPU | Applio's and RVC's recommended default |
| embedder | **contentvec** (`rvc/models/embedders/contentvec/`) | Applio default; loaded through `transformers`, not fairseq |

**Not needed here, unlike the XTTS install:** no FFmpeg. Applio reads audio through
`soundfile`/`libsndfile 1.2.2`, which decodes the corpus mp3s natively, and the training/inference paths
use only `torchaudio.functional.resample` — never `torchaudio.load` — so the torchcodec/`libav*.so`
dependency that forced XTTS onto torch 2.8.0 does not apply. Verified by grep before installing.

---

## 2. Layout

```
~/voice/rvc/
├── Applio/                       # the tool, git tag 3.6.4 (detached HEAD)
│   ├── core.py                   # THE CLI: preprocess / extract / train / index / infer
│   ├── rvc/models/               # pretraineds, predictors (rmvpe), embedders (contentvec) — 1.9 GB
│   └── logs/scheherazade/        # <<< the experiment: dataset, features, checkpoints, weights, index
├── venv/                         # python3.12 + torch 2.11.0+cu128
├── install.sh  preprocess.sh  extract.sh      # the setup steps, re-runnable
├── train.sh                      # LAUNCHER: train.sh [BATCH] [EPOCHS] [CLEANUP] [SAVE_EVERY]
├── stop-train.sh                 # stop the run (§6a)
├── train-status.sh               # RUNNING / FINISHED / DIED (§4)
├── finish.sh                     # watcher: builds the .index after training exits (§5)
├── auto-smoke.sh  smoke.sh       # unattended + manual inference smoke (§8)
├── losses.sh                     # dump the TensorBoard loss curves as text (§4)
├── setup.log preprocess.log extract.log train.log finish.log smoke.log
└── train-oom-batch4.log  train-probe-300ep.log     # kept as evidence, see §10
```

Everything under `logs/scheherazade/` is regenerable from the corpus; nothing here is precious except the
`.pth` weights and `.index` once you like a checkpoint.

---

## 3. The run

```sh
~/voice/rvc/train.sh 2 70 True 10 True    # batch=2, 70 epochs, cleanup, save every 10, CHECKPOINTING ON
```

which is:

```sh
cd ~/voice/rvc/Applio && PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
~/voice/rvc/venv/bin/python core.py train \
  --model_name scheherazade --vocoder HiFi-GAN --sample_rate 40000 \
  --batch_size 2 --total_epoch 70 --save_every_epoch 10 \
  --save_only_latest False --save_every_weights True \
  --gpu 0 --pretrained True --custom_pretrained False \
  --cache_data_in_gpu False --checkpointing True --cleanup True --index_algorithm Auto
```

**`--checkpointing True` is not optional on this card — it is the fix for an OOM that already killed one
run (§10).** It trades ~24 % speed for ~950 MiB of VRAM. `batch_size 2` is likewise the ceiling, not a
preference. Do not "optimize" either without re-reading §10.

Dataset it trains on (built by `preprocess.sh` + `extract.sh`, both re-runnable):

| | |
|---|---|
| source | `~/voice/corpus/eleven/` — 40 mp3, **37:01** of one female voice (read-only; never modified) |
| slices | **915** at 40 kHz + 915 at 16 kHz (`Automatic` cut, 48 Hz high-pass, no noise reduction, no normalization) |
| features | 915 contentvec `.npy` + 915 rmvpe f0 pairs; `filelist.txt` = 916 lines (915 slices + mutes) |
| steps/epoch | **461** at batch 2 · total for 70 epochs = **32 270** steps |

**Timing measured on this GPU**, all figures real:

| config | epoch time | VRAM resident | free | outcome |
|---|---|---|---|---|
| batch 2, `--checkpointing False` | **3:10** (epochs 3–7 all exactly 0:03:10, 2.46 it/s) | 3438 MiB | 471 MiB | OOM at epoch-15 save |
| batch 2, `--checkpointing True` | **3:55** (1.92 it/s) | 2492 MiB | 1419 MiB | OOM at epoch-10 save |
| batch 2, `--checkpointing True` **+ §11(b)** | **3:56** | 2502 MiB | 1409 MiB | **survived the epoch-10 save — this is the live config** |

**The 6 h budget was never the binding constraint; VRAM at save time was.** Current run: started
**01:18:42**, 70 epochs → **ETA ~05:55 UTC** (08:55 local), ~4h36m, plus 1–3 min for the index.

Loss evidence from the checkpointed run before it died (it *was* learning — this is not a
"training didn't work" failure): `lowest_value` 15.952 → 14.4 → 13.699 → 13.139 → **12.954** over epochs
2–7, and over the first run's 3300 steps `loss_avg_50/g/total` 35.22 → 25.99, `g/mel` 20.89 → 15.39,
`g/kl` 2.39 → 1.23, with `d/adv` holding flat at ~4.2 (correct GAN equilibrium). **90 epochs is a budget decision, not a quality
ceiling** — it is what fits 6 h at the only batch size that fits this card. With a v2 pretrained base and
37 min of one clean speaker, checkpoints at 45/60/75/90 are all plausibly usable; compare them (§8) rather
than assuming the last is best. To go further, resume (§6b) with a larger `--total_epoch`.

---

## 4. Checking progress

**The one command to poll** (also answers finished/died — §5):

```sh
ssh <user>@<gpu-host> '~/voice/rvc/train-status.sh'
```

Healthy output looks like:

```
RUNNING | scheherazade | epoch=2 | step=922 | time=23:47:30 | training_speed=0:03:38 | lowest_value=15.677 (epoch 2 and step 637)
        | in-epoch: 87%|████████▋ | 399/461 [02:44<00:25, 2.44it/s] | vram_used/free: 5268 MiB, 471 MiB
```

Read it as: **epoch=** how far along, **training_speed=** how long that epoch took (watch for it growing),
**lowest_value=** best validation mel loss so far (should keep setting new lows early on), **2.44 it/s**
the throughput golden signal, **vram free** the saturation golden signal.

**Loss curves** (Applio writes losses only to TensorBoard, never to stdout):

```sh
~/voice/rvc/losses.sh          # prints every scalar tag with first/last/sampled values
```

A learning run looks like this — measured over the first 1300 steps of this run:

```
loss_avg_50/g/total : 35.22 -> 26.87
loss_avg_50/g/mel   : 20.89 -> 15.80
loss_avg_50/g/kl    :  2.39 ->  1.36
loss_avg_50/g/fm    :  8.51 ->  6.89
loss_avg_50/d/adv   :  4.17 ->  4.23   (flat is CORRECT — a GAN discriminator holding equilibrium)
```

`d/adv` collapsing toward 0 or `g/total` climbing steadily for many epochs is the failure shape. A single
noisy epoch is not.

**The other saturation signal: heat.** Measured in steady state — **83 °C, fan 95 %, 159 W / 170 W,
84 % GPU util**. That is inside spec for a 2060 (it throttles in the high 80s) but it is the top of the
envelope for a ~5 h run, and the symptom of throttling is not an error — it is `training_speed` in the
status line quietly growing past 3:10. Watch that number, not the temperature.

```sh
nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,power.draw,clocks_throttle_reasons.active --format=csv
```

Interactive TensorBoard, if wanted (bind is local; tunnel to reach it):

```sh
~/voice/rvc/venv/bin/tensorboard --logdir ~/voice/rvc/Applio/logs/scheherazade --port 6006
ssh -N -L 6006:127.0.0.1:6006 <user>@<gpu-host>
```

---

## 5. Completion detection — the poll recipe

`~/voice/rvc/train-status.sh` prints exactly one of **RUNNING / FINISHED / DIED** and nothing else on the
first token, so it is greppable:

```sh
ssh <user>@<gpu-host> '~/voice/rvc/train-status.sh'
```

| first token | means | evidence it uses |
|---|---|---|
| `RUNNING` | still training | `pgrep -f rvc/train/train.py` matches; also prints last epoch line + in-epoch % + VRAM |
| `FINISHED` | done cleanly | trainer gone **and** the on-disk final-epoch weights file exists (primary); falls back to the log marker if the target epoch cannot be determined; also prints the index path, the newest weights file, and the finisher's verdict |
| `DIED` | exited without completing | trainer gone, no completion marker; prints the last error / OOM lines |

If you would rather have the raw one-liner than the script:

```sh
pgrep -f 'rvc/train/train.py' >/dev/null && echo RUNNING \
 || { grep -aq 'Training has been successfully completed' ~/voice/rvc/train.log && echo FINISHED || echo DIED; }
```

**Do not judge success by the exit code — and do not judge it by the log marker either.** Both lie.

*The exit code lies in both directions.* `train.py` ends a completed run with `os._exit(2333333)` in the
training child (shell status 149 if it propagates), while `core.py`'s `run_train_script` treats any
non-zero rc as failure and prints `Training failed for model scheherazade.` Conversely — observed here —
when the batch-4 attempt died of OOM *inside the spawned child*, the parent still exited 0 and `core.py`
went on to build the index and report nothing wrong.

*The completion marker lies too, and this one cost us an hour of false alarm on 2026-08-02.*
`os._exit()` **does not flush stdio buffers.** `train.log` is a redirected file, so stdout is
block-buffered, so the final epoch's output — the `epoch=70` line, its three `Saved model` lines, and
`Training has been successfully completed with 70 epoch...` — was **discarded at exit**. The run was
perfect: `G_32270.pth`, `D_32270.pth` and `scheherazade_70e_32270s.pth` were all written at 06:08. But the
log's last epoch line is `epoch=69` and the marker never appears, so anything keying on it concludes the
run died one epoch short. `finish.sh` did exactly that and skipped the index build; `auto-smoke.sh` then
skipped the smoke.

**The reliable evidence is on disk, not in the log:** the final weights file
`scheherazade_<total_epoch>e_<total_epoch*461>s.pth`. `train-status.sh` now derives the epoch target from
its own `TRAIN_LAUNCH` line and checks for that file, falling back to the marker; it reports which of the
two it used.

**The `.index` is not evidence of training either — it already exists.** `scheherazade.index` (318 MB) was
built at **23:28:47**, by the failed batch-4 run, and it is **valid**: `extract_index.py` builds a faiss
index over the contentvec features in `extracted/`, which depend only on preprocess+extract, never on the
trained weights. So the index for this dataset is already final and would survive any number of failed
training attempts. (`--cleanup True` does not remove it — that only deletes `added*.index`.)

`~/voice/rvc/finish.sh` runs detached as a watcher anyway: when the trainer exits it checks for the
completion marker and rebuilds the index, logging `INDEX_BUILD` / `INDEX_DONE` / `NO_INDEX` to
`~/voice/rvc/finish.log`. That is belt-and-suspenders, plus the marker `auto-smoke.sh` waits on. If the
watcher is gone, build it by hand:

```sh
cd ~/voice/rvc/Applio && ~/voice/rvc/venv/bin/python core.py index --model_name scheherazade --index_algorithm Auto
```

---

## 6. Stop, resume, restart

### 6a. Stop (containment — use this whenever the live 8355 server is at risk, or in the morning)

```sh
~/voice/rvc/stop-train.sh          # prints TRAIN_STOPPED + the VRAM it returned
```

Blast radius: you lose only the epochs since the last save (at most 15 epochs ≈ 55 min). Everything up to
the last checkpoint is on disk.

> **Never `pkill -f` these patterns straight from an ssh command line.** `pkill -f` matches the *remote
> shell's own* command line, which contains the pattern you just typed, so the shell kills itself and the
> real target survives — this bit twice while standing this up (once leaving an orphaned trainer holding
> 3.4 GiB). The patterns live inside `stop-train.sh` for exactly this reason. Same trap applies to
> `pkill -f xtts_server.py`; use the bracket form `[x]tts_server.py` *and* keep it out of a line that also
> mentions the literal name.

### 6b. Resume

Applio resumes automatically from the newest `G_*.pth`/`D_*.pth` in the experiment directory. Relaunch
with **`CLEANUP=False`** (third arg) or it deletes the checkpoints you are trying to resume from, and keep
**`CHECKPOINTING=True`** (fifth arg) or it will OOM at the next save (§10):

```sh
nohup setsid ~/voice/rvc/train.sh 2 70 False 10 True >> ~/voice/rvc/train.log 2>&1 < /dev/null &
```

To train *further* than planned, pass a bigger epoch total — e.g. `~/voice/rvc/train.sh 2 120 False 10 True`.
Re-arm the finisher too if you want the index built automatically:
`nohup setsid ~/voice/rvc/finish.sh >> ~/voice/rvc/finish.log 2>&1 < /dev/null &`.

### 6c. Full rollback

`~/voice/rvc/stop-train.sh` then `rm -rf ~/voice/rvc`. Nothing outside that path was created or changed.
The corpus is untouched. (Restart xtts afterwards — §9.)

---

## 7. Where the checkpoints land

All in `~/voice/rvc/Applio/logs/scheherazade/`:

| file | what | cadence |
|---|---|---|
| `G_<step>.pth`, `D_<step>.pth` | full training state (generator + discriminator + optimizers) — **this is what resume reads** | every 10 epochs; `save_only_latest False` keeps them all |
| `scheherazade_<epoch>e_<step>s.pth` | the **inference weights** — this is what you pass to `--pth_path` | every 10 epochs (`save_every_weights True`); final = `scheherazade_70e_32270s.pth` |
| `scheherazade.index` | faiss retrieval index (`--index_path`), 318 MB — built from the **features**, not the weights, so it is already final (§5) | already present; `finish.sh` rebuilds it after training |
| `eval/events.out.tfevents.*` | TensorBoard scalars + generated audio samples | continuously |
| `sliced_audios/`, `sliced_audios_16k/`, `f0/`, `f0_voiced/`, `extracted/`, `filelist.txt` | the preprocessed dataset (458 MB) | built once |

Expect ~1.5 GB of checkpoints for the full run; the disk has 136 GB free.

---

## 8. Inference — converting a wav with this model

> ## READ FIRST: `PYTHONPATH=$HOME/voice/rvc/shims` is MANDATORY for inference on this node.
>
> Without it, `core.py infer` dies instantly with **`Illegal instruction (core dumped)`, rc=132, and an
> empty log** — no traceback, no message. Cause: this node is an **old 4-core CPU (no AVX2/FMA/F16C)**, and the `pedalboard` 0.9.24 wheel's native extension is built for AVX2,
> so it SIGILLs at *import*. `rvc/infer/infer.py:11` imports it at module level, so inference cannot even
> start. **Training is unaffected — it never imports pedalboard — which is why 70 epochs trained fine and
> the very first inference crashed.**
>
> `~/voice/rvc/shims/pedalboard.py` provides the eleven imported names. Pedalboard is only used by
> `post_process_audio()`, reached solely when `post_process=True` (CLI default `False`), so nothing real is
> lost; the shim's classes **raise** if instantiated, so enabling effects fails loudly rather than silently
> skipping them. Nothing in site-packages was modified — remove the shim dir / drop `PYTHONPATH` to revert.
> Diagnose a recurrence with `PYTHONFAULTHANDLER=1 python -u ...`, which prints the crashing import frame.
>
> **Verified working end-to-end on 2026-08-02** (see §14): 5.60 s of input converted in **5.27 s**, and
> `smoke.sh` passes. **Inference VRAM is only ~206 MiB** — far below the ~1.5 GiB assumed earlier, so it
> comfortably coexists with both servers, and could even run alongside training.

```sh
cd ~/voice/rvc/Applio
PYTHONPATH=$HOME/voice/rvc/shims \
~/voice/rvc/venv/bin/python core.py infer \
  --input_path  /absolute/path/to/input.wav \
  --output_path /absolute/path/to/converted.wav \
  --pth_path    ~/voice/rvc/Applio/logs/scheherazade/scheherazade_70e_32270s.pth \
  --index_path  ~/voice/rvc/Applio/logs/scheherazade/scheherazade.index \
  --f0_method rmvpe \
  --pitch 0 \
  --index_rate 0.75 \
  --volume_envelope 1 \
  --protect 0.33
```

- `--pitch` semitones: **0** for a female source (the target voice is female — this is what the demo used,
  with Silero's `baya`, also female). Use **+12** for a *male* source into this voice, not −12: higher
  values mean higher pitch, and male→female needs to go up.
- `--index_rate` 0–1: how much the faiss index pulls timbre toward the training voice. 0.75 is a good
  start; drop toward 0.3 if you hear artifacts.
- `--protect` 0–0.5: consonant/breath protection. 0.33 default; raise toward 0.5 if sibilants smear.
- Output format defaults to WAV (`--export_format WAV|MP3|FLAC|OGG|M4A`).
- Embedder defaults to `contentvec` — it **must** match what training used. Do not change it.

Wrapper that picks the newest weights + index automatically and prints duration/peak/RMS of both files:

```sh
~/voice/rvc/smoke.sh                                   # defaults: a corpus slice -> ~/voice/rvc/smoke_out.wav
~/voice/rvc/smoke.sh /path/in.wav /path/out.wav
```

**VRAM rule: do not run inference while training is running.** Training leaves ~470 MiB free; inference
needs ~1.5 GiB and will either OOM itself or OOM the training run. Stop training first (§6a).

`auto-smoke.sh` is armed and will run the smoke **once, unattended**, as soon as the index exists — i.e.
in the gap after training finishes and before xtts comes back. Read its result before doing anything else:

```sh
cat ~/voice/rvc/smoke.log        # expect: infer rc=0, then IN/OUT lines with dur≈equal and OUT rms > 0.005
```

A returned file with `dur < 0.1s` or `rms < 0.005` is **silence** — treat as a failure even though the CLI
said success (same rule as the XTTS smoke).

---

## 9. Morning pipeline

> **This is live again — a run is in flight, ETA ~05:55 UTC (08:55 local), and xtts is DOWN.**
> If you are up before ~05:55 the run is still going: either let it finish or stop it at a save point
> (§6a) — every 10 epochs from 01:58 there is a usable checkpoint, so stopping early costs at most ~39 min
> of training, not the night.

Run in this order. Steps 1–2 are the ones that matter; 3 is verification.

```sh
# 1. Stop training if it is still going (it should be done ~05:13 UTC / 08:13 local).
ssh <user>@<gpu-host> '~/voice/rvc/train-status.sh'
#    RUNNING  -> ssh ... '~/voice/rvc/stop-train.sh'      (checkpoints are safe; resume later per §6b)
#    FINISHED -> nothing to stop; check the index line is not MISSING
#    DIED     -> read ~/voice/rvc/train.log, then §10

# 2. Restart the xtts server — its runbook §4b, verbatim:
ssh <user>@<gpu-host> \
  'cd ~/voice/xtts && nohup setsid ~/voice/xtts/venv/bin/python ~/voice/xtts/xtts_server.py >> ~/voice/xtts/server.log 2>&1 < /dev/null &'
#    wait ~35 s, then:
ssh ... 'curl -s 127.0.0.1:8356/health'          # want {"ok":true,"device":"cuda",...}
```

**Order is load-bearing.** `xtts_server.py` refuses CUDA and silently starts on **CPU** if less than
**2600 MiB** is free at startup (its saturation guard, its RUNBOOK §7). Training holds ~3.4 GiB. Start
xtts while training is running and you get a working-but-3x-slower CPU server and a puzzling
`"device":"cpu"`. Stop training first; if you did it in the wrong order, just restart xtts (its §4c).

```sh
# 3. Inference smoke (the GPU now has xtts on it too — this still fits: 1.82 + 2.2 + ~1.5 is over 6 GiB,
#    so run the smoke BEFORE step 2, or stop xtts for the minute the smoke takes).
cat ~/voice/rvc/smoke.log        # the unattended one from §8 — usually already done for you
ssh ... '~/voice/rvc/smoke.sh'   # only if you want a fresh one, and only with the VRAM free
```

Then verify the live contour is untouched, which is the only thing that was ever at risk:

```sh
ssh ... 'curl -s -m 5 127.0.0.1:8355/health'     # {"ok":true,...}
```

> Note: 8355's health prints `"cuda":false`. That is **pre-existing and cosmetic** — the field is
> `torch.cuda.is_available()` in that venv, while the actual whisper model runs on CUDA through
> ctranslate2 (`nvidia-smi` shows it holding 1.82 GiB). Not caused by this work; do not "fix" it.

---

## 10. Troubleshooting

### CUDA OOM — two of them happened, both measured, both instructive

The card is 6144 MiB and the live 8355 server permanently holds 1824 MiB, so **this run's ceiling is
~3.9 GiB, not 6.**

**OOM #1 — batch 4, during epoch 1.** `Tried to allocate 20.00 MiB … 3.81 GiB in use`, in the
discriminator's `leaky_relu`. Straightforward: batch 4 does not fit. Evidence:
`~/voice/rvc/train-oom-batch4.log`.

**OOM #2 — batch 2, at the epoch-15 SAVE, after 14 perfectly healthy epochs.** This is the subtle one and
the reason `--checkpointing True` is mandatory. Evidence: `~/voice/rvc/train-oom-batch2-save15.log`.

```
train.py:857  net_g.infer(*reference)          <- the save-epoch TensorBoard audio preview
  synthesizers.py:227 infer -> encoders.py:140/77 -> attentions.py:84/100/132/164
  F.pad: Tried to allocate 186.00 MiB. GPU 0 ... 124.25 MiB is free
```

The validation preview runs the text encoder's **relative-position attention over the whole reference
clip**, which is far longer than a 3-second training slice, and that attention is O(T²). So the peak
memory of the run is not in the training step at all — it is in a preview that happens only every
`save_every_epoch` epochs. Training can look rock-solid for an hour and then die the first time it tries
to save. **Nothing had been written yet, so all 14 epochs were lost.**

Two things that did *not* help, so do not reach for them again:
- `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` — it was already set and **verified live in the
  trainer's `/proc/<pid>/environ`** during the failure. The OOM message recommends it regardless; ignore
  that advice here, it is already done.
- `torch.cuda.empty_cache()` — `train.py` already calls it at every epoch end, before the preview.

What fixed it: **`--checkpointing True`**. It lowers the *training* peak, so PyTorch's reserved pool stays
smaller and more driver memory is left unreserved for the preview's allocations. Measured: 3438 → **2492
MiB** resident, free 471 → **1419 MiB** — against a preview that needed 186 MiB and could not get it.
Cost: 3:10 → 3:56 per epoch.

**OOM #3 — batch 2 WITH checkpointing, at the epoch-10 save. Same place, same cause.**

```
Tried to allocate 172.00 MiB. GPU 0 ... 62.25 MiB is free.
this process has 3.76 GiB in use; 3.36 GiB allocated by PyTorch, 280 MiB reserved-but-unallocated
```

Read those numbers carefully, because they close the question: during *training* this process held
2492 MiB, but at the save it held **3.36 GiB allocated** — the preview inference itself adds ~900 MiB on
top of the training baseline, and it still came up 172 MiB short. Checkpointing bought ~950 MiB and the
preview ate all of it. Fragmentation is no longer the story either (only 280 MiB reserved-unallocated this
time, vs 1.01 GiB before). **The gap is structural, not tunable:** an O(T²) attention over the full
reference clip does not fit in the ~3.8 GiB left after the whisper server takes its 1.8 GiB.

| symptom | do this |
|---|---|
| OOM at batch 4 | drop to **batch 2** |
| OOM at batch 2 during normal steps | add `--checkpointing True` (5th arg of `train.sh`) |
| OOM at a **save** epoch, checkpointing already on | **stop.** You are at the wall this run hit. Do not keep halving — training steps are not the problem. Go to §11, *Next attempt* |
| something else took the GPU | `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv` — most likely the xtts server came back. Stop it (its RUNBOOK §4a), never the 8355 server |

**The general lesson for this box:** a save/eval path can cost more VRAM than the training step it
follows, so **a run is not proven healthy until it has survived one checkpoint save.** Epoch time, loss
curves and a stable `nvidia-smi` for an hour prove nothing about the save. This is why `save_every_epoch`
was moved 15 → 10 after the first failure: it puts the risky path ~40 min in, where a failure is still
recoverable inside the night.

**All three OOMs were fixed by §11 option (b), not by tuning batch size.** See §11 for the applied change,
the reversal command, and the VRAM trace proving the spike is gone.

### "An error occurred extracting the model: ... assets/config.json" — the silent one

Seen at the epoch-10 save on 2026-08-02, **fixed at 02:01**. It is the nastiest bug here because it does
**not** stop training and does not appear in the status line:

```
Saved model '.../G_4610.pth' (epoch 10)
Saved model '.../D_4610.pth' (epoch 10)
An error occurred extracting the model: [Errno 2] No such file or directory: '.../Applio/assets/config.json'
```

`extract_model()` reads `model_author` out of `assets/config.json`, a file the **GUI** creates on first
launch from `assets/config_template.json` and which therefore **never exists in a headless git clone**. The
failure is caught and logged, so `G_*.pth`/`D_*.pth` (resume state) are written normally while the
`scheherazade_<epoch>e_<step>s.pth` **inference weights are silently skipped — including the final one.**
A run could finish "successfully" and leave nothing you can pass to `--pth_path`.

```sh
cd ~/voice/rvc/Applio && cp assets/config_template.json assets/config.json     # the fix
```

It is read at every save, so the fix takes effect on the next save with no restart. **Check for
`scheherazade_*e_*s.pth` after the first save of any future run.**

**It happened exactly once, and `--cleanup True` is NOT implicated.** It was suspected on 2026-08-02 that
the training flag had wiped `assets/config.json` again at end-of-run, because the same error line was seen
while reading the log. It had not: `grep -c` finds **one** occurrence in the entire run, at the epoch-10
save (02:01), before the fix; the saves at 20/30/40/50/60/70 all extracted weights cleanly, and
`assets/config.json` is still on disk with its original 02:01 mtime. For the record, `cleanup` only walks
`logs/<model_name>/` and only removes `.0` files, `G_*/D_*.pth` and `added*.index` (§6b) — it never
touches `assets/`. If you see that error line, check *when* it was written before acting on it.

### `cuda available: False` after any reinstall

Torch got resolved from PyPI (cu130) instead of the cu128 index. Reinstall with
`--index-url https://download.pytorch.org/whl/cu128`, exactly as `~/voice/rvc/install.sh` does. Do **not**
restart-loop: this is a version mismatch, not a transient.

### "Training failed for model scheherazade." in `train.log`

Means nothing on its own — see §5, the exit code is unreliable in both directions. Check for the
`Training has been successfully completed` marker, and check `train.log` for an OOM traceback. Note the
inverse case actually happened here: the OOM'd batch-4 run reported **no** failure at the `core.py` level.

### Preprocess finds no audio

`core.py preprocess` accepts `.wav/.mp3/.flac/.ogg` and walks subdirectories, where a subdirectory name
must be an **integer speaker id**. The corpus is flat, so everything is speaker 0. The `.txt` transcripts
alongside the mp3s are ignored (RVC is audio-only).

---

## 11. The fix that made it run — option (b), APPLIED

### What was done, and how to reverse it

The OOM lived exclusively in the save-epoch **TensorBoard audio preview**, a cosmetic feature that has no
effect on the trained model. `train.py` uses `logs/reference/<embedder>/feats.npy` if it exists, and
otherwise prints *"No custom reference found, using a default audio sample for validation"* and takes
`next(iter(train_loader))` — i.e. **an ordinary training batch** — as the preview input instead.

**Applied 2026-08-02 01:18 UTC, inside `~/voice/rvc/Applio/`:**

```sh
mv logs/reference/contentvec logs/reference/contentvec.disabled     # the exact command
```

**To reverse, swap the arguments:**

```sh
cd ~/voice/rvc/Applio && mv logs/reference/contentvec.disabled logs/reference/contentvec
```

Nothing was deleted. `logs/reference/{spin,spin-v2}/` were left alone — they are unused here (the embedder
is contentvec), but if you ever switch embedder you must disable the matching one.

### Why it works — the arithmetic

`feats.npy` is 5 354 624 B of 768-dim float32 = **1743 frames**, which `train.py` then doubles via
`np.repeat(phone, 2, axis=0)` to **3486 frames**. A 3-second training slice is ~150–300 frames. The failing
allocation was in relative-position attention, which is **O(T²)** — so the swap cuts that tensor by roughly
**two orders of magnitude**.

### Measured result — the preview spike is gone, not merely smaller

The 5-second VRAM sampler (`~/voice/rvc/vram.log`) across the epoch-10 save:

```
01:58:31   4330 MiB used, 1409 free   | trainer 2502 MiB      <- steady-state training
01:58:46   3978 MiB used, 1761 free   | trainer 2150 MiB      <- THE SAVE: usage went DOWN
01:59:01   4320 MiB used, 1419 free   | trainer 2492 MiB      <- back to normal, epoch 11 running
```

Compare with the failures, where the preview added ~900 MiB on top of the training baseline and still came
up 172–186 MiB short. The peak is now **below** the training baseline: `empty_cache()` releases memory and
the preview is too small to reclaim it. There is no longer a save-epoch spike to survive.

### The levers NOT used (kept for the record)

**(a) Stop the live 8355 whisper server to free its 1.8 GiB** — highest headroom (ceiling ~3.8 → ~5.6 GiB,
would likely allow batch 4 and ~2x speed), but it takes the live voice contour down, which §0 says never to
sacrifice. **Operator-only decision. Not used.**

**(c) Retrain at 32 kHz** — `f0G32k.pth`/`f0D32k.pth` are already downloaded; needs preprocess + extract
re-run with `--sample_rate 32000` (~4 min, both scripted). Small quality cost. **Not needed now.**

**(d) A bigger GPU.** Out of scope for this node.

---

## 12. License and use

**Applio is MIT** — source and the pretrained weights it downloads — so unlike the XTTS/CPML stack next
door, there is no non-commercial restriction from the tool itself. Using the official unmodified Applio
also means accepting Applio's **Terms of Use** (ethical/lawful use, respect for voice-owner rights);
`TERMS_OF_USE.md` ships in `~/voice/rvc/Applio/`.

The binding constraint is therefore **not** the tool but the **voice**: the corpus is ElevenLabs-generated
audio, and the rights to that voice come from that account's terms, not from Applio's MIT grant. Treat the
trained model as carrying the corpus's license, not the code's. Any use beyond the operator's own LAN
voice contour is a licensing question for the human, not a config change.

---

## 13. What was changed outside `~/voice/rvc/` (the honest list)

1. **`xtts_server.py` (8356) stopped** 2026-08-01 23:26 UTC, freeing 2750 MiB. Operator-authorized for the
   night; the speak hook falls back to 8355 automatically.
2. **Restarted** 2026-08-02 01:16 UTC when attempt 3 was abandoned, per its RUNBOOK §4b — verified
   `{"ok":true,"device":"cuda","model_loaded":true}`.
3. **Stopped again** 2026-08-02 01:18 UTC to launch attempt 4, under the same standing authorization
   (with it up, only 1991 MiB were free and training needs ~2500 MiB — the relaunch could not have
   started). **It is DOWN right now**; §9 step 2 restarts it.
4. **Restarted again** 2026-08-02 06:24 UTC once training had finished and the RVC inference was done —
   verified `{"ok":true,"device":"cuda","model_loaded":true}`, 1920 MiB. **Net change: none; it is UP.**
   One `POST /tts` was made to each of 8355 and 8356 to render the demo (§14) — normal use of both, no
   configuration touched.
5. Nothing else. No sudo, no package installed system-wide, no file under `~/voice/`, `~/voice/xtts/` or
   `~/voice/corpus/` written to. The corpus was opened read-only (`sf.info`/`sf.read` only) and is
   byte-for-byte untouched. Changes **inside** `~/voice/rvc/`: the Applio reference directory renamed
   (§11, reversible), `assets/config.json` created from the shipped template (§10), and
   `shims/pedalboard.py` added (§8, opt-in via `PYTHONPATH`, nothing installed modified).

---

## 14. The demo (2026-08-02 06:16–06:25 UTC)

One phrase — *«Доброе утро, мой друг. Ночь испекла мне новый голос — послушай и выбери.»* — in three voices,
in `~/voice/rvc/demo/` on the node and copied to the brain at
`<path>/voice-demo/`:

| file | what | wall clock | audio |
|---|---|---|---|
| `baya.wav` | live 8355, Silero `baya` (ru, female) — the source | ~1 s | 5.60 s, 48 kHz, rms 0.133 |
| `xtts.wav` | 8356 XTTS-v2 voice clone | 4.4 s | 6.28 s, 24 kHz, rms 0.138 |
| `rvc.wav` | `baya.wav` converted through this model, 70-epoch weights + index, `--pitch 0` | 14.6 s total / **5.27 s conversion** | 5.58 s, 40 kHz, rms 0.119 |

Ordering was chosen for VRAM: `baya` (no GPU cost) → **RVC inference while the GPU was empty** → restart
xtts → `xtts`. In hindsight unnecessary — RVC inference peaked at only 206 MiB — but correct given what was
known. Regenerate any of them with the commands in §8 plus:

```sh
curl -s -X POST -H "Content-Type: application/json" \
  --data @~/voice/rvc/demo/phrase.json http://127.0.0.1:8355/tts -o baya.wav   # speaker in the json
```
