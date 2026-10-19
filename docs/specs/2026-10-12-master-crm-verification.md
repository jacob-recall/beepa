# Master CRM implementation verification

Local implementation verified October 12, 2026. This records the current working
tree, not a deployed or published release. The feature uses the existing master
workspace, shared contacts, normal conversation viewer, proposal/approval paths,
source send guards and receipt renderer. No separate chat renderer, credential
store, address book, or native send path was added to the master.

## Requirements and evidence

| Requirement | Implemented behavior | Verification |
| --- | --- | --- |
| Central lists spanning users | One master-owned list, explicit per-person account binding; shared contacts only | `crm_members.test.js`, `master_crm_view.test.js`, actual-app browser |
| Shared person, separate histories | Explicitly link account bindings; review freezes one sender/person; history filters never reassign sender | Model, member, compose and browser cases |
| Compact rows and regular conversations | Compact desktop rows, expandable locations/manage controls, mobile reflow; row and exact-message links enter the ordinary viewer | Actual-app Chromium; actual-main binding/context tests |
| Direct on existing conversations | Source resolves exact one-to-one rooms including Private; prepare → canonical membership → commit; current per-room acknowledgment required | Control/policy adversarial tests and isolated scenario29 |
| Manual privacy and permission lifetime | Later manual Private wins; removal/archive cancel unattempted work and retain applied Direct; restore needs an explicit warned fresh setup | Coordinator47, control/policy/activation adversarial cases and scenario29 |
| Catalog maintenance | Unchanged shared identity can refresh its catalog version without regranting Direct; old cancellation scope stays immutable | Batch74, source batch/approval regressions |
| Personalize and review | Separate message/route per recipient, sender/channel groups, included/excluded reasons/counts; duplicate destinations warn; unresolved placeholders block | Compose38, batch74, actual-app browser |
| Durable central submission | Canonical reviewed parent precedes per-account staging; source validates actual proposal IDs and owns durable accepted work | Model/store/batch adversarial tests, scenario29 and browser replay |
| Approval for new conversations | Typed self-authored approval; unchanged existing dispatcher; no master native start-chat request | Approval/adversarial tests, scenarios12/29 and master import/write checks |
| No duplicate send on recovery | Immutable item IDs and attempt checkpoints; exact retries reconcile; ambiguous native outcomes are never blindly resent | Source batch/approval/control crash and replay tests; scenario29 |
| Message receipts | Shared gray/blue renderer; exact target/part evidence only; reply never promotes delivery/read | Receipt/native tests, compose38, scenario29 and browser gray→blue checks |
| Most-recent-unanswered reply | Source scope is account/generation/native conversation; exact native order, part deduplication, durable correction audit | Tracking13 plus15 adversarial cases, native history22, scenario29 |
| Other bridge providers | Qualified observed-history adapter; exact owner/login/portal/native mapping; audited runtime image digest; unknown schema/image/order reconciles | Provider20 plus20 independent cases; actual PostgreSQL joins/queries for all six providers |
| Central and per-person tracking | Cross-list history including archived/removed memberships; sender/list/message/response filters; exact message/reply navigation | Compose38, view47, actual-main checks and actual-app browser |
| Stale UI and authentication | Logout/session fences, original reviewed hashes, current account generation, hostile metadata/result rejection, request-order fencing | Store/session/binding/batch/view adversarial cases |
| Release compatibility and operator instructions | Candidate0.3.0, state2/readers1–2, wire1; preserve credentials/CLI/journals; staged per-account opt-in | Real isolated updater compatibility test and design specification operator runbook |

Three independent model roles implemented or reviewed source behavior, adversarial
boundaries, and independently authored UI/SQL tests. Their findings produced fixes
for malformed acknowledgments, post-cancel rejection, restored-list grants,
catalog-version maintenance, stale history overwriting newer receipts, approval
revocation after checkpoints, and deleted observed-history reply evidence.

## Executed checks

- `tests/run.sh`: all123 discovered test scripts passed, plus122,429 consent and
  8,112 invite conformance vectors. The runner uses the configured interpreter;
  bare system Python is not a substitute for the installed dependency environment.
