# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes.health import router as health_router
from app.api.routes.wishes import router as wishes_router
from app.api.routes.chapters import router as chapters_router
from app.api.routes.cart import router as cart_router
from app.api.routes.auth import router as auth_router
from app.api.routes.users import router as user_router
from app.api.routes.orders import router as orders_router
from app.api.routes.html_conversion import router as html_conversion_router
from app.api.routes.paypal import router as paypal_router
from app.api.routes.credits import router as credits_router
from app.api.routes.cashfree import router as cashfree_router
from app.api.routes.email import router as email_router
from app.api.routes.pdf import router as pdf_router
from app.api.routes.subscriptions import router as subscriptions_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.orchestrator import router as orchestrator_router

def create_app() -> FastAPI:
    app = FastAPI(title="Saras Backend", version="0.1.0", redirect_slashes=False)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(wishes_router)
    app.include_router(chapters_router)
    app.include_router(cart_router)
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(orders_router)
    app.include_router(html_conversion_router)
    app.include_router(paypal_router)
    app.include_router(credits_router)
    app.include_router(cashfree_router)
    app.include_router(email_router)
    app.include_router(pdf_router)
    app.include_router(subscriptions_router)
    app.include_router(webhooks_router)
    app.include_router(tasks_router)
    app.include_router(orchestrator_router)
    return app

app = create_app()