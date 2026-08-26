# Forge Lending V1 (Stage-1) — scope & salvage map (2026-06-19)

> **Status: dormant.** Reference only — not the served product; superseded as
> the activation target by **Alpha Risk V1**
> (`docs/specs/2026-07-06-alpha-risk-v1-scope.md`). The
> `lending.v1.subnet_asset` schema stays dormant in-tree: registered
> `registered_unserved`, with its code and tests in place. Reactivation path:
> Forge V2 derives lending parameters from Alpha
> risk consensus once Forge's confirmed parameter ranges arrive. Unqualified
> "spec §" citations in lending code resolve against this doc.

> **KRE status:** the former bank-risk vertical, runtime, fixtures, and five
> dedicated tables have been removed. KRE references below describe historical
> design lineage only and are not implementation or operator guidance.

This document records the earlier pivot from KRE bank-risk forecasts to
**Forge lending-parameter assessment**. It records the locked V1 decisions,
folds in Forge maintainer feedback (2026-06-19) that
reshapes the schema toward Forge's real contract surface, maps reuse vs.
net-new, and sequences the build. Decisions D1–D7 are locked below.

## Historical rationale
Forge was designed as a native TAO money market using Alpha-token collateral
and miner-recommended risk parameters. It is not an active Endure product or
operator path. The KRE first loop was the deliberate
**Stage-1 testnet vehicle** that built and hardened the reusable spine
(commit-reveal, EMA, weighted-median consensus, the schema registry, the 3-axis
type system, the resolver data-path pattern) on an easy-to-score domain. V1
redirects that spine at Forge. Very little is discarded.

## Forge maintainer requirements (authoritative — protocol team, 2026-06-19)
- **Forge is a Venus Core Pool fork** (LTV/factor-based, Diamond proxy) on
  Bittensor. Alpha is **collateral-only today** (CF 0.25 / LT 0.35 / LI 1.08;
  borrowing disabled). Schema is **factor/LTV-shaped, not ratio-shaped.**
- **Forge prices Alpha itself** via a native oracle stack (`BittensorNativeAlphaFeed`
  → `AlphaRiskPriceFeed` → `WAlphaPriceFeed`). The network does not own the price;
  `safe_asset_price` is a **borrow-safe alternate/secondary** feed.
- **Publish-recommendations, humans execute.** High-blast-radius knobs (CF/LT
  activation, oracle swaps, close factor, listing, pauses) stay under
  admin/guardian. Bounded auto-execution is the 6–12mo roadmap. **V1 publish-only.**
- **Three output classes; be deliberate about what is *scored*:** (1) judgment
  recommendations = the miner competition (V1); (2) stress analytics
  (liquidation-capacity-at-size, expected bad-debt) = **simulation = Stage-2**, the
  signal Forge most wants; (3) fast minutes-level telemetry = **not** a
  commit-reveal subnet output.
- **Trust = live shadow + stress performance, not backtests.** Our
  testnet-subnet-on-mainnet-data soak **is** that shadow evidence; design it to
  span a stress episode.

## Locked V1 decisions
1. **Domain:** Forge lending parameters via a schema reshaped to Forge's contract
   surface (below). Universe = whitelisted Alpha-tokens (`SubnetAssetTarget(netuid)`),
   static. Subnet on **testnet**, scoring **mainnet** Alpha data (dress rehearsal).
2. **Scoring model (D2/D4):** **Stage-1, sim-free.** Tier-1 + Tier-2 outputs are
   all *included* in the schema, but scoring weight tracks how credibly each
   grounds without a simulation (see §V1 schema). The bounded liquidation model
   that would rigorously ground caps/LT/LI is **Stage-2**, not V1.
3. **Vertical scope:** Forge remains the only dormant assessment schema; the
   removed bank-risk predecessor is not re-enabled for multi-domain proof.
4. **Cadence:** single uniform round bundling all outputs through the shared
   assessment round model. Per-parameter cadence (§4.2/§4.6) and per-output
   horizons → V1.1.
