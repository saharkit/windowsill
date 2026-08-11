# RUNBOOK-serve — resident RVC conversion service ("scheherazade")

> **Placeholders:** `<gpu-host>` is the training/serving machine and `<user>` is the operator account on it. Replace them with your own values.

**Owner:** Sahar (SRE role) · **Node:** `<gpu-host>` (user `<user>`, no sudo)
**Endpoint:** `http://127.0.0.1:8358` (loopback only) · **Last verified:** 2026-08-02 07:0x UTC
**Node clock is UTC.** Operator local time (Europe/Istanbul) = UTC+3.

> **This is ops v0.** It exists to turn the 14.6 s cold `core.py infer` CLI call into a warm HTTP
> call. It is deliberately small: one model, one endpoint, no auth, no metrics endpoint, no restart
> supervision, no client wiring. Productization — supervision, the speak-hook integration, the
> tunnel, an alert policy — is **windowsill#15**, not this file.
>
> **Nothing is wired to it yet.** The live speak hook and the tunnel were NOT touched; connecting
> them is a separate reviewed step.

---

## 0. What this is, and what it is NOT

| | LIVE — do not touch | xtts | THIS service | the training job |
|---|---|---|---|---|
| what | `~/voice/voice_server.py` :8355 | `~/voice/xtts/xtts_server.py` :8356 | `~/voice/rvc-serve/` :8358 | `~/voice/rvc/` (batch, idle) |
| venv | `~/voice/venv` | `~/voice/xtts/venv` | **`~/voice/rvc/venv`** (shared with training, read-only use) | `~/voice/rvc/venv` |
| VRAM | 1824 MiB, always resident | 2206 MiB cold / **2750 MiB after its first request** | see §5 — 0 or ~1.0-1.5 GiB | 0 (not running) |
| state | the working voice contour | the operator's TTS | **experiment, additive** | done |

**The 8355 server always wins.** This service is the thing that yields, and §5 is built so it yields
*before* it allocates, not after it OOMs.

**Additive, and reversible in one command.** Everything new lives in `~/voice/rvc-serve/`. It writes
nothing outside that directory except transient files under `/dev/shm` that it deletes. It does not
modify Applio, the venv, the weights, the index or the corpus — all are opened read-only. Roll back
with `~/voice/rvc-serve/stop.sh && rm -rf ~/voice/rvc-serve`.

---

## 1. Start / stop / restart

```sh
~/voice/rvc-serve/start.sh     # detached (nohup setsid), waits for /health, prints it. Idempotent.
~/voice/rvc-serve/stop.sh      # pidfile -> socket -> bracketed pgrep; prints the VRAM it returned
~/voice/rvc-serve/stop.sh && ~/voice/rvc-serve/start.sh    # restart
```

- Log: `~/voice/rvc-serve/server.log` (append-only, `-u` unbuffered — unlike the training job's
  block-buffered log, this one does not lose its last lines; RUNBOOK §5 of the training runbook).
- Pidfile: `~/voice/rvc-serve/server.pid`.
- Startup is ~8-10 s (model + hubert load, then one synthetic warm-up conversion). `start.sh` polls
  `/health` for up to 2 min and prints the log tail if the process dies.
- **There is no supervision.** If the process dies, it stays dead until someone runs `start.sh`.
  That is a known v0 gap → windowsill#15.

> **Never `pkill -f rvc_server.py` from an ssh command line.** `pkill -f` matches the remote shell's
> own command line, so the shell kills itself and the server survives. This bit the training work
> twice (training RUNBOOK §6a). `stop.sh` uses the pidfile first and a bracketed `[r]vc_server.py`
> pattern last, for exactly this reason.

---

## 2. The dependency that will break it first: the pedalboard shim

`PYTHONPATH=$HOME/voice/rvc/shims` — training RUNBOOK §8, **READ FIRST** section. This node is an
Intel Xeon E5-1620 (Sandy Bridge, 2012) with AVX but **no AVX2/FMA**, and the `pedalboard` wheel's
native extension is AVX2-only, so it **SIGILLs at import** — `Illegal instruction (core dumped)`,
no traceback, empty log. `rvc/infer/infer.py:11` imports it at module level, so nothing can be
imported without the shim.

Two belts here, deliberately:

