# Privacy

The short version: **this software collects nothing.** No telemetry, no analytics, no
accounts, no "anonymous usage statistics". We cannot see that you installed it, and we
cannot see what you do with it. This page exists because we would rather say that plainly
than leave a form field blank.

## What the plugins themselves do

Nothing leaves your machine because of us. There is no server of ours to send anything to.
The plugins store their working files — logs, job state, config — on your machine, under
your `~/.local/state` and `~/.config` directories, where you can read and delete them.

## Where your voice goes

Voice-loop turns speech into text and text into speech. The audio and the transcripts go
**only** to the speech backend **you** configure — that choice is the whole privacy story:

- **Local mode** — the model runs on your machine. Nothing ever leaves it.
- **LAN mode** — the model runs on another machine on your own network. Nothing leaves your
  network.
- **Cloud mode** — audio or text goes to the provider you picked, under **your** API key and
  **their** privacy policy. The provider tables in
  [PROVIDERS.md](plugins/voice-loop/PROVIDERS.md) carry a privacy-posture column per
  provider, so you can make that choice with open eyes.

Only what you tag is ever spoken: the assistant marks lines for speech explicitly, and
everything unmarked stays text. Dictation records only while you hold the toggle on.

## Bug reports

`/report-bug` builds its bundle on your machine, **strips it first, then shows you every
byte and asks** before anything is shared — naming where it would go. The stripping removes
API keys and tokens (by shape and by name), your username and home paths, every hostname
except loopback, and the words of anything you said or heard — a transcript line keeps its
length and loses its text. This is not a promise in prose: the redactor is code
([`scripts/report_bug.py`](plugins/voice-loop/scripts/report_bug.py)) with tests pinning
each rule ([`tests/test_report_bug.py`](plugins/voice-loop/tests/test_report_bug.py)).
And nothing is ever sent without your explicit yes, per report.

## What we would have to change to break this

A page like this is only as good as its next edit. If any plugin here ever gains a network
call that is not the backend you configured, that is a breaking change to this document —
it will be stated in the release notes and on this page, not slipped into a minor version.
