# Spec Delta

## Purpose

Turns a shared YouTube video into the raw text a recipe can be read from - its spoken
captions plus the video's own title and description - and keeps the three possible
outcomes apart: text obtained, no captions on the video, and captions withheld by
YouTube.

## ADDED Requirements

### Requirement: A video's recipe text comes from its captions plus its title and description

For a YouTube URL the system SHALL obtain the video's captions as plain running text, and
SHALL combine them with the video's title and description into the raw text handed to the
recipe extraction. The video itself and its audio track SHALL NOT be downloaded, and no
speech recognition SHALL be performed.

Captions SHALL be preferred in German, and English SHALL be used when German is not
available. Automatically generated captions are acceptable. The caption text SHALL be
reduced to running prose: timing information, cue markers and formatting markup SHALL NOT
appear in it, and a line repeated by consecutive caption blocks SHALL appear once.

The title SHALL also be reported separately, so that a recipe can be named from it.

#### Scenario: Video with German captions

- **WHEN** a video with German captions is shared
- **THEN** the raw text contains the caption text as prose, without timestamps or markup
- **AND** it also contains the video's title and description

#### Scenario: Only English captions available

- **WHEN** a video has English captions but no German ones
- **THEN** the English captions are used

#### Scenario: Automatic captions only

- **WHEN** a video has no manually written captions but automatic ones in German or
  English
- **THEN** those automatic captions are used
- **AND** a line repeated across consecutive caption blocks appears once in the raw text

### Requirement: A video without usable captions fails permanently

When YouTube answers that the video has no captions in German or English, or the captions
it hands over contain no usable text, the import SHALL fail permanently with the existing
wording stating that the video has no subtitles. Such an import SHALL NOT be queued for a
retry: the answer will not change on a later attempt.

#### Scenario: Video has no captions at all

- **WHEN** a shared video offers no captions in German or English
- **THEN** the import fails permanently stating that the video has no subtitles
- **AND** it is not queued for a retry

#### Scenario: Captions contain no text

- **WHEN** the captions handed over for a video contain no usable text
- **THEN** the import fails permanently with the same wording

### Requirement: Captions withheld by YouTube are reported as a throttle

When YouTube refuses to hand over the captions of a video that has them - by rate
limiting the request, by demanding proof that the caller is not a bot, or by any other
refusal that is not an answer about the video's content - the system SHALL report that
refusal as a throttle and SHALL NOT report it as a video without subtitles. A throttle is
a transient failure and is therefore queued for a later attempt by the retry queue.

The distinction SHALL NOT depend on the exact wording of a single upstream error message:
a refusal that the system cannot attribute to the video's content SHALL count as a
throttle.

#### Scenario: Caption request rate limited

- **WHEN** YouTube rejects the caption request of a video as too frequent
- **THEN** the failure is reported as a throttle
- **AND** the import is queued for a later attempt

#### Scenario: Caption request answered with a bot check

- **WHEN** YouTube answers the caption request by demanding sign-in to confirm the caller
  is not a bot
- **THEN** the failure is reported as a throttle, not as a video without subtitles
- **AND** the import is queued for a later attempt

#### Scenario: Unattributable refusal

- **WHEN** the caption request fails for a reason the system cannot attribute to the
  video having no captions
- **THEN** the failure is reported as a throttle

### Requirement: Title and description are obtained independently of the captions

The title and description SHALL be obtained by a request separate from the one that
obtains the captions, and the two SHALL fail independently of each other. A failure to
obtain title and description SHALL NOT be reported as a video without subtitles, and the
absence
of a description SHALL NOT prevent an import whose captions were obtained: the captions
alone are sufficient raw text.

#### Scenario: Captions available, description empty

- **WHEN** a video's captions are obtained and the video has no description
- **THEN** the import proceeds with the captions and the title as raw text

#### Scenario: Metadata unavailable, captions available

- **WHEN** the video's title and description cannot be obtained but its captions can
- **THEN** the import proceeds with the caption text as raw text