5. **Data:** mainnet subtensor archive access available; oracle points at a
   mainnet archive node while the subnet runs on testnet (data chain ≠ op chain).
6. **Outputs (D1 + maintainer):** seven snake_case outputs with Forge-native
   semantics, each wrapped in the split envelope (D5). Stage-1 signed payloads use
   deterministic integer wire units below; the read API converts ratio fields to
   Forge 1e18 mantissas for consumers. `market_mode` deferred (D7);
   `critical_collateral_ratio` and `interest_rate` dropped.
7. **Envelope (D5):** miner-submitted forecast fields are signed/hashed;
   current-chain-state + delta are **validator-annotated at publication**, never
   in the commit.

## V1 scored schema (`lending.v1.subnet_asset`, Alpha collateral markets)

**Per-output envelope.** Miner-submitted (signed, hashed, consensus-critical):
`value`, `confidence_bps`, `reason_codes[]`, `horizon_seconds`, `unit`.
Validator-annotated at publication (observed from chain, NOT in the commit):
`current_onchain_value`, `recommended_delta`.

**Wire-unit decision.** Stage 1 signs and hashes integer wire values. Ratio/factor
fields (`collateral_factor`, `liquidation_threshold`) use bps on the wire
(`2500 == 25%`). The Forge-facing read API converts those values to 1e18
mantissas before publication/consumer use. Confidence is always bps.

**Principle: "in the schema" ≠ "scored live" ≠ "published to Forge."** All seven
outputs are defined in the signed lending envelope so miners and validators agree
on the full V1 shape. Only outputs with `scored_live=True` affect miner scoring
in a given release. Public Forge consumption waits for the read-API/publication
gate; registry/config selection alone does not serve lending outputs.

| Output | Stage-1 wire unit | Forge consumer | Stage-1 (sim-free) scoring | Weight |
|---|---|---|---|---|
| `safe_asset_price` (+ derived `source_state`) | quote mantissa (`price_1e18`) | `ResilientOracle` secondary/cross-check feed | **borrow-safe** realized price; asymmetric — overprice **severe**, underprice mild, stale/incoherent = **fail** | full |
| `collateral_factor` | `ltv_bps` | `setCollateralFactor(vToken, cf, lt)` via read-API 1e18 conversion | forward-window retained value − liquidation buffer (§8.1.2) | full |
| `risk_tier` | `ordinal` 1–6 | frontend/governance risk score | deterministic vol/liquidity classifier | full |
| `supply_cap` | `underlying_units` | `setMarketSupplyCaps` | realized pool-depth-minimum × safe-clearance | full |
| `liquidation_threshold` | `ltv_bps`, **LT ≥ CF** | `setCollateralFactor(vToken, cf, lt)` via read-API 1e18 conversion | sim-free **drawdown magnitude + velocity buffer** ("triggered early enough") — **not** consensus-proximity | modest |
| `liquidation_incentive` | `mantissa_1e18` (value ≥ 1e18) | `setLiquidationIncentive` | best sim-free proxy (vol/liquidity heuristic); rigorous grounding is Stage-2 | low |
| `borrow_cap` | `tao_units` exposure ceiling per Alpha market | TAO `setMarketBorrowCaps` within that Alpha's **isolated pool** (see open-q #2) | realized **TAO-denominated liquidation capacity** (Alpha/TAO pool-depth × safe-clearance) | modest |

`source_state` ∈ {normal, upside_guard, downside_observed, invalid, paused} rides
with `safe_asset_price` as a derived coherence flag from mirroring
`AlphaRiskPriceFeed` (distinct from `risk_tier`; not a separate scored output).

