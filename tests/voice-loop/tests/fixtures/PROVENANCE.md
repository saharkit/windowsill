# Where these fixtures came from

A fixture that pins a third party's response shape is only worth what its provenance is worth. A
shape invented from a doc page proves the parser parses what we *imagined*; a shape captured from
the live API proves it parses what the vendor actually sends. This file says which each one is, and
nothing here may be silently promoted from the first to the second.

## `deepgram_listen_response.json`

**Status: NOT YET A LIVE CAPTURE — must be replaced before this seam is trusted in production.**

What it is: the documented shape of `POST https://api.deepgram.com/v1/listen` with
`model=nova-3&smart_format=true`, transcribed from Deepgram's published response schema on
2026-08-06, with every identifier zeroed out and the transcript replaced by a test phrase. It is
structurally faithful — the transcript lives at
`results.channels[0].alternatives[0].transcript`, which is the whole thing the parser reads — but
nobody has seen this exact document come off the wire.

Why it is here anyway: it is what makes a shape drift go RED. Without a pinned fixture, a Deepgram
response whose nesting changed would return no transcript, and dictation would degrade to local
whisper under a log line that says "the cloud failed" — the failure mode this seam exists to
prevent. A structurally faithful fixture catches that; it just cannot catch a shape we got wrong on
day one.

**How to replace it (the human step, windowsill#94 acceptance criterion 3):** run a real clip
through the operator's Deepgram key, save the response body verbatim, redact `request_id`, `sha256`
and the `models` UUIDs the same way (zeroed, not removed — the KEYS are part of the shape), replace
the transcript with a phrase safe to publish, drop it in here, and record the live run in the PR.
Then change the Status line above to name the capture date. `test_providers.py` asserts against the
file, so a real capture with a different nesting fails the suite immediately — which is the point.

MIT, like the rest of the repository: nothing in this file is vendor-supplied text.
