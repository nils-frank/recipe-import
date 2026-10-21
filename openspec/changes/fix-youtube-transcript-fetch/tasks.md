# Tasks

## 1. Dependency

- [x] 1.1 Add `youtube-transcript-api==1.2.4` to `requirements.txt` with a comment in the
  style of the existing blocks (date, why it is there, arm64 wheel), and verify
  `pip install -r requirements.txt` resolves in a clean virtual environment without a
  compiler
- [x] 1.2 Raise the `yt-dlp` pin from `2026.8.19` to `2026.10.21` and verify the suite
  still passes against it (`pytest -q`)

## 2. Transcript source

- [x] 2.1 Add a transcript fetch to `src/sources/youtube.py` that calls
  `YouTubeTranscriptApi().fetch(video_id, languages=["de", "en"])` and joins the snippets
  to prose, keeping the de-duplication of consecutive identical lines; verify with a new
  test over a recorded snippet list that a rolling-duplicate transcript collapses to each
  line once
- [x] 2.2 Extract the video id from the URL for that call, covering the `watch?v=`,
  `youtu.be/` and `shorts/` shapes, and verify with a unit test per shape
- [x] 2.3 Reduce the `yt-dlp` invocation in `_run_yt_dlp` to
  `--skip-download --print-json --no-warnings`, returning title and description only, and
  verify by asserting on the command the stubbed `subprocess.run` receives that no
  subtitle flag is passed
- [x] 2.4 Rewrite `fetch()` to combine transcript, title and description into the
  `SourceResult` (`stage="transcript"`, `recipe=None`), with a failed metadata call
  leaving title and description empty instead of failing the import; verify with a test
  where `subprocess.run` returns a non-zero exit status and the transcript still produces
  a result
- [x] 2.5 Delete the parts the file no longer needs - the per-language loop, the
  temporary work directory, `_find_subtitle_file`, the WebVTT/SRT parser, `_THROTTLE_RE`
  and the subtitle fixtures that only served them - and verify `pytest -q` and
  `ruff check .` pass with no reference left to them

## 3. Failure classification

- [x] 3.1 Map the library's exceptions onto `NoTranscriptError` and `ThrottledError` per
  the table in design.md, with any unrecognised failure defaulting to `ThrottledError`,
  and verify with one test per row plus a test that an exception type not in the table is
  a throttle
- [x] 3.2 Verify through `app.is_transient` and `app._describe_source_error` that a
  bot-check refusal is queued with the existing "YouTube drosselt gerade die Untertitel"
  wording and no longer reports "Das Video hat keine Untertitel", as the retry-queue delta
  requires
- [x] 3.3 Confirm no wording in `src/app.py` changed otherwise, by running the existing
  `tests/test_transient.py` and `tests/test_retry_queue.py` unchanged

## 4. Documentation

- [x] 4.1 Update the `youtube.py` contract in `DESIGN.md` §6: captions from the transcript
  endpoint, `yt-dlp` for title and description, one call per video, no subtitle files, and
  the reason the old one-language-per-call rule is gone; verify the section no longer
  describes `--write-auto-sub` or `<id>.<lang>.vtt`
- [x] 4.2 Record in `DESIGN.md` §7 that the throttle wording now also covers a
  bot-verification refusal, leaving the message text itself unchanged

## 5. Verification

- [ ] 5.1 Run `pytest -q` and `ruff check .` on the finished change and verify both pass
- [ ] 5.2 Run `openspec validate fix-youtube-transcript-fetch --strict` and
  verify it reports no errors
- [ ] 5.3 Deployment side, in the homelab repository: rebuild and restart the image, then
  re-share `https://www.youtube.com/watch?v=XLd6yTtwCqo` and
  `https://www.youtube.com/watch?v=gRwcWgQ7YVI` and record in the
  `recipe-import-*` notes that both reach Mealie. No compose or environment change is part
  of this change
