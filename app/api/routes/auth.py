# app/api/routes/auth.py
from fastapi import APIRouter, Depends, status, HTTPException
from supabase import Client

from app.db.supabase import get_supabase
from app.services.auth_service import AuthService
from app.domain.models import UserCredentials, AuthResponse
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

def get_auth_service(supabase: Client = Depends(get_supabase)) -> AuthService:
    """Dependency to provide an AuthService instance."""
    return AuthService(supabase)

@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=AuthResponse, summary="Register a new user")
def sign_up(
    credentials: UserCredentials,
    service: AuthService = Depends(get_auth_service)
):
    """
    Creates a new user account. Upon successful registration, Supabase sends a
    confirmation email. The user is also logged in and a session is returned.
    """
    log.info(f"Sign-up attempt for email: {credentials.email}")
    
    if not credentials.name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Full name is required for sign-up.")
    
    try:
        session_data = service.sign_up(credentials)
        user_id = session_data.get('user', {}).get('id')
        log.info(f"Successfully created user and session for user_id: {user_id} and session data: {session_data}")

        return {
            "session": {
                "access_token": session_data['session']['access_token'],
                "token_type": "bearer",
                "user_id": user_id,
                "email": session_data['user']['email']
            }
        }
    except HTTPException as e:
        log.error(f"Sign-up failed for {credentials.email}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        if "rate limit" in str(e).lower():
            log.warning(f"Rate limit exceeded during sign-up for {credentials.email}: {e}")
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded. Please try again later.")
        
        log.critical(f"An unexpected server error occurred during sign-up for {credentials.email}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error during sign-up.")


@router.post("/login", response_model=AuthResponse, summary="Log in a user")
def sign_in(
    credentials: UserCredentials,
    service: AuthService = Depends(get_auth_service)
):
    """
    Authenticates a user and returns a session object, including a JWT access token.
    This token must be included in the `Authorization` header for subsequent requests.
    """
    log.info(f"Sign-in attempt for email: {credentials.email}")
    try:
        session_data = service.sign_in(credentials)
        user_id = session_data.get('user', {}).get('id')
        log.info(f"Successfully authenticated and created session for user_id: {user_id} and session data: {session_data}")

        return {
            "session": {
                "access_token": session_data['session']['access_token'],
                "token_type": "bearer",
                "user_id": user_id,
                "email": session_data['user']['email']
            }
        }
    except HTTPException as e:
        log.error(f"Sign-in failed for {credentials.email}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        if "rate limit" in str(e).lower():
            log.warning(f"Rate limit exceeded during sign-in for {credentials.email}: {e}")
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded. Please try again later.")

        log.critical(f"An unexpected server error occurred during sign-in for {credentials.email}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error during sign-in.")
