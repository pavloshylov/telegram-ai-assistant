"""Settings from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    bot_token: str
    vertical: str = "realty"  # realty | shop
    llm_provider: str = "demo"  # demo (offline, scripted) | gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_model: str = ""  # tried after the first failed attempt (503, timeout); empty = same model
    gemini_timeout_s: float = 30.0  # per request
    # USD per 1M tokens, for the admin cost report (defaults: Gemini 3.5 Flash-Lite list price, Nov 2026)
    price_input_per_m: float = 0.30
    price_output_per_m: float = 2.50
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    database_url: str = "sqlite+aiosqlite:///data/assistant.db"
    data_dir: Path = Path("data")
    history_messages: int = 10  # previous messages sent to the model
    rate_limit_per_hour: int = 30  # protects the API budget from one noisy user
    rag_top_k: int = 4
    rag_min_score: float = 0.30  # hybrid score; off-topic questions score < 0.26 on the demo knowledge bases
    # --- website widget (python -m app.web)
    allowed_origins: tuple[str, ...] = ()  # sites allowed to embed the widget, e.g. https://www.example.com
    admin_api_key: str = ""  # enables /api/admin/* (sent as X-Admin-Key); empty = admin API off
    ip_rate_limit_per_hour: int = 120  # per client IP, on top of the per-visitor limit (visitors can rotate ids)
    # --- WhatsApp Cloud API channel (python -m app.whatsapp)
    whatsapp_token: str = ""  # access token (temporary on the test number, system-user token in production)
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""  # any string you also type into the App Dashboard
    whatsapp_app_secret: str = ""  # signs every webhook POST (X-Hub-Signature-256)
    whatsapp_graph_version: str = "v27.0"
    whatsapp_show_sources: bool = False  # True: keep [n] markers and a "Sources" line (off: chat reads like a person)
    whatsapp_api_base: str = "https://graph.facebook.com"  # tests point this at a fake

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load_settings(env_file: str | Path = ".env", require_bot_token: bool = True) -> Settings:
    """`require_bot_token=False` is for the website widget, which does not need Telegram."""
    env = {**_read_env_file(Path(env_file)), **os.environ}
    token = env.get("BOT_TOKEN", "").strip()
    if not token and require_bot_token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")
    provider = env.get("LLM_PROVIDER", "demo").strip().lower()
    if provider not in {"demo", "gemini"}:
        raise SystemExit("LLM_PROVIDER must be demo or gemini")
    if provider == "gemini" and not env.get("GEMINI_API_KEY"):
        raise SystemExit("LLM_PROVIDER=gemini needs GEMINI_API_KEY")
    vertical = env.get("VERTICAL", "realty").strip().lower()
    if vertical not in {"realty", "shop"}:
        raise SystemExit("VERTICAL must be realty or shop")
    defaults = Settings(bot_token="")
    return Settings(
        bot_token=token,
        vertical=vertical,
        llm_provider=provider,
        gemini_api_key=env.get("GEMINI_API_KEY", ""),
        gemini_model=env.get("GEMINI_MODEL") or defaults.gemini_model,
        gemini_fallback_model=env.get("GEMINI_FALLBACK_MODEL", "").strip(),
        gemini_timeout_s=float(env.get("GEMINI_TIMEOUT_S") or defaults.gemini_timeout_s),
        price_input_per_m=float(env.get("PRICE_INPUT_PER_M") or defaults.price_input_per_m),
        price_output_per_m=float(env.get("PRICE_OUTPUT_PER_M") or defaults.price_output_per_m),
        admin_ids=frozenset(int(x) for x in env.get("ADMIN_IDS", "").replace(";", ",").split(",") if x.strip()),
        database_url=env.get("DATABASE_URL") or defaults.database_url,
        data_dir=Path(env.get("DATA_DIR") or defaults.data_dir),
        rate_limit_per_hour=int(env.get("RATE_LIMIT_PER_HOUR") or defaults.rate_limit_per_hour),
        allowed_origins=tuple(
            o.strip().rstrip("/") for o in env.get("WIDGET_ALLOWED_ORIGINS", "").replace(";", ",").split(",") if o.strip()
        ),
        admin_api_key=env.get("ADMIN_API_KEY", "").strip(),
        ip_rate_limit_per_hour=int(env.get("IP_RATE_LIMIT_PER_HOUR") or defaults.ip_rate_limit_per_hour),
        whatsapp_token=env.get("WHATSAPP_TOKEN", "").strip(),
        whatsapp_phone_number_id=env.get("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        whatsapp_verify_token=env.get("WHATSAPP_VERIFY_TOKEN", "").strip(),
        whatsapp_app_secret=env.get("WHATSAPP_APP_SECRET", "").strip(),
        whatsapp_show_sources=env.get("WHATSAPP_SHOW_SOURCES", "").strip().lower() in {"1", "true", "yes"},
        whatsapp_graph_version=env.get("WHATSAPP_GRAPH_VERSION") or defaults.whatsapp_graph_version,
        whatsapp_api_base=(env.get("WHATSAPP_API_BASE") or defaults.whatsapp_api_base).rstrip("/"),
    )
