# app/api/routes/chapters.py
from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from openai import OpenAI

from app.api.deps import current_user
from app.db.supabase import get_supabase
from app.services.chapter_service import ChapterService
from app.domain.models import GenerateOneRequest, GenerateAllRequest
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/chapters", tags=["chapters"])

def get_openai() -> OpenAI:
    """Dependency to provide an OpenAI client instance."""
    return OpenAI()

def get_chapter_service(
    supabase: Client = Depends(get_supabase),
    openai_client: OpenAI = Depends(get_openai)
) -> ChapterService:
    """Dependency to provide a ChapterService instance."""
    return ChapterService(supabase=supabase, openai_client=openai_client)

@router.post("/generate", summary="Generate a single chapter for a book")
def generate_one(
    body: GenerateOneRequest,
    user: dict = Depends(current_user),
    service: ChapterService = Depends(get_chapter_service)
):
    """
    Generates the content for a single chapter based on its module and topic index
    within the learning roadmap.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} requested to generate chapter idx: {body.idx} for book_id: {body.book_id}")
    try:
        result = service.generate_chapter(
            user_id=user_id,
            wish_id=body.wish_id,
            book_id=body.book_id,
            idx=body.idx,
            module_index=body.module_index,
            topic_index=body.topic_index,
        )
        log.info(f"Successfully generated chapter idx: {body.idx} for book_id: {body.book_id}")
        return result
    except HTTPException as e:
        log.error(f"HTTP error during single chapter generation for book {body.book_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"Unexpected error during single chapter generation for book {body.book_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while generating chapter.")

@router.post("/generate_all", summary="Generate all chapters for a book")
def generate_all(
    body: GenerateAllRequest,
    user: dict = Depends(current_user),
    service: ChapterService = Depends(get_chapter_service)
):
    """
    Triggers the generation of all chapters for a given book based on its roadmap.
    This can be a long-running process.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} requested to generate all chapters for book_id: {body.book_id}")
    try:
        result = service.generate_all_chapters(
            user_id=user_id,
            wish_id=body.wish_id,
            book_id=body.book_id
        )
        log.info(f"Successfully generated all chapters for book_id: {body.book_id}. Count: {result.get('count', 0)}")
        return result
    except HTTPException as e:
        log.error(f"HTTP error during all chapters generation for book {body.book_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"Unexpected error during all chapters generation for book {body.book_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while generating all chapters.")