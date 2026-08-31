# Consuming the Endure Alpha Risk feed

Endure currently operates one public testnet validator HTTP endpoint:

- API: `https://api.testnet.endure.network`
- Bittensor testnet netuid: `504`
- Validator hotkey:
  `5E2bM6DXxyraVJCDjWBcixudbzYXToDnNcsDBB4hoJdCuwTi`

The endpoint is a testnet-alpha service, not a production oracle. There is only
one public validator endpoint today, so no independent-validator quorum exists.

## Discovery boundaries

Miners discover validator **axons** through the Bittensor testnet metagraph for
netuid `504`; axons receive commit/reveal protocol traffic. Consumers do not
use that miner transport. Consumers discover the separately hosted HTTPS read
API above and retrieve signed JSON from it.

Check readiness and the served schema before reading the feed:

```bash
curl --fail https://api.testnet.endure.network/health | jq
curl --fail https://api.testnet.endure.network/schemas | jq
curl --fail https://api.testnet.endure.network/risk/v1/subnets \
  --output risk-feed.json
```

`/health` must report `status: ok`, schema `risk.v1.subnet_alpha`, protocol key
`29`, and an explicit release source revision. `/schemas` reports horizons in
seconds. Alpha Risk uses `432000` (5 days) and `2592000` (30 days).

`source_revision` is attested by whoever built the image, not proven to you.
`content_revision` is derived from the code the validator is running, so you can
check it yourself against a checkout of the attested commit:

```bash
python -m scripts.content_revision
```

Equal values mean the endpoint is running that source. Unequal values mean it is
not, regardless of what `source_revision` claims.

## Verify one feed

From a locked repository checkout:

```bash
python -m verify.risk_feed --file risk-feed.json
```

The utility serializes `payload` as sorted-key, compact UTF-8 JSON using the
same canonicalizer as the protocol, checks `canonical_payload_sha256`, and
verifies `signature_hex` against `signed_by`. It prints the signer, round ID,
and canonical hash.

Successful verification proves only that the named hotkey signed those exact
payload bytes. It does not prove that the signer is independent, the source
data is correct, enough miners participated, or the risk values are fit for a
consumer's use.

## Interpret the payload

- All risk and economic values are encoded as integers or decimal strings;
  never parse them through binary floating point.
- Each consensus cell reports `horizon_seconds`, `median`, `mad`, and
  `n_submitters`.
- `tier` is derived from 30-day consensus medians and may be `unrated` until
  the required resolved history exists.
- `round_id` identifies the newest publication-eligible consensus round. During
  the next-round commit embargo the feed intentionally continues serving the
  prior eligible round.
- A consumer must define its own minimum `n_submitters`, staleness limits,
  failure behavior, and independent data checks.

The canonical field definitions are in the
[Alpha Risk V1 scope](specs/2026-07-06-alpha-risk-v1-scope.md). Current
economic and decentralization limits are disclosed in
[`economic-limitations.md`](economic-limitations.md).
