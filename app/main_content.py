# app/main_content.py - Content Generation Service
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.api.routes.health import router as health_router
from app.api.routes.chapters import router as chapters_router
from app.api.routes.html_conversion import router as html_conversion_router
from app.api.routes.pdf import router as pdf_router
from app.api.routes.tasks import router as tasks_router

# Load environment variables from .env file
load_dotenv()

def create_content_generation_app() -> FastAPI:
    """Create the content generation service application."""
    app = FastAPI(title="Saras Content Generation Service", version="0.1.0", redirect_slashes=False)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    
    # Include routers for content generation functionality
    app.include_router(health_router)
    app.include_router(chapters_router)
    app.include_router(html_conversion_router)
    app.include_router(pdf_router)
    app.include_router(tasks_router)
    
    return app

app = create_content_generation_app()
