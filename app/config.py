"""Runtime configuration. Every integration is optional: when its key is
missing the feature degrades to a local fallback instead of crashing."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    app_name: str = "Deskline"
    base_url: str = "http://localhost:8000"  # public HTTPS URL Vapi/Stripe can reach
    secret_key: str = "dev-secret-change-me"
    database_url: str = "postgresql+psycopg://deskline:deskline@localhost:5432/deskline"
    admin_email: str = "admin@example.com"
    admin_password: str = "change-me-now"
    secure_cookies: bool = False  # set true behind HTTPS

    # Text LLM for agent building, summaries, writing tools, chat widget.
    # Any OpenAI-compatible endpoint: OpenAI, OpenRouter, Ollama, vLLM, LM Studio.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    llm_timeout_seconds: float = 60.0

    # Vapi (voice orchestration: telephony bridge + STT + LLM + TTS)
    vapi_api_key: str = ""
    vapi_base_url: str = "https://api.vapi.ai"
    vapi_webhook_token: str = ""  # header secret Vapi sends back; derived from secret_key if blank
    vapi_model_provider: str = "openai"  # or "custom-llm" with vapi_custom_llm_url
    vapi_model: str = "gpt-4.1-mini"
    vapi_custom_llm_url: str = ""
    vapi_voice_provider: str = "vapi"
    vapi_voice_id: str = "Clara"
    vapi_transcriber_model: str = "nova-3"
    vapi_max_call_seconds: int = 900

    # Twilio (SMS; numbers can also be imported into Vapi)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Email: console | smtp | resend
    email_provider: str = "console"
    email_from: str = "Deskline <no-reply@example.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    resend_api_key: str = ""
    # Replies to outreach go here; forward that mailbox to /webhooks/inbound-email/<token> so replies are captured.
    inbound_reply_to: str = ""

    # Stripe (client billing)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    currency: str = "usd"

    # Lead finder + crawling
    google_places_api_key: str = ""
    firecrawl_api_key: str = ""
    crawl_max_pages: int = 12

    # Margin estimates when the provider hasn't reported a cost yet
    est_cost_per_minute_cents: float = 15.0

    @property
    def webhook_token(self) -> str:
        if self.vapi_webhook_token:
            return self.vapi_webhook_token
        import hashlib

        return hashlib.sha256(f"vapi:{self.secret_key}".encode()).hexdigest()[:40]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