- Isolated `7_read_only_manager`, `12_contact_share_and_propose`, and
  `28_crm_metadata`: passed through `tests/integration/run.sh`.
- Expanded `29_crm_ordered_controls`: passed with real disposable Synapse and
  PostgreSQL. Includes approval/edit/replay, exact read/reply mappings, Private
  masking, disabled cancellation, approved new-chat follow-on Direct and six
  provider SQL paths. Disposable cleanup completed; live stack untouched.
- `tests/integration/master_crm_browser.cjs`: passed against actual application
  modules with every request intercepted; unknown/live requests prohibited.
  Includes cross-account review/history, exact conversation context, cancellation
  and reload, and a populated390px mobile list. Screenshots were visually checked.
- `git diff --check`: clean.

## Capability boundaries and handoff

Receipt evidence depends on provider support. Bridge-observed response history is
labeled as such; it is not a promise of complete upstream history. Ties, gaps,
pending backfill, changed evidence, an unaudited image, and tracked messages beyond
the bounded provider window remain reconciling. Previously linked uncertain replies
stay qualified, and neither a reply nor command acceptance supplies read evidence.

CRM identifier-only new-chat approval currently supports iMessage. Other providers
need an existing bridged conversation and are visibly excluded until one is ready.
The signed iMessage CLI cannot identify the first message created by its start-chat
command; that item keeps its honest acceptance/unknown outcome without fabricated
receipt or reply IDs. The exact new portal can activate the original Direct grant
for later ordinary sends. Native first-send correlation is tracked in
`pm_mng-b55` and requires a deliberately adopted trusted signed upstream change.

Revocation prevents new CRM sending and current message navigation. CRM retains
its own reviewed outbound intent and stable history references; it does not copy
incoming conversation bodies. Revocation cannot retract information already
received through the existing Matrix history behavior. Private scopes publish no
fresh tracking evidence and clear navigation references.

No code was committed or pushed, no deployment occurred, and no live outreach was
sent during this work. The per-account rollout is tracked in `pm_mng-pvs`; it needs
an authorized committed release and the staged instructions in
[the implementation specification](2026-10-12-master-crm-design.md). The prior
self-chat receipt test is not authorization for a CRM campaign. The separate
cadence/follow-up roadmap remains `pm_mng-7p1`.


## Suggestion lifecycle verification — 2026-10-13

The task adds independent queued suggestions, durable accepted/declined history,
source-confirmed decision ownership, and terminal Direct refusals. Validation:

- 125 discovered unit scripts plus 122,429 consent and 8,112 invite conformance
  vectors. Logs: `/tmp/beepa-suggestions-final-unit.log`.
- Actual user modules in intercepted Chromium: queue three suggestions; accept
  one, decline another; verify one remains; refresh the page and retain both
  decisions; exclude a refused Direct send from the queue. Exactly one synthetic
  conversation write and no unexpected requests. Screenshot:
  `/tmp/beepa-suggestion-queue.png`.
- Actual master CRM browser suite: cross-account lists, normal conversation
  navigation, batch progress, distinct user approval versus source acceptance,
  receipts/reply links, cancellation/reload and mobile layout.
- All 29 disposable integration scenarios exercised. The initial full run passed
  28; scenario 10's old restart assertion expected one proposal after the test
  was expanded to create three. That assertion was corrected and scenario 10
  passed, including exact accepted/declined relay, real filtered pagination and
  manager status-forgery rejection (403). Final focused 10/14/29 rerun passed
  with source decision claims, Direct auto-send and CRM gate refusal.
  Logs: `/tmp/beepa-suggestions-integration.log`,
  `/tmp/beepa-suggestions-roundtrip.log`,
  `/tmp/beepa-suggestions-claims-integration.log`.
- Decision unit coverage includes losing-session refusal, durable claim recovery,
  missing decision-state recovery, wrong sender, master-identity mismatch,
  disconnected service and retry after a failed status publish.

Tests use synthetic accounts and disposable servers. No live external messages,
production deployment, commit or push was performed for this change.