**Stage-1 deviation score.** Stage 1 extends the current repo's bounded-linear
score with a full-credit grace band and an asymmetric lenient side. `delta` is
the deviation measured per the output's **deviation mode** (below); `cutoff` is
the total deviation (in the mode's units) where the score reaches zero — it
*includes* the grace band.

```python
if delta <= grace_band:
    score = Decimal("1")
elif delta >= effective_cutoff:
    score = Decimal("0")
else:
    score = Decimal("1") - (delta - grace_band) / (effective_cutoff - grace_band)
```

`effective_cutoff = cutoff` on the aggressive/dangerous side and
`cutoff * lenient_multiplier` on the conservative side. For `collateral_factor`,
too-high CF is aggressive because it under-margins collateral and increases
bad-debt risk.

**Deviation modes (per output).** `delta` is **absolute** (native units) for the
ratio/ordinal outputs — `collateral_factor`, `liquidation_threshold` (ltv bps),
`risk_tier` (ordinal) — where a fixed step is a fixed amount of risk (1 pp of LTV
is the same risk at 25% or 75%, so tolerance must not scale with magnitude). It
is **relative** (normalized to bps-of-target) for the 1e18-scale /
per-asset-varying outputs — `safe_asset_price`, `liquidation_incentive`,
`supply_cap`, `borrow_cap` — so the bands are scale-free; a `target` of 0 (e.g.
zero liquidation capacity) scores 1 only for a 0 submission. An absolute band
cannot work for those (the scale rides at 1e18 or varies per asset).

**Intra-submission invariants** (reject whole submission): all values > 0 except
caps, which may be 0; confidences ≥ per-output floor; `CF ≤ LT ≤ 10000`;
`risk_tier ∈ [1,6]`; caps ≥ 0.

**Dropped/deferred:** `critical_collateral_ratio` (no Forge consumer);
`interest_rate` (immutable kinked IRM, swapped by redeploy — not a live scalar;
a TAO-borrow concern); `market_mode` (D7 — borrowability/lifecycle read on-chain
by the validator); `liquidation_incentive` full grounding → Stage-2.

## Reused vs. net-new (grounded in current code)

### Reused as-is — the spine
`round_engine.py` (uniform round), commit-reveal + canonical encoding,
`endure/scoring/` primitives (bounded-linear deviation, EMA, coverage), the
pinned Decimal context in `endure/scoring/context.py`, the 3-axis registry,
`schema_id`-keyed storage, read API, weight-setting, and all 2026-06
testnet-readiness hardening.

### Net-new — four pillars (dependency order)
1. **Schema wiring** — `SubnetAssetTarget(netuid)` + `LendingContextV1`; static
   whitelist; `ParameterSpec` extension (deviation bands + confidence floor +
   reason-code enum + `horizon_seconds`/`unit`); the split envelope (miner fields
   vs validator-annotated `current_onchain_value`/`recommended_delta`);
   `build_lending_v1_subnet_asset_schema()` (7 outputs, Stage-1 wire units,
   Forge consumer semantics, `CF ≤ LT`).
2. **Chain-oracle layer** (`endure/scoring/oracle/subtensor/`) — realized
   price/liquidity/vol from pinned archive depth, **mirroring Forge's own
   valuation** (`AlphaRiskPriceFeed`/`WAlphaPriceFeed`: spot/moving/TWAP, wAlpha
   exchange rate, rao-per-alpha bounds). Read Forge's oracle contracts as the spec.
   The swing factor.
3. **Asymmetric deviation scoring** — grace-band/cutoff/asymmetry; sim-free
   targets for LT (drawdown+velocity) and caps (pool-depth); low/zero weighting for
   LI and borrow_cap; coverage-as-score factor; reason-code + validator-annotation
   passthrough at the read API. **No simulation engine (D4).**
4. **Observable resolvers** — `collateral_factor` (forward-window retained value
   from entry, not KRE's running-peak drawdown), `safe_asset_price`
   (Forge-coherent borrow-safe price), `risk_tier` (classifier), `supply_cap`
   (pool-depth), `liquidation_threshold` (drawdown+velocity), `liquidation_incentive`
   (vol/liquidity heuristic).

## Build sequence
- **M0 — de-risk the oracle.** Prototype the subtensor price/liquidity oracle
  **mirroring Forge's valuation**; validate bit-identical determinism (§8.1.7) +
  wall-time. The real risk.
- **Pre-registration hardening (before M1):** register a static lending whitelist
  that rejects non-whitelisted `netuid` values, and set product-aware ceilings for
  currently unbounded integer outputs (`safe_asset_price`, `liquidation_incentive`,
  `supply_cap`, `borrow_cap`) before miner submissions are served.
- **M1 — walking skeleton on `collateral_factor`** end-to-end (Tier-1, clean
  observable): schema stub → uniform round → Forge-coherent price → drawdown CF
  observable → asymmetric deviation → publish. Exercises all three risky pillars.
- **M2 — fan out Tier-1:** `safe_asset_price`, `risk_tier`.
- **M3 — Tier-2 sim-free:** `supply_cap` (Alpha-unit pool-depth) and `borrow_cap`
  (TAO-denominated liquidation capacity); `liquidation_threshold` (drawdown+velocity,
  modest); `liquidation_incentive` (low weight).
- **M4 — completeness:** the split envelope + validator annotations
  (`current_onchain_value`/`recommended_delta`), coverage factor, read-API
  serialization for lending outputs.
- **M5 — testnet soak** (the shadow-evidence track) over a window spanning a real
  Alpha/TAO stress episode; reuse the existing soak-gate machinery.

## Explicitly deferred
- **V1.1 (before mainnet):** per-parameter cadence + per-output horizons (§4.2/§4.6);
  calibration (§8.4); **timeliness/regime-anticipation (§8.6)** — Forge's trust
  criterion is "moved before liquidity disappeared," so the soak must evaluate
  stress-period performance even before this is in the scored formula; `market_mode`.
- **Roadmap (the trust-earning Stage-2):** the bounded → full liquidation-capacity
  /slippage model that rigorously grounds `liquidation_threshold`,
  `liquidation_incentive`, and the caps (and makes `safe_asset_price` truly
  *executable*); expected-bad-debt-under-stress; bounded auto-execution adapter;
  real-capital canary ramps.

## Removed predecessor work

The shared commit/reveal, registry, consensus, scoring, and storage spine built
before Forge remains in use. The predecessor's domain schema, fixtures, replay
tools, soak configuration, and dedicated tables have been removed.

## Open questions / risks
1. **Oracle coherence + determinism (§8.1.7)** — observables must match Forge's own
   valuation and be bit-identical across validators. Validated in M0.
2. **`borrow_cap` redefinition — mapping + overlap.** Redefined as a
   **TAO-denominated exposure ceiling per Alpha market** (the quantity governance
   reasons about; now sim-free scoreable via TAO liquidation capacity).
   (a) **Confirm with Forge:** in the shared Core Pool this isn't a single native
   setter (Compound/Venus `borrowCap` is per-borrowed-asset = global TAO); it maps
   to a real knob only if Alpha markets are **isolated per-netuid pools** (then
   it's the TAO `borrowCap` within that pool). (b) `supply_cap` (Alpha units) and
   `borrow_cap` (TAO) ground on the same liquidation-capacity signal — keep both
   (distinct controls: deposit size vs debt exposure) with a *combined* weight, or
   make `borrow_cap` primary and derive `supply_cap`? (c) Confirm
   `liquidation_incentive` low-weight heuristic vs. omit until Stage-2.
3. **`safe_asset_price` oracle wiring** — confirm Forge wires the network feed as a
   `ResilientOracle` secondary/cross-check source and the divergence-acceptance bands.
4. **Forge integration — RESOLVED for V1.** Ad-hoc pull; V1 publishes consensus to
   the existing read API. Tighter contract deferred.
5. **Whitelist** — candidates: mainnet netuids **44, 8** for the realistic soak
   (final set TBD); testnet **288 / 333 / 418** as a pure-testnet fallback.
