# Spec Delta

## Purpose

Keeps a shared recipe alive when an upstream dependency is temporarily unavailable - the
LLM provider is at capacity, YouTube is throttling, or Mealie is briefly unreachable - by
parking the import and retrying it automatically on a schedule, instead of discarding it
and asking the person to share the same link or file again.

## ADDED Requirements

### Requirement: Transient upstream failures are queued instead of failed

An import whose failure came from an upstream dependency that reported temporary
unavailability SHALL be placed in a retry queue rather than marked failed. The following
failures are transient:

- the LLM provider answered with a transient HTTP status (429, 500, 502, 503, 504) on
  both the initial call and its immediate in-request retry;
- YouTube throttled the subtitle download;
- Mealie could not be reached at all (connection failure, DNS failure, or timeout).

All other failures are permanent and SHALL keep their current behaviour: the import is
marked failed and a single "Import fehlgeschlagen" notification is sent with the existing
wording. Permanent failures include: no recipe found in the source, no subtitles on the
video, an unreadable PDF, an unsupported file type, a model answer that does not match
the schema after its second attempt, and any Mealie response that carried an HTTP status.

#### Scenario: LLM at capacity

- **WHEN** the LLM provider answers with HTTP 503 on both attempts of a recipe extraction
- **THEN** the import is queued for a later retry
- **AND** the import is not marked failed

#### Scenario: YouTube throttles subtitles

- **WHEN** the subtitle download for a video is rejected as throttled
- **THEN** the import is queued for a later retry

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

### Requirement: Retry timing uses backoff, then an off-peak window

A queued import SHALL carry a due time and SHALL NOT be retried before it. The due time
for the first retries is computed by exponential backoff on the number of attempts
already made, measured in minutes, with a random jitter added so that several items
queued at the same moment do not retry in lockstep.

Once the computed backoff delay would exceed a configured threshold, the due time SHALL
instead be the start of the next configured off-peak window, plus jitter within that
window. The off-peak window is a local-time range given by configuration and represents
the hours at which upstream capacity is expected to be easier to obtain. The system SHALL
NOT derive that window from observed traffic.

If an import becomes due while its off-peak window is already in progress, it SHALL be
retried within that window rather than waiting for the next one.

#### Scenario: First retry is minutes away

- **WHEN** an import is queued for the first time
- **THEN** its due time is a few minutes in the future, not seconds
- **AND** the delay includes a random jitter

#### Scenario: Backoff grows with each attempt

- **WHEN** a queued import fails transiently again
- **THEN** its next due time is further out than the previous delay
- **AND** the attempt count is increased by one

#### Scenario: Long backoff becomes an off-peak slot

- **WHEN** the computed backoff delay exceeds the configured threshold
- **THEN** the due time is set inside the next configured off-peak window
- **AND** the delay is not extended beyond that window's start by backoff alone

#### Scenario: Queued during the off-peak window

- **WHEN** an import is parked to an off-peak slot while that window is currently open
- **THEN** it is scheduled inside the current window instead of the following day's

### Requirement: The queue gives up after a bounded number of attempts or a bounded age

A queued import SHALL be abandoned when either the configured maximum number of attempts
is reached or the configured maximum age since it was first accepted has passed,
whichever comes first. An abandoned import SHALL be marked failed, SHALL state in its
stored error that the automatic retries were exhausted, and SHALL produce exactly one
final notification asking the person to share the source again.

#### Scenario: Attempt limit reached

- **WHEN** a queued import fails transiently on its last permitted attempt
- **THEN** it is marked failed
- **AND** the person receives one final "Import fehlgeschlagen" notification stating that
  the automatic retries were exhausted

#### Scenario: Age limit reached

- **WHEN** a queued import comes due after the configured maximum age has passed
- **THEN** it is abandoned without a further upstream call

#### Scenario: Retry succeeds

- **WHEN** a queued import succeeds on a retry
- **THEN** it is marked done
- **AND** the person receives the normal "Rezept angelegt" notification with the link

