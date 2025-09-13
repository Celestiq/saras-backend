"""
API routes for HTML conversion service.

This module provides endpoints to convert markdown chapters to styled HTML.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from pydantic import BaseModel
from typing import Dict, Any

from app.api.deps import current_user
from app.db.supabase import get_supabase
from app.services.html_conversion_service import HTMLConversionService
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/html-conversion", tags=["html-conversion"])


class ConvertChapterRequest(BaseModel):
    """Request model for converting a single chapter to HTML."""
    chapter_id: str


class ConvertBookRequest(BaseModel):
    """Request model for converting all chapters in a book to HTML."""
    book_id: str


class ConversionResponse(BaseModel):
    """Response model for conversion operations."""
    status: str
    message: str
    data: Dict[str, Any]


def get_html_conversion_service(
    supabase: Client = Depends(get_supabase)
) -> HTMLConversionService:
    """Dependency to provide an HTMLConversionService instance."""
    return HTMLConversionService(supabase=supabase)


@router.post("/chapter", response_model=ConversionResponse, summary="Convert a single chapter to HTML")
async def convert_chapter_to_html(
    request: ConvertChapterRequest,
    user: dict = Depends(current_user),
    service: HTMLConversionService = Depends(get_html_conversion_service)
):
    """
    Convert a single chapter from markdown to styled HTML.
    
    This endpoint:
    1. Fetches the chapter from the database
    2. Downloads the markdown content from storage
    3. Converts it to styled HTML using the antique newspaper theme
    4. Uploads the HTML to the 'html' bucket
    5. Updates the html_path column in the chapters table
    
    Args:
        request: Contains the chapter_id to convert
        user: Current authenticated user
        service: HTML conversion service instance
        
    Returns:
        Conversion result with status and details
    """
    try:
        log.info(f"User {user.get('id')} requested HTML conversion for chapter_id: {request.chapter_id}")
        
        result = service.convert_chapter_to_html(request.chapter_id)
        
        return ConversionResponse(
            status="success",
            message=f"Successfully converted chapter {request.chapter_id} to HTML",
            data=result
        )
        
    except HTTPException as e:
        log.error(f"HTTP error converting chapter {request.chapter_id}: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error converting chapter {request.chapter_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to convert chapter: {str(e)}"
        )


@router.post("/book", response_model=ConversionResponse, summary="Convert all chapters in a book to HTML")
async def convert_book_chapters_to_html(
    request: ConvertBookRequest,
    user: dict = Depends(current_user),
    service: HTMLConversionService = Depends(get_html_conversion_service)
):
    """
    Convert all chapters in a book from markdown to styled HTML.
    
    This endpoint:
    1. Fetches all chapters for the given book_id
    2. For each chapter, downloads markdown content and converts to HTML
    3. Uploads HTML files to the 'html' bucket with proper path structure
    4. Updates html_path columns in the chapters table
    
    Args:
        request: Contains the book_id to convert chapters for
        user: Current authenticated user
        service: HTML conversion service instance
        
    Returns:
        Conversion results with summary statistics
    """
    try:
        log.info(f"User {user.get('id')} requested HTML conversion for all chapters in book_id: {request.book_id}")
        
        result = service.convert_book_chapters_to_html(request.book_id)
        
        return ConversionResponse(
            status="success",
            message=f"Successfully converted {result['converted_chapters']} chapters for book {request.book_id}",
            data=result
        )
        
    except HTTPException as e:
        log.error(f"HTTP error converting book {request.book_id}: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error converting book {request.book_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to convert book chapters: {str(e)}"
        )


@router.get("/health", summary="Health check for HTML conversion service")
async def health_check():
    """
    Health check endpoint for the HTML conversion service.
    
    Returns:
        Simple health status
    """
    return {"status": "healthy", "service": "html-conversion"}
