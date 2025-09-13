# app/api/routes/email.py
import os
from fastapi import APIRouter, Depends, status, HTTPException, Request
from supabase import create_client, Client

# Import the new service
from app.services.email_sending_service import DailyEmailService
from app.services.email_service import EmailService
from app.domain.models import EmailRequest, EmailResponse
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/email", tags=["email"])

# --- Existing and New Dependencies ---

def get_email_service() -> EmailService:
    """Dependency to provide a standard EmailService instance."""
    return EmailService()

def get_supabase_client() -> Client:
    """Dependency to provide a Supabase client instance."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supabase client is not configured on the server."
        )
    return create_client(url, key)

def get_daily_email_service(
    supabase: Client = Depends(get_supabase_client)
) -> DailyEmailService:
    """Dependency to provide the DailyEmailService instance."""
    return DailyEmailService(supabase)

async def verify_job_secret(request: Request):
    """Dependency to verify the secret token for running jobs."""
    secret = os.environ.get("JOB_RUNNER_SECRET")
    if not secret:
        log.error("CRITICAL: JOB_RUNNER_SECRET is not set in environment variables.")
        raise HTTPException(status_code=500, detail="Endpoint not configured.")

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        log.warning("Job runner endpoint called without authorization header.")
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    token = auth_header.split(" ")[1]
    if token != secret:
        log.warning("Job runner endpoint called with an invalid token.")
        raise HTTPException(status_code=403, detail="Invalid credentials")

# --- Existing Endpoints ---

@router.post("/send", status_code=status.HTTP_200_OK, response_model=EmailResponse, summary="Send an email")
def send_email(
    email_request: EmailRequest,
    service: EmailService = Depends(get_email_service)
):
    """
    Sends an email with optional attachment using Zoho Mail API.
    
    - **recipient_email**: Email address of the recipient
    - **subject**: Email subject (1-200 characters)
    - **html_content**: HTML content of the email
    - **attachment_path**: Optional path to file to attach
    """
    log.info(f"Sending email to {email_request.recipient_email} with subject: {email_request.subject}")
    
    try:
        if email_request.attachment_path:
            result = service.send_email_with_attachment(
                recipient_email=email_request.recipient_email,
                subject=email_request.subject,
                html_content=email_request.html_content,
                attachment_path=email_request.attachment_path
            )
        else:
            result = service.send_simple_email(
                recipient_email=email_request.recipient_email,
                subject=email_request.subject,
                html_content=email_request.html_content
            )
        
        log.info(f"Email sent successfully to {email_request.recipient_email}")
        return result
        
    except HTTPException as e:
        log.error(f"Failed to send email to {email_request.recipient_email}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error while sending email to {email_request.recipient_email}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while sending the email"
        )

@router.get("/health", status_code=status.HTTP_200_OK, summary="Check email service health")
def email_health_check(service: EmailService = Depends(get_email_service)):
    """
    Checks if the email service is properly configured and can authenticate with Zoho.
    """
    log.info("Performing email service health check")
    
    try:
        access_token = service.get_access_token()
        if access_token:
            log.info("Email service health check passed")
            return {
                "status": "healthy",
                "message": "Email service is properly configured and can authenticate with Zoho",
                "service": "zoho_mail"
            }
        else:
            log.error("Email service health check failed - no access token received")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Email service is not properly configured"
            )
    except HTTPException as e:
        log.error(f"Email service health check failed: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error during email service health check: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email service health check failed"
        )

# --- New Endpoint for Daily Email Job ---

@router.post(
    "/run-daily-email-job",
    status_code=status.HTTP_200_OK,
    summary="Trigger the daily subscription email workflow",
    dependencies=[Depends(verify_job_secret)]
)
def run_daily_email_job(
    service: DailyEmailService = Depends(get_daily_email_service)
):
    """
    Executes the daily workflow to send chapter emails to all due subscribers.
    
    This endpoint is protected and should only be called by a trusted scheduler
    (e.g., a Supabase Edge Function with a secret key).
    """
    log.info("Received request to run the daily email job.")
    try:
        result = service.run_daily_email_workflow()
        log.info(f"Daily email job completed successfully. Result: {result}")
        return result
    except Exception as e:
        log.error(f"An unexpected error occurred during the daily email job: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Daily email job failed: {str(e)}"
        )