"""Protocol version contract for cross-validator semantics (spec §6).

Anything consensus-critical — scoring constants, validation rules, wire
formats — must live inside WATCHED_PATHS so changing it trips the digest
and forces a deliberate version bump. tests/quality_gates enforces that
watched code never imports unwatched modules (storage mechanics excepted),
so constants cannot drift outside the contract again.
"""

from pathlib import Path

ACTIVATED_VERSION_REGISTRY_DIGEST = (
    "780b8636c26b6442586d9e73f5a18af81b7d2318af33865d5da84f0c1bbd0cba"
)
ACTIVATED_VERSION_HISTORY_DIGEST = (
    "c529e9225d197b2e6cfcd4711390992d882c4903e7f0f355d81c62a992315821"
)

WATCHED_PATHS = (
    Path("endure/assessment"),
    Path("endure/protocol"),
    Path("endure/aggregation"),
    Path("endure/scoring"),
    Path("endure/publication"),
)

# Previous accepted protocol snapshot. When watched paths change, promote the
# current values into the previous fields, then write the new digest and bump
# the current version key.
PREVIOUS_VERSION_KEY = 28
PREVIOUS_VERSION_DIGEST = (
    "05da1df37dc67de435d0954d9b102be45922c6956822643ff1dcc7a892176e26"
)

# Production serving status and CURRENT_VERSION_KEY stay unchanged until R6.
# Alpha Risk R6 is the semantic activation: risk.v1.subnet_alpha is served by
# default, live mainnet Alpha market data is wired for live runtimes, and scoring
# constants/tier thresholds are frozen. This is the single authorized R6 key bump.
# 21: absence-aware scoring (docs/specs/2026-07-20-scoring-fairness-deltas.md
# §1). The Alpha Risk scoring set becomes active-EMA hotkeys ∪ current
# submitters: an expected miner with no accepted reveal now receives a zero
# observation per resolved coordinate, so skipping a round decays the EMA
# instead of preserving it, and a dormant miner can no longer earn from a
# stale EMA indefinitely. Confirmed-deregistered hotkeys (two consecutive
# metagraph resync generations absent) and fully-decayed absent hotkeys
# (every scored-coordinate EMA below 0.01) are archived out of the active
# state; re-registration cold-starts from the zero prior. Emitted weights are
# additionally filtered to currently-registered hotkeys; consensus weighting
# still uses the unfiltered blended scores, so a bundle accepted while
# registered keeps its earned consensus weight. This changes active
# risk.v1.subnet_alpha scored semantics, so it is an authorized key bump,
# not a fold. The §2 weight-emission audit trail (storage + base hook) is
# validator-internal observability and folds into this key.
# 22: confirmed-deregistration archival is marker-based and deferred. A horizon
# is archived only after every unfinished round has persisted its resolution
# marker for that horizon; incomplete passes cannot make it eligible. Each
# archival attempt is contained per horizon and retries next tick after a
# storage failure, while successful archival emits fresh weights. This changes
# watched scoring behavior and requires a lockstep key cutover.
# The digest is a build-time plus lockstep-deploy invariant: all validators and
# miners must run the same watched tree for a key. The wire check carries only
# the integer version key.
# 23: reveal admission is bounded per round and miner, while an exact retry of
# an already accepted reveal is idempotent. This prevents repeated validation
# work and unbounded in-memory/disk pressure without changing accepted payload
# semantics. Validators serving this key must deploy the handler together.
# 24: accepted-reveal idempotency validates the request protocol key before
# acknowledging the persisted verdict, and reveal-attempt admission is stored
# atomically with the submission so the budget survives validator restarts.
# Validators serving this key must deploy the handler and migration together.
# 25: weight-emission intents bind the recorded protocol key into their
# canonical identity, and the activated-version registry prevents any consumed
# key or digest from being reassigned. Validators must cut over in lockstep.
# The public-release cleanup folds into this key: it removes an unused schema
# lookup helper and refreshes package documentation without changing wire,
# validation, aggregation, scoring, or publication behavior.
# 26: structural remediation of the validator runtime replaces mutable
# scoring-orchestrator state with an immutable per-call resolution context,
# makes SQLite the sole emission score authority, and extracts vertical
# dispatch to delete schema-ID branches below composition roots. Wire formats,
# schemas, migrations, and scoring and aggregation math are unchanged. This is
# an internal refactor, so validator and miner lockstep is required only by the
# existing digest-pinning mechanism.
# 27: deleting the legacy bank-risk reference vertical changed the watched tree.
# Wire formats, canonical serialization, commit binding, validation verdicts,
# aggregation, and scoring math are unchanged; only the digest moves.
# 28: the RC1 correctness cutover bounds live sampling to the deterministic
# resolution window, settles missing archive timestamps after grace, prevents
# later joiners from receiving retroactive absence observations, and persists
# the next-commit-close publication embargo. Validators and miners must deploy
# this scoring and publication contract in lockstep.
# 29: the RC1 public-cleanup cutover removes stale source citations and an
# obsolete reference-miner roadmap promise, and consensus publication now skips
# an accepted bundle that no longer parses (the policy scoring already applied)
# instead of leaving the round open forever. Wire formats, scoring math, and
# aggregation of parseable bundles are unchanged; validators and miners still
# cut over in lockstep under the new key.
#
# 20: Alpha Risk calendar-independent daily rounds
# (docs/specs/2026-07-18-alpha-risk-24x7-rounds.md). The served Alpha schema
# now uses a fixed 20:00 UTC anchor, including weekends and NYSE holidays, so
# validators and miners must deploy this schedule change in lockstep.
# The 2026-07-21 determinism remediation folds into key 20: scoring reads the
# exact bundle set snapshotted at consensus publication (restart-safe), both
# resolution window boundaries are deterministic finalized-chain timestamp
# lookups (start: first block at/after reveal_close; end: last block at/before
# reveal_close + horizon) instead of head-relative or 12s/block estimates, and
# transient archive unavailability — including the boundary lookups themselves —
# defers horizon resolution (24h grace before voiding) instead of permanently
# voiding it. Wire formats, scoring math, and tier thresholds are unchanged;
# resolved values become identical across independent validators.
CURRENT_VERSION_KEY = 29
CURRENT_VERSION_DIGEST = (
    "2a3054efe774950d35f951100dd4e9912de8091cf8371814d516e75f99c562c9"
)