1. `start.sh` exports `PYTHONPATH=$HOME/voice/rvc/shims`;
2. `rvc_server.py` *also* puts that directory on `sys.path[0]` itself and then imports `pedalboard`
   explicitly, before anything else, logging which file it resolved to:

```
2026-08-02 06:37:58 INFO pedalboard shim active: ~/voice/rvc/shims/pedalboard.py
```

That import is a **deliberate fail-fast**: if the shim ever stops shadowing the real wheel, the
process dies at startup with an obvious cause instead of dying mid-request. **If `server.log` ends
without that line, the shim is the first thing to check.** The shim's classes raise if instantiated,
so `post_process=True` fails loudly rather than silently skipping effects — the server never sets it.

---

## 3. The API

### `GET /health`

```json
{"ok":true,"device":"cuda","model_loaded":true,"model":"scheherazade_70e","in_flight":0,
 "sample_rate":40000,"chunk_seconds":6.0,"cpu_overflow_loaded":false,"vram_free_mib":697,
 "admission":"card free 1705MiB >= 1700MiB required -> GPU","uptime_s":41.2,
 "converted":12,"chunked":1,"oom_overflows":0,"errors":0}
```

| field | read it as |
|---|---|
| `device` | `cuda` = the fast path (§5). `cpu` = the service is serving but ~30-90x slower |
| `admission` | **why** it is on that device — the pre-flight VRAM decision, in words |
| `in_flight` | requests currently held. Conversions are serialized; >1 means someone is queued |
| `vram_free_mib` | saturation signal. `null` in CPU mode (the process has no CUDA context at all) |
| `oom_overflows` | how many requests the GPU could not take. **>0 means the card is oversubscribed** |
| `chunked` | requests longer than `chunk_seconds` that were split (§6) — normal, not an error |
| `errors` | 4xx/5xx conversions. Non-zero with `converted` climbing = a client problem, not a service one |

### `POST /convert`

