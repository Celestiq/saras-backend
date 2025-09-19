# File: app/api/routes/webhooks.py

from fastapi import APIRouter, Request, status, HTTPException, Depends
from supabase import Client
from app.core.logging import get_logger
from app.db.supabase import get_supabase
from app.services.credit_service import CreditService
import json

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

def get_credit_service(supabase: Client = Depends(get_supabase)) -> CreditService:
    """Dependency to provide a CreditService instance."""
    return CreditService(supabase=supabase)

@router.post("/cashfree", status_code=status.HTTP_200_OK)
async def handle_cashfree_webhook(
    request: Request,
    supabase: Client = Depends(get_supabase),
    credit_service: CreditService = Depends(get_credit_service)
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
            # Check if this is a cart/subscription order
            cart_order_response = supabase.table("direct_orders") \
                    .select("*") \
                    .eq("gateway_order_id", gateway_order_id) \
                    .execute()
            
            # Check if this is a credit purchase order
            credit_purchase_response = supabase.table("credit_purchases") \
                    .select("*") \
                    .eq("payment_id", gateway_order_id) \
                    .execute()
            
            cart_order_found = bool(cart_order_response.data)
            credit_purchase_found = bool(credit_purchase_response.data)
            
            if cart_order_found:
                # Handle cart/subscription order
                log.info(f"Processing cart order webhook for gateway_order_id: {gateway_order_id}")
                
                # Update the order status in direct_orders table
                update_response = supabase.table("direct_orders") \
                        .update({"gateway_order_status": new_status}) \
                        .eq("gateway_order_id", gateway_order_id) \
                        .execute()
                
                log.info(f"Successfully updated cart order status via webhook", extra={
                    "gateway_order_id": gateway_order_id,
                    "event_type": event_type,
                    "new_status": new_status,
                    "records_updated": len(update_response.data)
                })
                
            elif credit_purchase_found:
                # Handle credit purchase order
                log.info(f"Processing credit purchase webhook for gateway_order_id: {gateway_order_id}")
                
                credit_purchase = credit_purchase_response.data[0]
                user_id = credit_purchase["user_id"]
                credits_to_add = credit_purchase["credits"]
                
                if new_status == "completed":
                    # Payment successful - add credits to user account
                    try:
                        credit_update = credit_service.add_credits(user_id, credits_to_add)
                        
                        # Update purchase record status to completed
                        credit_service.update_credit_purchase_status(gateway_order_id, "completed")
                        
                        log.info(f"Successfully processed credit purchase webhook", extra={
                            "gateway_order_id": gateway_order_id,
                            "user_id": user_id,
                            "credits_added": credits_to_add,
                            "new_balance": credit_update["new_balance"]
                        })
                        
                    except Exception as credit_error:
                        log.error(f"Failed to add credits for webhook processing", extra={
                            "gateway_order_id": gateway_order_id,
                            "user_id": user_id,
                            "credits_to_add": credits_to_add,
                            "error": str(credit_error)
                        }, exc_info=True)
                        
                        # Update purchase record with error status
                        credit_service.update_credit_purchase_status(gateway_order_id, "failed")
                        raise HTTPException(status_code=500, detail="Failed to process credit purchase")
                        
                else:
                    # Payment failed or cancelled - update status only
                    credit_service.update_credit_purchase_status(gateway_order_id, new_status)
                    
                    log.info(f"Updated credit purchase status via webhook", extra={
                        "gateway_order_id": gateway_order_id,
                        "user_id": user_id,
                        "new_status": new_status
                    })
                    
            else:
                log.error(f"Order not found in any table for webhook processing", extra={
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

        except HTTPException:
            # Re-raise HTTP exceptions
            raise
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

    # Determine order type for response
    order_type = "unknown"
    if new_status:
        try:
            cart_order_exists = bool(supabase.table("direct_orders").select("id").eq("gateway_order_id", gateway_order_id).execute().data)
            credit_purchase_exists = bool(supabase.table("credit_purchases").select("id").eq("payment_id", gateway_order_id).execute().data)
            
            if cart_order_exists:
                order_type = "cart_order"
            elif credit_purchase_exists:
                order_type = "credit_purchase"
        except Exception as e:
            log.warning(f"Could not determine order type for response: {e}")
    
    return {
        "status": "received",
        "event_type": event_type,
        "gateway_order_id": gateway_order_id,
        "order_type": order_type,
        "processed": new_status is not None
    }