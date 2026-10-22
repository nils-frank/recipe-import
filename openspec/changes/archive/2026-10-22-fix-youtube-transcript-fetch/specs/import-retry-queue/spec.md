# Spec Delta

## MODIFIED Requirements

### Requirement: Transient upstream failures are queued instead of failed

An import whose failure came from an upstream dependency that reported temporary
unavailability SHALL be placed in a retry queue rather than marked failed. The following
failures are transient:

- the LLM provider answered with a transient HTTP status (429, 500, 502, 503, 504) on
  both the initial call and its immediate in-request retry;
- YouTube refused to hand over a video's captions - by rate limiting the request, by
  demanding proof that the caller is not a bot, or by any other refusal that is not an
  answer about the video's content;
- Mealie could not be reached at all (connection failure, DNS failure, or timeout).

All other failures are permanent and SHALL keep their current behaviour: the import is
marked failed and a single "Import fehlgeschlagen" notification is sent with the existing
wording. Permanent failures include: no recipe found in the source, YouTube answering
that the video has no captions in German or English, an unreadable PDF, an unsupported
file type, a model answer that does not match the schema after its second attempt, and
any Mealie response that carried an HTTP status.

#### Scenario: LLM at capacity

- **WHEN** the LLM provider answers with HTTP 503 on both attempts of a recipe extraction
- **THEN** the import is queued for a later retry
- **AND** the import is not marked failed

#### Scenario: YouTube throttles subtitles

- **WHEN** the caption request for a video is rejected as throttled
- **THEN** the import is queued for a later retry

#### Scenario: YouTube demands a bot check

- **WHEN** the caption request for a video is answered with a demand to sign in to
  confirm the caller is not a bot
- **THEN** the import is queued for a later retry
- **AND** it is not marked failed as a video without subtitles

#### Scenario: Mealie unreachable

- **WHEN** a request to Mealie fails with a connection error rather than an HTTP status
- **THEN** the import is queued for a later retry

#### Scenario: Mealie rejects the recipe

- **WHEN** Mealie answers a recipe creation with an HTTP error status
- **THEN** the import is marked failed immediately
- **AND** the person receives the existing "Mealie hat den Import abgelehnt" notification

#### Scenario: Source has no recipe

- **WHEN** the source yields no recipe
- **THEN** the import is marked failed immediately with the existing wording
- **AND** it is not queued
