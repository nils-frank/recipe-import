# Design

## Context

See proposal.md - Why for the motivation and the measurements behind it.

What matters for the approach, all measured on the live host on 2026-10-22, from the same
public address the service runs on:

- `yt-dlp 2026.8.19` reaches YouTube normally. The video page, the player API answer and
  the list of caption tracks all arrive; `--list-subs` prints over a hundred languages for
  the video whose import had just reported "no subtitles".
- Only the caption fetch fails. `yt-dlp` resolves captions to a signed
  `https://www.youtube.com/api/timedtext?...&signature=...` URL that carries no `pot`
  parameter, because the container has no proof-of-origin token provider
  (`[youtube] [pot] PO Token Providers: none`). That URL answers `HTTP Error 429`.
- The 429 is unconditional, not a passing rate limit. It also comes back with
  `--sub-format json3`, `srv1` and `ttml`, with `--retries 10 --retry-sleep 5
  --extractor-retries 5 --sleep-subtitles 2`, and three days after the first failure.
  The player clients `web`, `mweb`, `web_safari`, `tv` and `android_vr` do not route
  around it: they either return no caption tracks at all or fail the same way.
- YouTube's innertube transcript endpoint hands over the same captions from that address
  with no token, no cookies and no account. `youtube-transcript-api 1.2.4` returned the
  German transcript of `XLd6yTtwCqo` (635 characters) and the English one of
  `gRwcWgQ7YVI` (364 characters) - the two videos whose imports failed.

Constraints from the project: modules in `src/` are flat and flatly imported, tests make
no network calls, the container runs on a Raspberry Pi with roughly 11.9 GB free on the
card, and the image carries no `ffmpeg` and no Node runtime.

## Goals / Non-Goals

**Goals:**

- One source for captions that works without a proof-of-origin token, and one source for
  title and description, each failing independently of the other.
- A classification of caption failures that does not hinge on matching one upstream error
  string, so the next wording YouTube invents does not reappear as "this video has no
  subtitles".
- No change to the deployment surface: no new container, no new environment variable, no
  new system package.

**Non-Goals:**

- Any form of authentication towards YouTube (cookie file, account, API key).
- A proof-of-origin token provider, in-process or as a sidecar.
- Speech recognition as a fallback when a video genuinely has no captions - that remains
  a non-goal of the project (DESIGN.md §1).
- Changing the retry schedule, its limits, or the wording the person sees.

## Decisions

### Captions come from `youtube-transcript-api`, not from a hand-written innertube call

The transcript endpoint needs a `params` token built from the video id, and its answer is
a protobuf-flavoured JSON structure whose shape changes without notice.
`youtube-transcript-api 1.2.4` builds that request, handles the consent cookie, exposes
the available tracks with their language codes and their manual-or-generated flag, and
returns parsed snippets. It is a pure-Python wheel (`py3-none-any`) and pulls in only
`requests`, which the project already has, and `defusedxml`, also pure Python - so the
arm64 image stays compiler-free, as `requirements.txt` requires of every pin.

Alternative considered: issue the innertube request directly with `requests`, avoiding a
dependency. Rejected - it would put the token construction and the answer parsing into
this repository, which is precisely the part that breaks when YouTube changes something,
and that is the maintenance the library exists to absorb.

Alternative considered: keep `yt-dlp` for captions and add `bgutil-ytdlp-pot-provider`.
Rejected in the proposal: a second container and a Node runtime on this host, to obtain a
token the working endpoint does not ask for.

### `yt-dlp` stays, for title and description only

The metadata path is unaffected, and `yt-dlp` already knows how to get a video's title and
description from a URL in any of YouTube's shapes. The call loses its caption flags and
becomes a single invocation:

```
yt-dlp --skip-download --print-json --no-warnings <url>
```

With that, the per-language loop, the work directory under `/tmp`, the
`<id>.<lang>.vtt` file discovery and the WebVTT/SRT parser all go away: nothing writes a
file any more. The 300-second process limit stays as the defensive bound it already was.

