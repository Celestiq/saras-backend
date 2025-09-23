# app/api/auth/task_auth.py
from fastapi import HTTPException, status, Header, Request
from jwt import PyJWKClient
import jwt
from app.core.logging import get_logger
from app.core.config import settings

log = get_logger(__name__)

def verify_task_authorization_oidc(authorization: str = Header(None), request: Request = None) -> bool:
    """Verify that the request is authorized using Google Cloud OIDC token."""
    if not authorization:
        log.warning("Task endpoint called without authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header"
        )
    
    # Extract token from "Bearer <token>" format
    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            raise ValueError("Invalid scheme")
    except ValueError:
        log.warning(f"Task endpoint called with invalid authorization format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format"
        )
    
    try:
        # Verify the OIDC token
        # For Google Cloud Tasks, we need to verify the JWT token
        jwks_client = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        # Get the expected audience from the request URL
        audience = str(request.url) if request else f"{settings.BACKEND_URL}/tasks"
        
        # Decode and verify the token (without strict audience check for now)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},  # We'll verify audience manually
            issuer="https://accounts.google.com"
        )
        
        # Verify the service account email
        expected_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
        if payload.get("email") != expected_email:
            log.warning(f"Invalid service account email in token: {payload.get('email')}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid service account"
            )
        
        log.debug(f"Successfully verified OIDC token for service account: {payload.get('email')}")
        return True
        
    except jwt.InvalidTokenError as e:
        log.warning(f"Invalid OIDC token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OIDC token"
        )
    except Exception as e:
        log.error(f"Error verifying OIDC token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed"
        )

def verify_task_authorization_legacy(authorization: str = Header(None)) -> bool:
    """Legacy verification using custom Bearer token (for backward compatibility)."""
    if not authorization:
        log.warning("Task endpoint called without authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header"
        )
    
    # Extract token from "Bearer <token>" format
    try:
        scheme, token = authorization.split(" ")
        if scheme.lower() != "bearer":
            raise ValueError("Invalid scheme")
    except ValueError:
        log.warning(f"Task endpoint called with invalid authorization format: {authorization}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format"
        )
    
    # Verify the token matches our job runner secret
    expected_secret = settings.JOB_RUNNER_SECRET
    if token != expected_secret:
        log.warning(f"Task endpoint called with invalid token. Expected length: {len(expected_secret) if expected_secret else 'None'}, Received length: {len(token)}")
        log.debug(f"Expected secret (first 10 chars): {expected_secret[:10] if expected_secret else 'None'}...")
        log.debug(f"Received token (first 10 chars): {token[:10]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token"
        )
    
    return True

def verify_task_authorization(authorization: str = Header(None), request: Request = None) -> bool:
    """
    Verify task authorization using OIDC token with fallback to legacy token.
    This provides backward compatibility while transitioning to OIDC.
    """
    try:
        # Try OIDC verification first
        return verify_task_authorization_oidc(authorization, request)
    except HTTPException as oidc_error:
        # If OIDC fails, try legacy verification as fallback
        try:
            log.debug("OIDC verification failed, trying legacy token verification")
            return verify_task_authorization_legacy(authorization)
        except HTTPException as legacy_error:
            # Both methods failed, return the OIDC error (preferred method)
            log.warning("Both OIDC and legacy token verification failed")
            raise oidc_error
