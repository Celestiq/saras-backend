from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str
    SUPABASE_JWT_SECRET: str
    OPENAI_API_KEY: str
    PAYPAL_CLIENT_ID: str
    PAYPAL_CLIENT_SECRET: str
    PAYPAL_BASE_URL: str = "https://api-m.sandbox.paypal.com"
    APP_BUCKET_BOOKS: str = "books"
    APP_BUCKET_COVERS: str = "covers"
    APP_BUCKET_COVERS: str = "chapters"
    PAYPAL_PRODUCT_ID: str
    ALLOW_DEV_BYPASS: bool = False
    DEV_USER_ID: str | None = None
    
    # Zoho Mail Configuration
    ZOHO_CLIENT_ID: str
    ZOHO_CLIENT_SECRET: str
    ZOHO_REFRESH_TOKEN: str
    ZOHO_FROM_EMAIL: str = "aayush@mysaras.club"

    model_config = {"env_file": ".env"}

    PLANNING_MODEL: str = "gpt-5-nano"
    CONTENT_MODEL: str = "gpt-5-nano"
    WEB_SEARCH: bool = False
    CONTENT_EFFORT: str = "minimal"     # options: low, medium, high

    JOB_RUNNER_SECRET: str

    BACKEND_URL: str = "https://api.mysaras.club"
    FRONTEND_URL: str = "https://www.mysaras.club"

    CASHFREE_CLIENT_ID: str
    CASHFREE_CLIENT_SECRET: str
    CASHFREE_BASE_URL: str = "https://sandbox.cashfree.com"

settings = Settings()