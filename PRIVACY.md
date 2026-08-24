# Privacy

The short version: **this software collects nothing.** No telemetry, no analytics, no
update pings, no accounts with us — the only accounts involved are yours (your speech
provider's, and your GitHub account if you choose to file a bug report). We cannot see that
you installed it, and we cannot see what you do with it. This page exists because we would
rather say that plainly than leave a form field blank — and because a privacy page is only
worth reading if it is precise, the fine print below was checked against the code, not
written from hope.

## What the plugins themselves do

Nothing of yours is sent anywhere because of us — there is no server of ours to send it to.
Three network exceptions exist, none of them your data: the local speech server downloads
its models once (from GitHub, Coqui and PyTorch's mirrors); setup makes one HTTPS request to
`pypi.org` to check your Python's certificate store; and installing the plugin at all is a
fetch from GitHub through Claude Code's marketplace — that traffic belongs to Claude Code
and GitHub, not to us.

Working files stay on your machine, under `~/.local/state` and `~/.config` — and here is
exactly what they hold, because some of it is the content of your speech: the last recorded
clip (`dictate-last.wav`), the last spoken line verbatim (`last-spoken`), and the first
~100 characters of each transcript and spoken line in the two logs. They stay until you
delete them; nothing prunes them but a 1 MB log roll. While a line is being played, its
audio sits briefly in your temp directory (removed when playback ends; a hard crash can
leave one behind). The local server also keeps its model cache under `~/.cache`.

**agent-statusline** also writes under `~/.claude/`: `/statusline-setup` copies the renderer to
`~/.claude/tools/agent-statusline.js`, writes the `statusLine` key into `~/.claude/settings.json`
preserving every other key, and takes a `~/.claude/settings.json.bak` before any overwrite. The plugin
reads the payload Claude Code pipes to it on stdin, sends nothing anywhere, and writes no snapshot of
that payload to disk.

## Where your voice goes

Voice-loop turns speech into text and text into speech. The audio and transcripts go to the
speech backend **you** configure — plus one named fallback: if a cloud call fails, the clip
is retried once against the local speech server on `127.0.0.1:8355`, which never leaves
your machine. That choice of backend is the whole privacy story:

- **Local mode** — the model runs on your machine. Your voice never leaves it.
- **LAN mode** — the model runs on another machine on your own network, over plain HTTP
  with no authentication by default: anyone on that network can read the traffic or use the
  server, so treat "your network" as literally trusted (or put TLS/ssh in front). The
  server keeps its own log on that machine, and recognized text appears in it.
- **Cloud mode** — audio or text goes to the provider you picked, under **your** API key
  and **their** privacy policy. The provider tables in
  [PROVIDERS.md](plugins/voice-loop/PROVIDERS.md) carry a privacy-posture column per
  provider, so you can make that choice with open eyes.

What gets spoken and recorded: only lines the assistant explicitly tags are synthesized —
plus the voice-contour alert line, if you set that poller up (`contour.alerts: false` turns
it off). Dictation records between the toggle-press that starts it and the toggle-press
that stops it — never at any other time. It is a toggle, not a held key: if you forget to
stop it, it keeps recording, and the desktop notification is your cue.

## Bug reports

`/report-bug` builds its bundle on your machine and strips it before you see it. The
**redaction is code with tests** — [`scripts/report_bug.py`](plugins/voice-loop/scripts/report_bug.py),
pinned rule by rule in [`tests/test_report_bug.py`](plugins/voice-loop/tests/test_report_bug.py):
API keys and tokens go (by shape and by name), your username (three characters or longer)
and home paths go, hostnames inside URLs and bare IPv4 addresses go (loopback stays), and
anything you said or heard keeps its length and loses its words. The honest edges: a bare
hostname or an IPv6 literal inside an error message can survive the rules, and the values
of your `VOICE_LOOP_*` settings travel (credential-named ones show only as "set") — which
is exactly why the whole bundle is **shown to you, every byte, before anything is sent**.
That show-and-ask step is enforced by the `/report-bug` skill itself; sending is a separate
command that is never run automatically.

And know where "sent" means: the primary transport is a **public** issue at
`github.com/saharkit/windowsill`, filed with your own GitHub account (the alternatives are
a pre-filled form you submit yourself, or a mailto). A public issue is permanent and
world-readable — that is the reason you read the bundle first.

## What we would have to change to break this

A page like this is only as good as its next edit. If any plugin here ever gains a network
call beyond the ones this page names, that is a breaking change to this document — it will
be stated in the release notes and on this page, not slipped into a minor version.
