# app/api/deps.py
from fastapi import Header, HTTPException, status
from jose import jwt, JWTError
from app.core.config import settings
from app.core.logging import get_logger
# Instantiate the logger for this specific module
log = get_logger(__name__)

def current_user(authorization: str | None = Header(default=None), x_dev_user_id: str | None = Header(default=None)) -> dict:
    """
    FastAPI dependency to authenticate and identify the current user.
    It validates the JWT from the Authorization header or uses a dev bypass.
    """
    # Dev bypass (local only)
    if settings.ALLOW_DEV_BYPASS and (x_dev_user_id or settings.DEV_USER_ID):
        dev_id = x_dev_user_id or settings.DEV_USER_ID
        log.info(f"Authenticated via developer bypass for user_id: {dev_id} ALLOW_DEV_BYPASS is {settings.ALLOW_DEV_BYPASS} and DEV_USER_ID is set to {settings.DEV_USER_ID}")
        return {"id": dev_id, "mode": "dev-bypass"}

    if not authorization or not authorization.startswith("Bearer "):
        log.error("Authentication failed: Missing or invalid Authorization header.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header"
        )

    token = authorization.split(" ", 1)[1]
    try:
        claims = jwt.decode(token, settings.SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
        user_id = claims.get("sub")
        if not user_id:
            log.error("Authentication failed: 'sub' claim missing from JWT.")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims.")
        
        log.info(f"Successfully authenticated user_id: {user_id}")
        return {"id": user_id}
    except JWTError as e:
        # This will catch expired tokens, invalid signatures (wrong secret), etc.
        log.error(f"Authentication failed: Invalid JWT. Reason: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    except Exception as e:
        log.critical(f"An unexpected error occurred during token validation: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during authentication."
        )