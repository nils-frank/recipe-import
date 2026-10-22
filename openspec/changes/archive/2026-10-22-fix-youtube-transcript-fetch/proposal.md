# Proposal

## Why

YouTube imports cannot produce a transcript any more. `yt-dlp` downloads captions from a
signed `api/timedtext` URL, and YouTube now answers that URL with `HTTP Error 429` unless
the request carries a proof-of-origin token, which the deployed build cannot produce
(`[youtube] [pot] PO Token Providers: none`). The 429 is a refusal code for the missing
token, not real rate limiting, so it does not pass: the two most recent YouTube imports
(2026-10-19 and 2026-10-20) both failed after the retry queue had spent every attempt on
the same wall, and one of them told the person the video had no subtitles although it
offers captions in over a hundred languages.

## What Changes

- Read the transcript from YouTube's innertube transcript endpoint instead of the
  token-gated `timedtext` download. Measured on 2026-10-22 from the same public address
  that the service runs on, this endpoint returns the German or English transcript of
  both videos that had just failed, without a token, cookies, or an account.
- Keep `yt-dlp` for the video's title and description. That path is unaffected and still
  works; only the caption fetch is blocked.
- Treat YouTube refusing a caption request as a throttle in every form it takes, so a
  refusal is parked for a later attempt instead of being reported as a video without
  subtitles. Today only the literal text `HTTP Error 429` is recognised, and the
  bot-verification refusal (`Sign in to confirm you're not a bot`) falls through to the
  "no subtitles" wording.
- Raise the `yt-dlp` pin from `2026.8.19` to `2026.10.21`.

Deliberately not part of this change: a proof-of-origin token provider
(`bgutil-ytdlp-pot-provider`), which needs a second container and a Node runtime on a host
with 11.9 GB free, and a cookie file from a signed-in account, which expires and ties
imports to a personal account. Neither is needed once the transcript comes from an
endpoint that does not ask for a token.

## Capabilities

### New Capabilities

- `youtube-transcript`: how a YouTube video's recipe text is obtained - which source
  supplies the transcript, which supplies title and description, the language
  preference, and how a video without captions is told apart from a video whose captions
  YouTube refused to hand over.

### Modified Capabilities

- `import-retry-queue`: the transient list names "YouTube throttled the subtitle
  download", which is tied to one error text. It changes to cover any refusal of a
  caption request by YouTube, so that a bot-verification refusal is queued rather than
  being counted among the permanent failures as "no subtitles on the video".

## Impact

- `src/sources/youtube.py`: the transcript no longer comes from a file written by
  `yt-dlp`; `yt-dlp` is called once for metadata, and the subtitle flags, the per-language
  loop, the WebVTT/SRT parser and the work directory it needed go away.
- `src/app.py`: `ThrottledError` keeps its wording and its transient classification; the
  text that a bot-verification refusal maps to changes from "no subtitles" to the
  existing throttle wording.
- `requirements.txt`: `youtube-transcript-api` added, `yt-dlp` pin raised.
- `tests/test_youtube.py`: the tests stub a `subprocess.run` that writes subtitle files;
  they move to stubbing the transcript source, keeping the rule that tests make no
  network calls.
- `DESIGN.md` §6 (`youtube.py` module contract) and §7 (feedback wording for the
  throttle case).
- No deployment change: no new environment variable, no new container, no change to
  `deploy/compose.yaml`. The homelab repository needs a rebuild of the image, nothing
  more.
