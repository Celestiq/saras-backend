# app/api/routes/pdf.py
from fastapi import APIRouter, Depends, status, HTTPException
from supabase import Client

from app.db.supabase import get_supabase
from app.services.pdf_service import PDFService
from app.domain.models import PDFGenerationRequest, PDFGenerationResponse, PDFInfoResponse
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/pdf", tags=["pdf"])

def get_pdf_service(supabase: Client = Depends(get_supabase)) -> PDFService:
    """Dependency to provide a PDFService instance."""
    return PDFService(supabase)

@router.post("/generate", status_code=status.HTTP_200_OK, response_model=PDFGenerationResponse, summary="Generate PDF from book chapters")
def generate_pdf(
    request: PDFGenerationRequest,
    service: PDFService = Depends(get_pdf_service)
):
    """
    Generates a PDF from book chapters and uploads it to storage.
    
    - **book_id**: The ID of the book to generate PDF for
    - **book_title**: The title of the book (optional, defaults to "My Book")
    """
    log.info(f"Generating PDF for book_id: {request.book_id} with title: {request.book_title}")
    
    try:
        result = service.create_book_pdf(
            book_id=request.book_id,
            book_title=request.book_title
        )
        
        log.info(f"PDF generated successfully for book {request.book_id}")
        return result
        
    except HTTPException as e:
        log.error(f"Failed to generate PDF for book {request.book_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error while generating PDF for book {request.book_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the PDF"
        )

@router.post("/generate-local", status_code=status.HTTP_200_OK, response_model=PDFGenerationResponse, summary="Generate PDF and save locally")
def generate_pdf_locally(
    request: PDFGenerationRequest,
    service: PDFService = Depends(get_pdf_service)
):
    """
    Generates a PDF from book chapters and saves it locally.
    
    - **book_id**: The ID of the book to generate PDF for
    - **book_title**: The title of the book (optional, defaults to "My Book")
    - **output_path**: Optional custom path for the output file
    """
    log.info(f"Generating local PDF for book_id: {request.book_id} with title: {request.book_title}")
    
    try:
        result = service.create_book_pdf_locally(
            book_id=request.book_id,
            book_title=request.book_title,
            output_path=request.output_path
        )
        
        log.info(f"Local PDF generated successfully for book {request.book_id}")
        return result
        
    except HTTPException as e:
        log.error(f"Failed to generate local PDF for book {request.book_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error while generating local PDF for book {request.book_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the local PDF"
        )

@router.get("/info/{book_id}", status_code=status.HTTP_200_OK, response_model=PDFInfoResponse, summary="Get PDF generation info for a book")
def get_pdf_info(
    book_id: str,
    service: PDFService = Depends(get_pdf_service)
):
    """
    Gets information about a book's chapters without generating PDF.
    
    - **book_id**: The ID of the book to get info for
    """
    log.info(f"Getting PDF info for book_id: {book_id}")
    
    try:
        result = service.get_pdf_info(book_id=book_id)
        
        log.info(f"PDF info retrieved successfully for book {book_id}")
        return result
        
    except HTTPException as e:
        log.error(f"Failed to get PDF info for book {book_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error while getting PDF info for book {book_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while getting PDF info"
        )

@router.get("/health", status_code=status.HTTP_200_OK, summary="Check PDF service health")
def pdf_health_check(service: PDFService = Depends(get_pdf_service)):
    """
    Checks if the PDF service is properly configured and can access Supabase.
    """
    log.info("Performing PDF service health check")
    
    try:
        # Try to access Supabase to verify connection
        # This is a simple check - in a real scenario you might want to do a more specific test
        supabase_client = service.sb
        if supabase_client:
            log.info("PDF service health check passed")
            return {
                "status": "healthy",
                "message": "PDF service is properly configured and can access Supabase",
                "service": "pdf_generation"
            }
        else:
            log.error("PDF service health check failed - no Supabase client")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PDF service is not properly configured"
            )
    except HTTPException as e:
        log.error(f"PDF service health check failed: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error during PDF service health check: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PDF service health check failed"
        )
