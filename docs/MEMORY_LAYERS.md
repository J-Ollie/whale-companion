# Memory v1.5: selective layers and progressive recall

This update adapts the useful ideas in a user-supplied TMC 0.4.0 reference:
selective channels, short indexes before detailed reads, and bounded injection.
It independently extends the existing Python plugin; it does not install TMC,
copy its role card, or introduce its multi-step model compression pipeline.

## Retrieval

`companion_memory_recall` accepts a `category`:

| Category | Contents |
| --- | --- |
| `all` | Existing supported facts, events, continuity and conversation observations |
| `facts` | Structured stable facts (`semantic`) |
| `plans` | Structured plans (`plan`), with existing historical/validity rules |
| `events` | Evidence-linked conversation episodes |
| `conversation` | Recorded original statements |

Use keywords to find a topic. For `facts`, `plans` and `events`, an empty query
lists a recent directory first; a missing directory does not trigger an LLM
rewrite. All/raw-conversation searches require keywords. Category selection
never selects a different contact and never grants permission.

Results are short index cards containing IDs, previews and evidence references
or source offsets. Use `companion_memory_read` to verify and page through the
source. The existing identity, friend-status, sleep, suppression, four-call,
1,800-byte per reply and 4,200-byte per event limits remain in force.

Kind, type, validity, suppression, already-shown IDs and dates are filtered before
the candidate limit. Automatic fact retrieval searches the full indexed corpus
instead of first selecting the newest 2,000 facts. Related episodes are retrieved
through their index and links instead of scanning only 100 recent quote bundles.
Query-only cleanup removes common recall boilerplate without altering source
messages or discarding negation/cancellation language.

## Context layers

Personal core, relevant facts, related events, working topics, previous exchanges
and impressions each have a bounded share. The outer 2,600 UTF-8-byte cap remains
authoritative. Unrelated working topics and impressions are omitted; previous
exchanges are not reinjected when the current request already has history.
Impressions remain provisional evidence-linked material, not personality rules.

No new database tables, evidence rewrites, free-form global person summaries,
new browsing/group permissions, or additional model calls are introduced. Complete
sanitized sources and the [v1.4 portable backup](MEMORY_COMPLETENESS.md) remain.

Validation: 134 targeted tests with synthetic events and mocked models, including
old-record recall, category isolation, suppression, routing, budgets and paging.
This is not a claim about recall accuracy across all real conversations.
