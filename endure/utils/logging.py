import re
from urllib.parse import urlsplit


def safe_endpoint_label(endpoint: object) -> str:
    """Return an endpoint label that cannot expose credentials or URL details."""
    raw = str(endpoint or "").strip()
    if not raw:
        return "<unset>"

    has_scheme = "://" in raw
    try:
        parsed = urlsplit(raw if has_scheme else f"//{raw}")
    except ValueError:
        return "<configured>"
    hostname = parsed.hostname
    if hostname is None:
        return "<configured>"

    host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError:
        return "<configured>"
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme.lower()}://{authority}" if has_scheme else authority


_ENDPOINT_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://\S+")
_USERINFO_RE = re.compile(r"[A-Za-z0-9._~%+\-]+:[^\s@/]+@[A-Za-z0-9._\-]+(?::\d+)?")


def safe_error(exc: object) -> str:
    """Return an exception message with embedded endpoint credentials redacted.

    HTTP and RPC client exceptions frequently embed the endpoint — and any
    inline credentials — either as a full ``scheme://user:pass@host`` URL or as
    a bare ``user:pass@host`` authority. Redact both before the text can reach a
    log line.
    """
    text = _ENDPOINT_URL_RE.sub("<redacted-endpoint>", str(exc))
    return _USERINFO_RE.sub("<redacted-endpoint>", text)


# C0/C1 controls plus Unicode line/paragraph separators (U+2028/U+2029) and
# bidi controls (U+061C ALM, U+200E/U+200F, U+202A–U+202E, U+2066–U+2069):
# all can forge log line breaks or visually reorder rendered log text.
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]+"
)
_REMOTE_TEXT_LIMIT = 200


def safe_remote_text(value: object, *, max_length: int = _REMOTE_TEXT_LIMIT) -> str:
    """Bound peer-supplied text for logging: redact credentials, collapse
    control characters (newline/ANSI/Unicode-separator/bidi log-injection
    vectors), and truncate.
    """
    text = _CONTROL_CHARS_RE.sub(" ", safe_error(value))
    if len(text) > max_length:
        return text[:max_length] + "…"
    return text


def startup_config_summary(config: object, *, neuron_type: str) -> dict[str, object]:
    """Build a strict allowlist of non-sensitive startup configuration."""
    summary: dict[str, object] = {"neuron_type": neuron_type}
    for key in ("netuid", "mock"):
        value = getattr(config, key, None)
        if value is not None:
            summary[key] = value

    sections = {
        "runtime": ("mode",),
        "neuron": ("name",),
        "wallet": ("name", "hotkey"),
        "endure": ("active_schema", "serving_stage", "api_port"),
    }
    for section_name, keys in sections.items():
        section = getattr(config, section_name, None)
        if section is None:
            continue
        for key in keys:
            value = getattr(section, key, None)
            if value is not None:
                summary[f"{section_name}.{key}"] = value

    subtensor = getattr(config, "subtensor", None)
    chain_endpoint = getattr(subtensor, "chain_endpoint", None)
    if not chain_endpoint:
        chain_endpoint = getattr(subtensor, "network", None)
    if callable(getattr(config, "is_set", None)):
        # A bittensor Config: log the endpoint the SDK will actually dial (a
        # network alias outranks an unused chain_endpoint default).
        from bittensor.core.subtensor import Subtensor

        chain_endpoint, _network = Subtensor.setup_config(None, config)
    summary["subtensor.endpoint"] = safe_endpoint_label(chain_endpoint)

    endure = getattr(config, "endure", None)
    api_host = getattr(endure, "api_host", None)
    if api_host:
        summary["endure.api_host"] = safe_endpoint_label(api_host)
    market_endpoint = getattr(endure, "market_data_endpoint", None)
    if market_endpoint:
        summary["endure.market_data_endpoint"] = safe_endpoint_label(market_endpoint)
    return summary
