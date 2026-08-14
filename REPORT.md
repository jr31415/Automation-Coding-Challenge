## Architecture

For my architecture, I made a split repo: ```mock-app/``` contains the banking app, whereas ```automation/``` contains the python system. For perception, Playwright was used, every target on the page was given a ```ref``` ID within an accessibility tree, rather than feeding a screenshot, which has three benefits, being faster execution, lower token cost, and gives the LLM an anchor even when the page looks odd. 

The discovery loop follows a path of first giving the page state to the LLM, the LLM then makes a decision on what to do, and then said task gets executed. The whole history of execution is passed to the LLM each time, and it has a bounded run length of 25 steps, ensuring that the job is done consisely, and any loops that the LLM finds itself in do not run indefinitely. 

Claude Sonnet 5 was chosen for this task. Rather than using a frontier model like Opus or Fable, or a lightweight model like Haiku, Sonnet was picked to balance both cost/speed with accuracy. Before starting, the user logs in (instead of the LLM doing the login phase), saving tokens and allowing more steps within the bounded window.

## Artifact schema

Both inputs and outputs are typed within the schema, to ensure that the value requested or retrieved are expected. When looking for a target, a ```TargetLocator``` object is passed, which is an ordered fallback chain for what to look for (RoleLocator — ARIA role -> TextLocator — Text -> ```LabelForLocator``` (```<tr><td>Label</td><td>Value</td></tr> pattern```) — value cells do not have stable text since they change per user, so it looks for surrounding objects -> StructuralLocator — tag + position). These get checked in order until one element matches, as role is stable because it is an element for a user, whereas tag and position are arbitrary. Additionally, there is a ```reasoning``` field, which makes the artifact reviewable for humans, explaining why the LLM was looking for what it was looking for (The LLM never sees this field). ```successCondition```/```knownOutcomes``` are checkpoint assertions (rather than a naïve assumption that there was success because nothing was thrown). Whereas ```successCondition``` simply answers if the artifact found what it was looking for, knownOutcomes can encode what was, or was not found (e.g. "member_not_found"). One caveat to note as that this is not populated automatically and must be done so by a person, therefore it is empty right now.

## Determinism and error handling

The result of a run will be ```Success``` / ```BusinessOutcome``` / ```Failure(kind=...)```. Letting the user know why there was a failure rather than there simply was a failure is important so that failures can be more readily addressed (such as ```locator_not_found```, ```action_error```, ```checkpoint_failed```, ```session_expired``` etc). Replay is deterministic because locator resolution requires exactly one match, and every param template is rendered from the caller's actual inputs before anything is evaluated. Retries only happen when the locator cannot find anything (e.g. the page hasn't loaded), anything thrown will immediately fail the run, as retrying something that was thrown just delays a broken page rather than fixing anything. Session-timeouts are detected by the URL pattern (instead of just an "element not found"). 

## Heterogeneity and multi-tenant

The seam is is at the locator, not at the actions, so extending this to desktop applications means a new locator not a new schema. For apps with hostile markup, ```LabelForLocator``` handles these cases (the mock app is a good example of this). The approach I took can be reused for a multi-tenant setup, by having specific tenants target specific URLS.

## Escalation and handoff
