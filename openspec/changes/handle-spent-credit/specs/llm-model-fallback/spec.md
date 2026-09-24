# Spec Delta

## ADDED Requirements

### Requirement: A spent credit moves the call to the next model

When a model rejects a call with a status naming a spent credit, spent prepayment or
disabled billing, the service SHALL mark that model as exhausted and send the same,
unchanged call to the next model in the chain, continuing down the chain on the same rule.

Unlike a quota rejection, this case SHALL NOT be retried on the same model first: a
depleted credit does not refill within the short retry delay, so the second attempt would
only be a second rejection and a second wait.

When every model in the chain has been rejected this way, the call SHALL fail with the
same error class the service uses for an exhausted chain today, so that the import is
parked by the retry queue and retried later rather than discarded. The user SHALL be told
that the attempt will be repeated automatically, never that the source contained no
recipe.

#### Scenario: Preferred model reports a spent credit

- **WHEN** the first model rejects the call because the account's credit is spent
- **THEN** the call goes to the next model in the chain without a second attempt on the
  first, and a successful answer there completes the import with no user-visible
  difference

#### Scenario: The whole account is out of credit

- **WHEN** every model in the chain rejects the call because the credit is spent
- **THEN** the import is parked with a due time and retried automatically, and the
  notification says the attempt will be repeated rather than reporting a missing recipe

#### Scenario: Credit returns before the import is retried

- **WHEN** a parked import comes due after the credit has been topped up, or the provider
  serves the account again for any other reason
- **THEN** that import completes on its own, without anyone re-sharing the source

#### Scenario: Rejection that does not name a spent credit

- **WHEN** a model rejects a call with a status that names a reason of this call, such as
  a malformed request or a missing credential
- **THEN** the call fails as it does today, without walking the chain, because the next
  model would reject it the same way

## MODIFIED Requirements

### Requirement: The image stage is unaffected

The model chain, the quota switch, the cooldown and the discovery of this capability SHALL
apply to the text stage only. The image stage SHALL keep its own ordered chain of
candidates, its own cooldown and its own provider forms, as the `recipe-image` capability
describes, and SHALL be unchanged by this capability. Shared recognition of provider
answers, such as the wording that identifies a spent quota or an unknown model name, MAY
be defined once and used by both stages; that sharing SHALL NOT give either stage a say
over the other's order, cooldown or state.

#### Scenario: Text model exhausted while an image is generated

- **WHEN** every text model is exhausted
- **THEN** the image stage still walks its own chain, unaffected by the state of the text
  chain

#### Scenario: Both stages meet the same rejection

- **WHEN** the provider rejects both a text call and an image call because the credit is
  spent
- **THEN** each stage marks its own candidate in its own state, with its own cooldown, and
  neither skips a candidate on account of the other
