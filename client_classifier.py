"""
client_classifier.py — 6-tier classification for MCP / HTTP clients.

Wired into mcp_server.py to enrich each `mcp_call` event with:
  - tier (1-6)
  - client_name (e.g. "Claude Desktop", "Smithery", "Unknown: ...")
  - client_type ("real_user" | "ai_client" | "agent_framework" | "directory_bot" | "generic_http" | "suspicious" | "unknown")
  - details dict (real_ip, ua, has_payment, etc.)

Designed so callers can use either:
  - `classify(request, recent_payments_24h)` — full path with header reads
  - `classify_user_agent(ua)` — pure-string fast path for tests / batch backfill

No I/O, no async — safe to call inline in the request path.
"""
import re


# === 6-tier definitions =====================================================
TIER_1_REAL_USER = 1        # paid in last 24h
TIER_2_AI_CLIENT = 2        # validated AI client (Claude Desktop, Cursor, …)
TIER_3_AGENT_FRAMEWORK = 3  # LangChain, AutoGen, n8n, …
TIER_4_DIRECTORY_BOT = 4    # Smithery, Glama, mcp.so, …
TIER_5_GENERIC_HTTP = 5     # curl, python-requests, …
TIER_6_SUSPICIOUS = 6       # no UA / empty / pattern-burst


# === Pattern registry =======================================================
# Lookup is case-insensitive substring match against User-Agent. Order does
# not matter inside a category; categories are tried in priority order
# (ai_client → agent_framework → directory_bot → generic_http).
KNOWN_PATTERNS = {
    "ai_client": {
        "Claude Desktop": ["claude/", "claude-desktop", "claudemcp", "anthropic-mcp"],
        "Cursor": ["cursor/", "cursor-mcp"],
        "Cline (VSCode)": ["cline/", "cline-mcp"],
        "Continue.dev": ["continue/", "continue-dev"],
        "Zed Editor": ["zed/"],
        "Codeium": ["codeium/"],
        "Windsurf": ["windsurf/"],
        "GitHub Copilot": ["github-copilot/", "copilot-mcp"],
        "AWS Bedrock AgentCore": ["agentcore", "bedrock-agent"],
        "ChatGPT": ["chatgpt/", "openai-mcp"],
        "Gemini": ["gemini/", "google-mcp"],
        "Grok": ["grok/", "xai-mcp"],
        "Perplexity": ["perplexity/", "perplexity-mcp"],
    },
    "agent_framework": {
        "LangChain": ["langchain/", "langchain-mcp"],
        "LlamaIndex": ["llama-index/", "llamaindex"],
        "AutoGen": ["autogen/", "pyautogen"],
        "CrewAI": ["crewai/"],
        "Semantic Kernel": ["semantic-kernel/"],
        "n8n": ["n8n/"],
        "Make.com": ["make.com/", "integromat"],
        "Zapier": ["zapier/"],
        "MCP Inspector": ["mcp-inspector"],
    },
    "directory_bot": {
        "Smithery": ["smithery", "smithery.ai"],
        "Glama": ["glama", "glama.ai"],
        "mcp.so": ["mcp.so"],
        "PulseMCP": ["pulsemcp", "pulse-mcp"],
        "ClawHub": ["clawhub"],
        "MCP Registry": ["mcp-registry", "modelcontextprotocol"],
        "xpay.tools": ["xpay"],
        "Awesome MCP": ["awesome-mcp"],
        "x402scan": ["x402scan"],
        "MPPScan": ["mppscan"],
        "Cowork": ["cowork"],
    },
    "generic_http": {
        "Python requests": ["python-requests/", "requests/"],
        "httpx": ["httpx/"],
        "aiohttp": ["aiohttp/"],
        "Node fetch": ["node-fetch", "undici"],
        "axios": ["axios/"],
        "curl": ["curl/"],
        "Go HTTP": ["go-http-client/"],
        "Java HTTP": ["java/", "okhttp/", "apache-httpclient/"],
        "Postman": ["postmanruntime/"],
        "Insomnia": ["insomnia/"],
    },
}

