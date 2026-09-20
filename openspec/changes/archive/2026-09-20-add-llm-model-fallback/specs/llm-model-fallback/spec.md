# Spec Delta

## Purpose

Keeps recipe extraction working when the text model the service normally uses has run out
of quota, by moving the same call down an ordered chain of alternative models, and keeps
that chain current by adopting newer models the provider starts offering.

## ADDED Requirements

### Requirement: Ordered model chain for the text stage

The service SHALL use an ordered list of text models, newest first. The first entry is
the preferred model; the remaining entries are fallbacks used only when an earlier entry
is unavailable. The list SHALL be configurable, and its configured default SHALL be
`gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash, gemini-3.5-flash`. A configuration
naming a single model SHALL be accepted and SHALL behave as a chain of length one.

Every call of the text stage - recipe extraction from text, recipe extraction from
images, and the naming stage - SHALL use the same chain and the same chain state.

#### Scenario: Preferred model answers

- **WHEN** an import triggers a text-stage call and the first model in the chain answers
  successfully
- **THEN** that answer is used and no other model is contacted

#### Scenario: Single configured model

- **WHEN** the chain is configured with exactly one model
- **THEN** the service behaves exactly as before this change: one retry on a transient
  status, then a failure

### Requirement: A quota rejection moves the call to the next model

When a model answers HTTP 429, the service SHALL retry the same call once on the same
model after the existing short delay. If that second answer is also 429, the service
SHALL mark that model as exhausted and send the same, unchanged call to the next model in
the chain, continuing down the chain on the same rule.

A request answered by a fallback model SHALL be indistinguishable in its result from one
answered by the preferred model: same enforced schema, same prompt, same validation and
the same single schema retry.

#### Scenario: Preferred model out of quota

- **WHEN** the first model answers 429 twice in a row for the same call
- **THEN** the call is re-sent to the second model in the chain and, if that model
  answers successfully, the import completes normally with no user-visible difference

#### Scenario: Several models out of quota

- **WHEN** the first two models each answer 429 twice
- **THEN** the call is re-sent to the third model

#### Scenario: Quota rejection during the schema retry

- **WHEN** a model answered successfully but its answer failed schema validation, and the
  single schema retry is rejected with 429 twice
- **THEN** the schema retry is sent to the next model in the chain, and the schema retry
  budget for that call is still exactly one

### Requirement: A model the provider does not know is skipped

When a model is rejected because the provider does not know it - typically HTTP 404, or a
400 naming an unknown model - the service SHALL mark that model unavailable for the rest
of the process lifetime and SHALL send the same call to the next model in the chain. A
configured or defaulted model name that does not exist at the provider SHALL therefore
never make an import fail while another model in the chain can answer.

#### Scenario: Chain head does not exist

- **WHEN** the first model in the chain is rejected as unknown by the provider
- **THEN** the rejection is logged once, the call is sent to the next model, and later
  calls in the same process skip the unknown model without contacting it

#### Scenario: No model in the chain exists

- **WHEN** every model in the chain is rejected as unknown
- **THEN** the import fails with the existing wording for a failed recipe extraction and
  the log names every rejected model

### Requirement: A model under load moves the call on without a cooldown

When a model answers HTTP 502, 503 or 504, the service SHALL retry the same call once on
the same model after the existing short delay, and on a second such answer SHALL send the
same call to the next model in the chain. These statuses describe one model's load rather
than an exhausted quota, so the model SHALL NOT be marked exhausted and SHALL be eligible
again for the very next call.

HTTP 500 SHALL keep its current handling unchanged: one retry on the same model, then
failure with the existing overload wording. It names no model-specific condition, so it
is not read as a reason to try a different model.

When the chain is used up by these statuses, the failure SHALL be the same error class
and the same wording as for an exhausted chain.

#### Scenario: Head under load, fallback free

- **WHEN** the first model answers 503 twice for the same call and the second model
  answers successfully
- **THEN** the import completes normally, and the first model is contacted again on the
  next call without waiting for a cooldown

#### Scenario: Every model under load

- **WHEN** every model in the chain answers 503 twice
- **THEN** the import fails with the existing "Das Sprachmodell ist gerade überlastet.
  Bitte später erneut teilen." wording

#### Scenario: Internal error

- **WHEN** a model answers 500 twice for the same call
- **THEN** no other model is contacted and the import fails with the existing overload
  wording

### Requirement: Exhausted models are skipped for a cooldown period

A model marked exhausted SHALL be skipped by later calls until a configurable cooldown
has passed, so that a used-up quota costs at most one rejected call rather than one per
import. The cooldown SHALL default to one hour. After it passes, the model SHALL be
eligible again in its configured chain position.