Body is **raw audio bytes** (`Content-Type: audio/wav`) or **multipart/form-data** with a `file`
part. libsndfile sniffs the container, so wav/flac/ogg all work regardless of the header. Returns
`audio/wav` (PCM_16, 40 kHz — the model's native rate).

```sh
# raw bytes, the normal call
curl -s -X POST -H 'Content-Type: audio/wav' --data-binary @in.wav \
  'http://127.0.0.1:8358/convert?pitch=0' -o out.wav

# multipart, if a client finds that easier
curl -s -F 'file=@in.wav' -F 'pitch=0' http://127.0.0.1:8358/convert -o out.wav
```

Query params (all optional, defaults are the CLI defaults from training RUNBOOK §8):

| param | default | meaning |
|---|---|---|
| `pitch` | `0` | semitones. `0` for a female source; **`+12` for a male source into this voice** (up, not down). Range ±24 |
| `index_rate` | `0.75` | how hard the faiss index pulls timbre toward the training voice; drop toward 0.3 if you hear artifacts |
| `protect` | `0.33` | consonant/breath protection; raise toward 0.5 if sibilants smear |
| `volume_envelope` | `1.0` | RMS envelope mix |
| `f0_method` | `rmvpe` | leave it — rmvpe is what the model was trained and verified with |

Response headers carry the per-request telemetry, so a client can log it without parsing the log:
`X-RVC-Device`, `X-RVC-Chunks`, `X-RVC-Wall-S`, `X-RVC-Audio-S`, `X-RVC-RTF`.

Error shapes: `400` empty body / undecodable audio / bad pitch, `413` body >128 MB or audio >600 s,
`503` model not loaded, `500` conversion failure (with the exception text).

---

## 4. The seam: Applio as a LIBRARY (this is the one shipped, not a worker subprocess)

`rvc.infer.infer.VoiceConverter.convert_audio` — the exact callable `core.py infer` drives. There is
**no subprocess per request**; the class is stateful and reuses weights (`get_vc` no-ops when the
path is unchanged) and the hubert embedder across calls. Three things make that work in-process:

1. **shim first** (§2);
2. **cwd must be `Applio/`** — `infer.py`, `lib/utils.py` and `configs/config.py` all read
   `os.getcwd()` or relative paths (`rvc/configs/40000.json`, `rvc/models/predictors/rmvpe.pt`) **at
   import time**. `start.sh` cds there and the module re-`chdir`s defensively;
3. **two warm caches patched into `rvc.infer.pipeline`**, process-local, no file edits:

| what upstream does per request | cost | what the server does |
|---|---|---|
| `faiss.read_index(...)` then `index.reconstruct_n(0, ntotal)` | reads the 318 MB index and materializes a (105516, 768) float32 matrix — ~309 MiB — **every call** | memoized: loaded once (0.16 s), matrix built once |
| `RMVPE(device=…)` inside `get_f0` | re-constructs the rmvpe predictor (~1.0 s) **every call** | memoized per (device, sr, hop) |

Fine for a one-shot CLI; absurd for a resident service. These are the difference between "resident"
and "merely long-running" — without them a warm request still pays ~1.2 s of model loading.

`rvc.configs.config.Config` is a `@singleton`, so `.device` is process-global. A converter captures
it only at load time (`net_g`/`hubert` `.to()`, and `Pipeline.__init__` copies it), so a loaded
converter is immune to later flips — which is what makes the two-converter design in §5 safe.

**Reversal:** nothing to reverse. No Applio file, no site-package and no weights file is modified;
all patching is in-process and dies with the process.

---

## 5. VRAM — the constraint, and the admission gate (READ THIS BEFORE TUNING ANYTHING)

**Peak VRAM is roughly linear in input duration, and this card is shared with two incumbents.**
Measured 2026-08-02, both incumbents resident:

| input | process peak | min free on the card | outcome |
|---|---|---|---|
| 1.5 s (warm-up) | ~960 MiB | — | ok |
| 5.6 s | 1008 MiB | 697 MiB | ok, **RTF 0.064** |
| 11.2 s | ~1487 MiB | **83 MiB** | ok, but the card was effectively full |
| 16.8 s | — | **9 MiB** | **CUDA OOM** |
| 61.6 s | — | — | **CUDA OOM** |

Two independent guards came out of that table.

### 5a. The chunker (`CHUNK_SECONDS = 6.0`)

Any input longer than 6 s is split into ≤6 s pieces **at the quietest 20 ms frame** within 1.5 s of
each boundary, converted piece by piece, and re-joined with a 5 ms fade at each seam. Peak VRAM is
therefore bounded by the chunk size, not by the request. Cutting in a low-energy region puts the
join inside a pause, where RVC's own padded windows make it inaudible — verified numerically on an
11.2 s input: max sample-to-sample step 0.1536 against a 99.99th-percentile step of 0.1246, with the
largest steps spread across the file rather than clustered at the seam. (A click would show as an
isolated step far above the distribution. **A listening check on a long input is still an open
item** — the numeric check rules out clicks, not prosody at the joins.)

`CHUNK_SECONDS` is a **headroom constant, not a quality knob.** Raise it only with a fresh
measurement of the table above.

### 5b. The admission gate (`MIN_FREE_VRAM_MIB = 1700`)

Measured line, not a guess: the GPU path **ran with 1705 MiB free** and **OOM'd with 1570 MiB free**.

Before `torch` is even imported, the server reads free VRAM from `nvidia-smi` and, if it is under
1700 MiB, empties `CUDA_VISIBLE_DEVICES`. The consequence matters: a CPU-mode server holds **zero
VRAM** rather than squatting ~1.15 GiB it cannot use — 1.15 GiB handed back to the two incumbents
that own this card. Verified:

```
admission: card free 1161MiB < 1700MiB required -> CPU, holding no VRAM
$ nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader
1029504, 1824 MiB     <- :8355 whisper
1247915, 2750 MiB     <- :8356 xtts
                      <- :8358 absent. Not one MiB.
```

Override for a deliberate measurement: `RVC_FORCE_DEVICE=cuda` (or `=cpu`) in the environment of
`start.sh`. Forcing cuda on a full card is safe for the *incumbents* — a CUDA OOM hits the process
that asks for memory, and this one catches it (§5c) — but it will be slow and noisy.

### 5c. The CPU overflow lane (last line of defence)

If a conversion OOMs anyway, that **single request** is re-run on a lazily-built second converter
held on CPU; the resident CUDA converter is **never downgraded**. This is the fix for a real bug
found during shakedown: the first design flipped the whole server to CPU on the first OOM and left
it there, so one oversized clip made every later request ~30x slower until someone restarted it —
a sticky, silent, self-inflicted outage. `/health.oom_overflows` counts these; **any non-zero value
means the card is oversubscribed and the fast path is not what you think it is.**

### 5d. The capacity finding — an operator decision, not a config change

The card's usable pool is ~5735 MiB, and:

```
:8355 whisper   1824 MiB   fixed, always resident, always wins
:8356 xtts      2206 MiB cold  ->  2750 MiB once it has served one request
                ------------------------------------------------
leaves          1705 MiB (xtts cold)  /  1161 MiB (xtts warm)
:8358 rvc       needs ~1700 MiB for the GPU fast path
```

**So all three do not fit once xtts has served a single request.** The service was measured on the
GPU at RTF 0.064 while xtts was still cold at 2206 MiB; xtts then grew to its 2750 MiB steady state
through normal use, and :8358 has been correctly demoting itself to CPU ever since. Restarting xtts
returns it to 2206 MiB only until its next request — it is not a fix.

The choice is therefore **xtts or rvc on the GPU**, and it belongs to the operator: stopping :8356 is
a live-contour action (training RUNBOOK §11(a)/§13 — the previous night's stop was explicitly
operator-authorized). **This service will not take it.** Options, cheapest first:

1. **Operator stops :8356** when RVC conversion matters more than XTTS cloning, then
   `~/voice/rvc-serve/stop.sh && ~/voice/rvc-serve/start.sh` — the gate will admit CUDA.
2. Shrink the RVC footprint (hubert/net_g in fp16). Applio hardcodes `.float()`; this needs patched
   loading and a quality re-check. Not v0.
3. A bigger card. Out of scope for this node.

---

## 6. Measured latency (the honest numbers, all on 5.6 s of audio = `~/voice/rvc/demo/baya.wav`)

| regime | run 1 | run 2 | run 3 | RTF | note |
|---|---|---|---|---|---|
| **cold CLI, for comparison** | 14.6 s total / 5.27 s conversion | — | — | 0.94 | training RUNBOOK §14 |
| **warm, GPU** (`device":"cuda"`) | **0.41 s** | **0.38 s** | **0.37 s** | **0.064** | server-side 0.389 / 0.356 / 0.356 s |
| warm, CPU lane, node otherwise idle | 12.8 s | 10.3 s | 9.9 s | ~1.8 | |
| warm, CPU lane, **CI running on the node** | 31.0 s | 32.2 s | 30.9 s | ~5.5 | see §7 |

**The headline: 14.6 s cold CLI -> 0.37 s warm GPU request. ~39x, RTF 0.064 (~15x faster than
real time).** The first request after startup is ~1.1 s rather than 0.36 s (cuDNN autotunes for the
new input shape); every request after that is flat.

Long inputs, chunked, on the CPU lane under CI load: 16.8 s in 30.3 s (3 chunks), 61.6 s in 134.7 s
(11 chunks) — both correct output (`dur` preserved to 0.2 s, rms 0.117-0.118, matching the
single-chunk reference 0.118).

Re-measure any time with the shipped smoke:

```sh
~/voice/rvc-serve/smoke.sh                          # baya.wav, 3 runs, pitch 0
~/voice/rvc-serve/smoke.sh /path/in.wav 5 12        # 5 runs, pitch +12
```

> **The GPU row was measured on the same single-chunk code path**, before the admission gate demoted
> the service to CPU (§5d). A 5.6 s input is one chunk, so the shipped path differs from the measured
> one by a single `sf.read` of the input and a `_cut_points` call that returns immediately — under
> 10 ms. It is not a fresh measurement of the current build, and it is not restated as one. **When
> the card next has ≥1700 MiB free** (i.e. after the operator's §5d decision), this is the one
> command that turns it into one:
>
> ```sh
> ~/voice/rvc-serve/stop.sh && ~/voice/rvc-serve/start.sh && ~/voice/rvc-serve/smoke.sh
> ```
>
> Confirm `"device":"cuda"` in the HEALTH line it prints first; if it says `cpu`, read `"admission"`.

It asserts the output is real audio — `dur > 0.5 s` **and** `rms > 0.005`. A returned file that is
silence is a **failure** even though the HTTP status said 200 (same rule as the XTTS and training
smokes).

---

## 7. This node's other saturation signal: CPU, and CI is on it

This host also runs the repo's self-hosted GitHub Actions runner.
During a CI run six of the eight cores sit at ~82 % and the
load average passes 11. **That is the whole explanation for the 10 s -> 31 s CPU-lane row in §6** —
same code, same input, three times slower. It does not affect the GPU path.

So the CPU fallback lane is a *correctness* fallback, not a *performance* one: its latency is
whatever the node's CI schedule leaves over. If a client ever depends on this service's latency, it
depends on the GPU path, which depends on §5d.

```sh
uptime; ps -eo pcpu,pid,args --sort=-pcpu | head -6      # who is eating the node
```

---

## 8. Troubleshooting

| symptom | do this |
|---|---|
| `/health` never comes up; `server.log` stops after the uvicorn banner | model load. Read the traceback in `server.log`; check the weights and index paths in the config block at the top of `rvc_server.py` |
| `server.log` has **no** `pedalboard shim active` line and the process is gone | §2 — the shim is not shadowing the AVX2 wheel. Diagnose with `PYTHONFAULTHANDLER=1` |
| `"device":"cpu"` and you expected cuda | read `"admission"` in the same JSON — it says exactly why. Usually §5d: xtts is warm and the card is full |
| `"oom_overflows"` climbing | the card is oversubscribed (§5c/§5d). The service is still correct, just slow. Do not raise `CHUNK_SECONDS` |
| conversion returns 200 but the wav is silence | treat as failure. Check `index_rate`/`protect` params, and confirm the weights file is the 70-epoch one, not a stale checkpoint |
| everything is slow but `"device":"cuda"` | §7 — check the load average; CI is probably running. GPU path should not care, so also check `nvidia-smi` for a third process |
| `Illegal instruction` in the log | §2, always |
| `Could not initialize NNPACK! Reason: Unsupported hardware.` | **benign, CPU path only.** Same root cause as §2 — this 2012 Xeon lacks the instruction set NNPACK wants, so torch falls back to a generic conv kernel. It is part of why the CPU lane is slow. Not an error, do not chase it |
| port 8358 in use by a stale process | `~/voice/rvc-serve/stop.sh` (it finds the pid by socket, not by pattern) |

---

## 9. Files

```
~/voice/rvc-serve/
├── rvc_server.py        # the service — config block at the top is the only thing to edit
├── start.sh             # detached launch + /health wait (idempotent)
├── stop.sh              # pidfile -> socket -> bracketed pgrep; reports VRAM returned
├── smoke.sh             # real-invocation smoke: N conversions + non-silence assertions
├── RUNBOOK-serve.md     # this file
├── server.log           # append-only, unbuffered
└── server.pid
```

Read-only dependencies, none of them modified: `~/voice/rvc/venv` (the training venv — `fastapi`,
`uvicorn`, `python-multipart` were **already present**, nothing was installed), `~/voice/rvc/shims/`,
`~/voice/rvc/Applio/` (code + `rvc/models/`), and the two model files in the config block:

```
~/voice/rvc/Applio/logs/scheherazade/scheherazade_70e_32270s.pth
~/voice/rvc/Applio/logs/scheherazade/scheherazade.index
```

---

## 10. Known v0 gaps

Productization is **windowsill#15**; the monitoring gap is filed separately as **windowsill#40**
(this service, and the two incumbents next to it, have no SLI and no alert — every fault listed
below is currently detected by a human noticing).

1. **No supervision.** No systemd unit (no sudo on this node), no user-level restart-on-boot. It
   dies quietly. A `@reboot` cron entry or a `systemd --user` unit is the obvious next step.
2. **No client wiring.** The speak hook and the tunnel are untouched, deliberately.
3. **No metrics endpoint and no alert (windowsill#40).** `/health` carries counters, but nothing
   scrapes them. The SLI that matters is **conversion latency at the p95, split by `device`**,
   because the CPU/GPU split is a 30-90x cliff, not a gradient — an average over both is meaningless.
   The alert worth having first is not on latency at all but on `"device":"cpu"` while a client
   depends on the fast path, i.e. **the admission gate demoting itself is the page-worthy event**
   (§5d): the service stays correct and simply gets ~80x slower, silently, until a human looks.
4. **Single model, no reload endpoint.** Switching weights needs a restart.
5. **No auth.** Loopback binding is the only control. Same posture as :8356.
6. **Concurrency is 1.** Conversions are serialized under a lock (the pipeline, the CUDA context and
   the `Config` singleton are all shared). `in_flight > 1` means queueing, and queued requests
   inherit the wait — there is no admission limit or timeout on the queue.
7. **Chunk joins are numerically clean but not ear-verified** (§5a).