# Type → tier map used when a match is found
_TYPE_TO_TIER = {
    "ai_client": TIER_2_AI_CLIENT,
    "agent_framework": TIER_3_AGENT_FRAMEWORK,
    "directory_bot": TIER_4_DIRECTORY_BOT,
    "generic_http": TIER_5_GENERIC_HTTP,
}

# Smithery-style discovery bots authenticate via query params even when UA is
# generic httpx/requests; treat presence of either key as a Smithery probe.
SMITHERY_QUERY_KEYS = ("api_key", "profile")


# === Helpers =================================================================
def extract_real_ip(request) -> str:
    """Cloudflare → origin IP. Falls back to X-Forwarded-For, then client host."""
    if request is None:
        return "-"
    headers = getattr(request, "headers", None) or {}
    cf = (headers.get("cf-connecting-ip") if hasattr(headers, "get") else None)
    if cf:
        return cf.strip()
    xff = headers.get("x-forwarded-for") if hasattr(headers, "get") else None
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    if client is not None and getattr(client, "host", None):
        return client.host
    return "-"


def classify_user_agent(user_agent: str) -> tuple:
    """Pure-string classification. Returns (tier, client_name, client_type) or
    (None, None, None) if no known pattern matched."""
    if not user_agent:
        return (None, None, None)
    ua_low = user_agent.lower()
    # Priority order: ai_client > agent_framework > directory_bot > generic_http
    for client_type in ("ai_client", "agent_framework", "directory_bot", "generic_http"):
        for name, sigs in KNOWN_PATTERNS[client_type].items():
            for sig in sigs:
                if sig.lower() in ua_low:
                    return (_TYPE_TO_TIER[client_type], name, client_type)
    return (None, None, None)


def classify(request, recent_payments_24h: int = 0) -> tuple:
    """Full classification using request headers + payment history.

    Returns:
      (tier, client_name, client_type, details_dict)

    details_dict keys: user_agent, real_ip, has_payment_header,
                       is_smithery_query, recent_payment_count_24h,
                       origin, referer, country (best-effort).
    """
    headers = getattr(request, "headers", None)
    def _h(name, default=""):
        if headers is None:
            return default
        try:
            v = headers.get(name)
            return (v or default).strip() if isinstance(v, str) else default
        except Exception:
            return default

    user_agent = _h("user-agent")
    real_ip = extract_real_ip(request)
    has_payment = bool(_h("x-payment") or _h("X-PAYMENT"))
    origin = _h("origin")
    referer = _h("referer")
    country = _h("cf-ipcountry")

    # Query-param sniff for Smithery-style auth probes
    qp = {}
    try:
        qp = dict(getattr(request, "query_params", {}) or {})
    except Exception:
        qp = {}
    is_smithery_query = any(k in qp for k in SMITHERY_QUERY_KEYS)

    details = {
        "user_agent": user_agent[:300],
        "real_ip": real_ip,
        "has_payment_header": has_payment,
        "is_smithery_query": is_smithery_query,
        "recent_payment_count_24h": int(recent_payments_24h or 0),
        "origin": origin[:200],
        "referer": referer[:200],
        "country": country,
    }

    # Tier 4: Smithery query-param probe (even with generic UA)
    if is_smithery_query:
        return (TIER_4_DIRECTORY_BOT, "Smithery", "directory_bot", details)

    # Tier 1: paid in last 24h or carrying X-PAYMENT now
    if has_payment or (recent_payments_24h or 0) > 0:
        tier_match = classify_user_agent(user_agent)
        name = tier_match[1] if tier_match[1] else "Anonymous Payer"
        return (TIER_1_REAL_USER, name, "real_user", details)

    # Tier 2-5: known pattern match
    tier_match = classify_user_agent(user_agent)
    if tier_match[0] is not None:
        return (tier_match[0], tier_match[1], tier_match[2], details)

    # Tier 6: suspicious — empty / missing UA
    if not user_agent or len(user_agent) < 3:
        return (TIER_6_SUSPICIOUS, "Unknown (no UA)", "suspicious", details)

    # Unknown but well-formed UA — Tier 5 with snippet preview
    snippet = user_agent[:60]
    return (TIER_5_GENERIC_HTTP, f"Unknown: {snippet}", "unknown", details)