Alternative considered: drop `yt-dlp` and take the title from the transcript endpoint's
neighbouring data. Rejected - the description is useful raw text (it often holds the
written-out recipe), and the library does not expose it.

### Language choice moves into one call

`YouTubeTranscriptApi().fetch(video_id, languages=["de", "en"])` applies the preference in
one request: German if present, English otherwise, manual tracks before generated ones.
That replaces the two sequential `yt-dlp` invocations, and with them the reason they
existed (A9: one `HTTP 429` discarding the other language's already downloaded file).

### Failure classification inverts its default

Today `ThrottledError` is raised only when `stderr` matches `HTTP Error 429`, and anything
else becomes `NoTranscriptError` - which is how a bot check became "this video has no
subtitles". The new rule is the other way round: only an answer *about the video* is
permanent, everything else is a throttle.

| Library exception | Mapped to | Why |
|---|---|---|
| `NoTranscriptFound`, `TranscriptsDisabled` | `NoTranscriptError` | YouTube answered about this video: no captions in de/en |
| `VideoUnavailable`, `InvalidVideoId`, `AgeRestricted`, `VideoUnplayable` | `NoTranscriptError` | Also about this video, and no later attempt changes it |
| `RequestBlocked`, `IpBlocked`, `PoTokenRequired`, `YouTubeRequestFailed`, `YouTubeDataUnparsable` | `ThrottledError` | A refusal or a shape change, not an answer about the video |
| any other `CouldNotRetrieveTranscript`, `requests.RequestException` | `ThrottledError` | Unattributable, so transient by default |

An empty transcript stays `NoTranscriptError`, as today.

The yt-dlp stderr scan disappears with the caption flags; `_THROTTLE_RE` is deleted rather
than extended. A failure of the metadata call is not a caption failure at all: it is
logged and leaves title and description empty, and the import continues on the transcript
alone.

Alternative considered: keep an allow-list of transient exceptions and treat the rest as
permanent. Rejected - that is the current design, and the current design is what produced
a wrong message to the person. A throttle that was really permanent costs a day of
retries and then says "please share it again"; a permanent failure misreported as a video
without captions tells the person something untrue about their video.

### `yt-dlp` pin raised to 2026.10.21

The pin is two months old and the metadata path it now serves is the part YouTube keeps
changing. `2026.10.21` is the current release. This is independent of the caption fix and
gets its own commit, so a regression in it can be reverted without touching the rest.

## Risks / Trade-offs

- [The transcript endpoint gets gated the same way the timedtext URL was] → Then captions
  need a token whichever way they are fetched, and the choice becomes a token provider or
  no YouTube imports. The classification change above means that day arrives as "YouTube
  is throttling, retrying later" plus a queue that gives up after 24 hours, not as a
  false claim about the video. The library is also the place where such a change is
  absorbed first.
- [`youtube-transcript-api` is a second YouTube-facing dependency to keep current] →
  Accepted: it replaces the caption responsibility rather than adding to it, and both
  pins are plain version bumps in `requirements.txt`.
- [Treating unknown failures as transient parks imports that can never succeed] → Bounded
  by the existing queue limits (5 attempts, 24 hours), after which the person gets the
  existing "share it again" message. The previous default was worse: it ended the import
  immediately with a wrong reason.
- [The caption text from the transcript endpoint may be segmented differently than
  WebVTT] → The joining step keeps the de-duplication of consecutive identical lines, so
  both rolling auto-captions and clean segments produce the same prose. Verified on both
  failing videos during the measurement above.

## Migration Plan

No data migration and no deployment change: no new environment variable, no compose
change, no new container. Rolling the change out is the existing image rebuild and
restart from the homelab repository's runbook. Rollback is the previous image, which
restores today's behaviour - YouTube imports failing.

Queue entries that are parked when the new image starts are retried by the new code
path, so an import that is still in the queue from a token-less 429 can succeed after the
deploy without the person doing anything.

Verification against the live system belongs to the homelab repository: re-share
`XLd6yTtwCqo` and `gRwcWgQ7YVI` after the deploy and record that both reach Mealie.