The memory of exhausted models SHALL be held in the running service only; a restart SHALL
start again at the head of the chain.

#### Scenario: Next import skips the exhausted model

- **WHEN** the first model was marked exhausted by an earlier import and the cooldown has
  not passed
- **THEN** the next import's text-stage call goes directly to the next eligible model
  without contacting the exhausted one

#### Scenario: Cooldown expires

- **WHEN** the cooldown of an exhausted model has passed
- **THEN** the following text-stage call contacts that model again in its configured
  position

#### Scenario: Restart clears the memory

- **WHEN** the service restarts after a model was marked exhausted
- **THEN** the first text-stage call after the restart starts at the head of the chain

### Requirement: An exhausted chain fails with the existing wording

When every model in the chain is exhausted or has been tried for the current call, the
text stage SHALL fail with the same error class and the same user-facing wording used
today for an overloaded model. This change SHALL NOT introduce a new user-facing failure
text or a new failure mode.

#### Scenario: Every model out of quota

- **WHEN** all models in the chain answer 429
- **THEN** the import fails with "Das Sprachmodell ist gerade überlastet. Bitte später
  erneut teilen." and the notification is the one sent today for that case

### Requirement: Newer models are discovered from the provider

When automatic discovery is enabled, the service SHALL read the provider's list of
available models at startup and then on a configurable interval, keep the entries
matching a configurable model-name pattern, and order them by their version, newest
first. The pattern SHALL default to the `gemini-<major>.<minor>-flash` family of the
configured provider.

A discovered model whose version is newer than the current head of the chain SHALL be
placed in front of the chain. Discovery SHALL NOT remove configured models from the
chain, and SHALL NOT reorder them relative to each other.

A failure to read the model list SHALL be logged and otherwise ignored: the chain in use
stays as it is, and no import fails because discovery failed.

#### Scenario: Provider offers a newer model

- **WHEN** the provider's model list contains a model of the configured family whose
  version is higher than the current chain head
- **THEN** that model becomes the new chain head and the previous head becomes the first
  fallback

#### Scenario: Provider offers nothing newer

- **WHEN** the provider's model list contains no model newer than the current head
- **THEN** the chain is left unchanged

#### Scenario: Model list unreachable

- **WHEN** the request for the provider's model list fails or returns an unreadable body
- **THEN** the failure is logged, the chain stays as it is, and imports continue

#### Scenario: Configured model the provider does not list

- **WHEN** a configured chain entry does not appear in the provider's model list
- **THEN** the entry is logged as unavailable and skipped while the list says so, and the
  chain is not otherwise changed

### Requirement: A discovered model is probed before it is used

Before a newly discovered model is used for an import, the service SHALL verify it with
one minimal request that enforces a JSON schema the same way the text stage does. Only a
model that answers successfully and schema-conformant SHALL be adopted. A model that
fails the probe SHALL be rejected, logged, and not probed again until the next discovery
run.

#### Scenario: Probe succeeds

- **WHEN** a newly discovered model answers the probe with a schema-conformant response
- **THEN** it is adopted as the chain head and used for subsequent imports

#### Scenario: Probe fails

- **WHEN** a newly discovered model rejects the probe, times out, or answers something
  that does not match the enforced schema
- **THEN** it is not added to the chain, the rejection is logged, and imports keep using
  the previous chain

### Requirement: Adoption of a new model is reported

When the head of the chain changes because a new model was discovered and probed
successfully, the service SHALL log the change with the old and the new model name and
SHALL send exactly one push notification about it. Repeated discovery runs that find the
same already-adopted model SHALL NOT notify again.

#### Scenario: First adoption

- **WHEN** a newly discovered model passes its probe and becomes the chain head
- **THEN** one push notification names the previous and the new model

#### Scenario: Unchanged head on a later run

- **WHEN** a later discovery run finds the same model that is already the chain head
- **THEN** no notification is sent

### Requirement: Discovery can be switched off

Automatic discovery SHALL be switchable off by configuration. With discovery off, the
chain SHALL consist only of the configured models in their configured order, no request
for the provider's model list SHALL be made, and no model SHALL be probed.

#### Scenario: Discovery disabled

- **WHEN** automatic discovery is disabled and the service starts
- **THEN** no model list is requested, no probe is sent, and the chain is exactly the
  configured one

### Requirement: The image stage is unaffected

The model chain, the quota switch, the cooldown and the discovery SHALL apply to the text
stage only. The image stage SHALL keep using its configured single model and its own
provider, and SHALL be unchanged by this capability.

#### Scenario: Text model exhausted while an image is generated

- **WHEN** every text model is exhausted
- **THEN** the image stage still uses its configured image model unchanged
