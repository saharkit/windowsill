"""AVX2-free stand-in for `pedalboard`.

WHY: on a CPU that lacks AVX2/FMA, the `pedalboard` wheel ships a native extension built for
AVX2, so merely IMPORTING it dies with SIGILL (`Illegal instruction (core dumped)`, rc=132, no
traceback, empty log). `rvc/infer/infer.py` imports these names at module level, so RVC inference
cannot start at all -- while training is entirely unaffected, because the training path never
imports pedalboard. That is why a model can train for 70 epochs and then crash on its first
inference.

Pedalboard is only used by `post_process_audio()`, reached solely when `post_process=True`; the
CLI default is `False`. So the import is the only hard requirement, and these placeholders satisfy it.

They deliberately RAISE rather than silently no-op: if anyone ever enables the post-processing
effects on such a CPU, that must fail loudly, not produce audio that quietly skipped the requested
effect.

This file ships in the repo at `plugins/voice-loop/rvc/shims/pedalboard.py`. To use it on an
AVX2-less host, copy it (or the whole `shims/` directory) to `~/voice/rvc/shims/` and put that
directory ahead of site-packages:

    PYTHONPATH=$HOME/voice/rvc/shims        # the infer / serve commands in the runbooks already do

Remove it by deleting the file (or dropping `PYTHONPATH`). Nothing in site-packages is modified.
"""

_MSG = (
    "pedalboard is not usable here: this CPU lacks AVX2/FMA and the pedalboard wheel's native "
    "extension SIGILLs on import. This is the AVX2-free shim "
    "(plugins/voice-loop/rvc/shims/pedalboard.py). Audio post-processing effects are unavailable; "
    "run inference with post_process=False (the default)."
)


class _Unavailable:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(_MSG)


class Pedalboard(_Unavailable):
    pass


class Chorus(_Unavailable):
    pass


class Distortion(_Unavailable):
    pass


class Reverb(_Unavailable):
    pass


class PitchShift(_Unavailable):
    pass


class Limiter(_Unavailable):
    pass


class Gain(_Unavailable):
    pass


class Bitcrush(_Unavailable):
    pass


class Clipping(_Unavailable):
    pass


class Compressor(_Unavailable):
    pass


class Delay(_Unavailable):
    pass