### Requirement: Queued file imports keep their uploaded content

The content of an uploaded photo or PDF SHALL be retained for as long as its import is
queued, so that the retry does not need the person to upload it again. Retained content
SHALL be deleted as soon as the import reaches a terminal outcome - done, failed
permanently, or abandoned - and SHALL NOT be retained for any import that is not queued.

The total size of retained content SHALL be bounded by configuration. When accepting a
new queue entry would exceed that bound, the import SHALL fail permanently with a
notification rather than displacing content that another queued import still needs.

#### Scenario: Photo import hits an overloaded model

- **WHEN** a photo import fails because the LLM provider is at capacity
- **THEN** the uploaded bytes are retained with the queue entry
- **AND** the retry reads a recipe from those bytes without a new upload

#### Scenario: Content removed after success

- **WHEN** a queued file import succeeds on a retry
- **THEN** the retained content is deleted

#### Scenario: Content removed after give-up

- **WHEN** a queued file import is abandoned
- **THEN** the retained content is deleted

#### Scenario: Retention budget exhausted

- **WHEN** retaining the content of a new queue entry would exceed the configured total
  size bound
- **THEN** that import fails permanently with a notification
- **AND** the content of already queued imports is left untouched

### Requirement: A queued import is visible to the person

When an import is queued, the person SHALL receive one notification stating that the
import was not lost and will be retried automatically, naming the reason in the same
plain wording used elsewhere (for example the model being at capacity). No import is
parked silently.

A subsequent successful retry SHALL send the normal success notification, and an
abandoned import SHALL send the final failure notification. An import SHALL NOT send a
notification for each individual retry attempt.

#### Scenario: Notification on queueing

- **WHEN** an import is queued because the model is at capacity
- **THEN** exactly one notification is sent saying the import will be retried
  automatically later

#### Scenario: No notification per attempt

- **WHEN** a queued import fails transiently three times before succeeding
- **THEN** the person receives the queueing notification and the success notification
- **AND** no notification for the second or third attempt

### Requirement: The queue survives a restart

Queue entries, their attempt counts, and their due times SHALL be stored durably. After a
restart, queued imports SHALL still be retried at their due times, and any entry whose due
time has already passed SHALL be retried promptly after startup.

A URL import that was being processed when the service stopped SHALL keep its current
behaviour of being resumed at startup. A file import that was being processed when the
service stopped SHALL be resumed if its content is retained with a queue entry, and SHALL
otherwise keep its current behaviour of being marked failed with a request to share the
file again.

#### Scenario: Restart with a pending due time

- **WHEN** the service restarts while an import is queued for a due time in the future
- **THEN** the import is retried at that due time

#### Scenario: Restart after the due time passed

- **WHEN** the service restarts and a queued import was already due during the downtime
- **THEN** it is retried shortly after startup

#### Scenario: Restart with retained file content

- **WHEN** the service restarts while a file import is queued with retained content
- **THEN** the import is retried from the retained content
- **AND** the person is not asked to share the file again

### Requirement: Retries do not multiply imports or consume the rate limit twice

The hourly rate limit SHALL count accepted imports, not retry attempts: a retry of an
already accepted import SHALL NOT be counted again and SHALL NOT be rejected by the rate
limit.

Sharing the same source again while it is queued SHALL NOT create a second queue entry and
SHALL NOT start a parallel import of that source. A queued import that later succeeds SHALL
result in exactly one recipe in Mealie.

#### Scenario: Retry under an exhausted rate limit

- **WHEN** a queued import becomes due while the hourly limit is already exhausted by
  other imports
- **THEN** the retry runs
- **AND** the hourly counter is not increased by it

#### Scenario: Same URL shared again while queued

- **WHEN** a person shares a URL that is currently queued for retry
- **THEN** no second queue entry is created
- **AND** no second import of that URL runs

#### Scenario: One recipe per source

- **WHEN** an import is retried several times and finally succeeds
- **THEN** exactly one recipe exists in Mealie for that source
