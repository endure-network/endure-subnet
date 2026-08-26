# Security policy

Report vulnerabilities privately to `hello@endure.network`. Include a concise
description, affected release/protocol version, safe reproduction steps, and
redacted logs. Do not post vulnerability details in public issues.

Never commit, publish, attach to an issue, or include in logs a mnemonic, seed
phrase, coldkey, hotkey JSON, private key, wallet archive, API token,
credential-bearing endpoint, or backup. Rotate any material accidentally
exposed before contacting us. Non-sensitive miner and validator support belongs
in the repository issue forms.

The team-operated testnet soak has one narrow exception: its Coolify compose
configuration may receive a hotkey-only wallet archive through the platform's
secret-backed `WALLETS_TAR_B64` environment setting. The initializer rejects a
coldkey secret before extraction. This mechanism accepts that a testnet hotkey
is visible to privileged host and container administrators; it is not supported
for third-party operators or mainnet, and the archive must never be attached to
a public issue, report, or build artifact.
