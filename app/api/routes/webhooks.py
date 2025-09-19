# File: app/api/routes/webhooks.py

from fastapi import APIRouter, Request, status, HTTPException, Depends
from supabase import Client
from app.core.logging import get_logger
from app.db.supabase import get_supabase
import json

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

@router.post("/cashfree", status_code=status.HTTP_200_OK)
async def handle_cashfree_webhook(
    request: Request,
    supabase: Client = Depends(get_supabase)
):
    """
    Receives and processes webhook notifications from Cashfree for various payment events.
    """
    # Get request headers for logging
    headers = dict(request.headers)
    client_ip = request.client.host if request.client else "unknown"
    
    log.info(f"Received Cashfree webhook from IP: {client_ip}")
    log.debug(f"Webhook headers: {headers}")

    # --- CRITICAL SECURITY STEP ---
    # TODO: Implement webhook signature verification here to ensure the request is from Cashfree.
    # --------------------------------

    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        log.error(f"Invalid JSON payload in Cashfree webhook: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    except Exception as e:
        log.error(f"Error parsing webhook payload: {str(e)}")
        raise HTTPException(status_code=400, detail="Error parsing payload")

    event_type = payload.get("type")
    
    # Structured logging with all relevant context
    log.info(f"Processing Cashfree webhook", extra={
        "event_type": event_type,
        "client_ip": client_ip,
        "payload_size": len(json.dumps(payload)) if payload else 0
    })
    
    log.debug(f"Full webhook payload", extra={"payload": payload})

    # Extract common data from the payload
    order_data = payload.get("data", {}).get("order", {})
    gateway_order_id = order_data.get("order_id")

    if not gateway_order_id:
        log.warning("Webhook received without an order_id. Cannot process.", extra={
            "event_type": event_type,
            "payload_structure": list(payload.keys()) if payload else []
        })
        return {"status": "received_but_ignored", "reason": "No order_id found"}

    new_status = None
    
    # Determine the new status based on the webhook event type
    if event_type == "PAYMENT_SUCCESS_WEBHOOK":
        new_status = "completed"
    elif event_type in [
        "PAYMENT_FAILED_WEBHOOK", 
        "PAYMENT_USER_DROPPED_WEBHOOK", 
        "ORDER_ABANDONED_WEBHOOK"  # <-- Added the new event type here
    ]:
        new_status = "cancelled"
    
    if new_status:
        log.info(f"Processing Cashfree order status update", extra={
            "gateway_order_id": gateway_order_id,
            "event_type": event_type,
            "new_status": new_status
        })
        
        try:
            # Update the order status in your database
            response = supabase.table("direct_orders") \
                    .update({"gateway_order_status": new_status}) \
                    .eq("gateway_order_id", gateway_order_id) \
                    .execute()
            
            if not response.data:
                log.error(f"Order not found in database for webhook processing", extra={
                    "gateway_order_id": gateway_order_id,
                    "event_type": event_type,
                    "new_status": new_status,
                    "action": "webhook_ignored"
                })
                # Return error status instead of success when order is not found
                return {
                    "status": "error", 
                    "reason": "Order not found in database",
                    "gateway_order_id": gateway_order_id
                }
            else:
                log.info(f"Successfully updated order status via webhook", extra={
                    "gateway_order_id": gateway_order_id,
                    "event_type": event_type,
                    "new_status": new_status,
                    "records_updated": len(response.data)
                })

        except Exception as e:
            log.error(f"Database update failed for webhook processing", extra={
                "gateway_order_id": gateway_order_id,
                "event_type": event_type,
                "new_status": new_status,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Database update failed.")
    else:
        log.info(f"Ignoring unhandled Cashfree event type", extra={
            "event_type": event_type,
            "gateway_order_id": gateway_order_id,
            "action": "ignored"
        })

    return {
        "status": "received",
        "event_type": event_type,
        "gateway_order_id": gateway_order_id,
        "processed": new_status is not None
    }