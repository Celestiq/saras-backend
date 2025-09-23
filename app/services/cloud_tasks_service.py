# app/services/cloud_tasks_service.py
import json
import os
from typing import Dict, Any, Optional
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account

from app.core.config import settings
from app.core.logging import get_logger
from app.db.supabase import get_supabase

log = get_logger(__name__)

class CloudTasksService:
    """
    Service for managing Google Cloud Tasks for background job processing.
    """
    
    def __init__(self):
        """Initialize the Cloud Tasks client with service account authentication."""
        # Load service account credentials
        try:
            if os.path.exists(settings.GCP_SERVICE_ACCOUNT_KEY_PATH):
                credentials = service_account.Credentials.from_service_account_file(
                    settings.GCP_SERVICE_ACCOUNT_KEY_PATH
                )
                self.client = tasks_v2.CloudTasksClient(credentials=credentials)
                log.info(f"Cloud Tasks client initialized with service account: {settings.GCP_SERVICE_ACCOUNT_KEY_PATH}")
            else:
                # Fallback to default credentials (useful for cloud deployment)
                self.client = tasks_v2.CloudTasksClient()
                log.warning(f"Service account file not found at {settings.GCP_SERVICE_ACCOUNT_KEY_PATH}, using default credentials")
        except Exception as e:
            log.error(f"Failed to initialize Cloud Tasks client with service account: {e}")
            # Fallback to default credentials
            self.client = tasks_v2.CloudTasksClient()
            log.warning("Using default Cloud Tasks client credentials")
        
        self.project_id = settings.GCP_PROJECT_ID
        self.location = settings.GCP_LOCATION
        self.queue_name = settings.GCP_QUEUE_NAME
        self.parent = self.client.queue_path(self.project_id, self.location, self.queue_name)
        log.info(f"Cloud Tasks service initialized with queue: {self.parent}")
    
    def create_chapter_generation_task(
        self, 
        order_id: str, 
        delay_seconds: int = 0
    ) -> str:
        """
        Create a Cloud Task for chapter generation with duplicate prevention.
        
        Args:
            order_id: The order ID to process
            delay_seconds: Optional delay before task execution (default: 0)
            
        Returns:
            str: The task name/ID, or existing task name if already exists
            
        Raises:
            Exception: If order is already being processed or task creation fails
        """
        try:
            # Get database connection
            supabase = get_supabase()
            
            # Check if order is already being processed
            try:
                order_response = supabase.table("orders").select("status, generation_task_id").eq("id", order_id).single().execute()
                
                if not order_response.data:
                    raise Exception(f"Order {order_id} not found in database")
                
                order_data = order_response.data
                current_status = order_data.get("status")
                existing_task_id = order_data.get("generation_task_id")
                
                # Check if order is already being processed
                if current_status in ["generating", "pending"]:
                    if existing_task_id:
                        log.warning(f"DUPLICATE PREVENTION: Order {order_id} is already being processed (status: {current_status}, task: {existing_task_id}). Skipping task creation.")
                        return existing_task_id
                    else:
                        log.warning(f"Order {order_id} is already being processed (status: {current_status}) but no task ID found. Continuing with task creation.")
                        # Continue with task creation but update the order with the new task ID
                
                # Check if order is completed or failed
                if current_status in ["completed", "failed", "completed_with_errors"]:
                    raise Exception(f"Order {order_id} is already {current_status}, cannot create new generation task")
                    
            except Exception as db_error:
                log.error(f"Failed to check order status for {order_id}: {db_error}", exc_info=True)
                # Continue with task creation if we can't check status
                # This ensures the system is resilient to database issues
            
            # Prepare the task payload - only include order_id
            task_payload = {
                "order_id": order_id
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-chapters",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/generate-chapters"
            }
            
            # Add delay if specified
            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.now(timezone.utc).replace(
                        microsecond=0
                    ) + timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp
            
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            
            # Update the order with the generation task ID to prevent duplicates
            try:
                supabase.table("orders").update({
                    "generation_task_id": task_name,
                    "status": "pending"  # Ensure status is set to pending if not already generating
                }).eq("id", order_id).execute()
                log.info(f"Updated order {order_id} with generation task ID: {task_name}")
            except Exception as update_error:
                log.warning(f"Failed to update order {order_id} with generation task ID: {update_error}")
                # Don't fail the entire operation if we can't update the database
            
            log.info(f"Created Cloud Task for chapter generation: {task_name} for order_id: {order_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create Cloud Task for order_id: {order_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create chapter generation task: {str(e)}")
    
    def create_retry_task(
        self,
        order_id: str,
        retry_count: int,
        delay_seconds: int = 300  # 5 minutes default delay
    ) -> str:
        """
        Create a retry task for failed chapter generation.
        
        Args:
            order_id: The order ID to retry
            retry_count: Current retry attempt number
            delay_seconds: Delay before retry (default: 5 minutes)
            
        Returns:
            str: The retry task name/ID
        """
        try:
            # Create retry payload
            retry_payload = {
                "order_id": order_id,
                "retry_count": retry_count
            }
            
            # Create the retry task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-chapters",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(retry_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/generate-chapters"
            }
            
            # Add delay
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(
                datetime.now(timezone.utc).replace(
                    microsecond=0
                ) + timedelta(seconds=delay_seconds)
            )
            task["schedule_time"] = timestamp
            
            # Submit the retry task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            log.info(f"Created retry Cloud Task (attempt {retry_count}): {task_name} for order_id: {order_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create retry Cloud Task for order_id: {order_id}, retry {retry_count}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create retry task: {str(e)}")
    
    def create_individual_chapter_task(
        self, 
        chapter_id: str,
        order_id: str,
        book_id: str,
        user_id: str,
        module_index: int,
        topic_index: int,
        idx: int,
        delay_seconds: int = 0
    ) -> str:
        """
        Create a Cloud Task for individual chapter generation with duplicate prevention.
        
        Args:
            chapter_id: The chapter ID to generate content for
            order_id: The order ID this chapter belongs to
            book_id: The book ID this chapter belongs to
            user_id: The user ID who owns this chapter
            module_index: Module index in the roadmap
            topic_index: Topic index within the module
            idx: Chapter index/number
            delay_seconds: Optional delay before task execution (default: 0)
            
        Returns:
            str: The task name/ID, or existing task name if already exists
            
        Raises:
            Exception: If chapter is already completed or task creation fails
        """
        try:
            # Get database connection
            supabase = get_supabase()
            
            # Check if chapter is already being processed or completed
            try:
                chapter_response = supabase.table("chapters").select("status, content_path, generation_task_id").eq("id", chapter_id).single().execute()
                
                if not chapter_response.data:
                    raise Exception(f"Chapter {chapter_id} not found in database")
                
                chapter_data = chapter_response.data
                current_status = chapter_data.get("status")
                content_path = chapter_data.get("content_path")
                existing_task_id = chapter_data.get("generation_task_id")
                
                # Check if chapter already has content (completed)
                if content_path:
                    log.warning(f"CHAPTER DUPLICATE PREVENTION: Chapter {chapter_id} (idx: {idx}) already has content. Skipping task creation.")
                    return f"already_completed_{chapter_id}"
                
                # Check if chapter is already being processed
                if current_status in ["generating", "pending"]:
                    if existing_task_id:
                        log.warning(f"CHAPTER DUPLICATE PREVENTION: Chapter {chapter_id} (idx: {idx}) is already being processed (status: {current_status}, task: {existing_task_id}). Skipping task creation.")
                        return existing_task_id
                    else:
                        log.warning(f"Chapter {chapter_id} (idx: {idx}) is already being processed (status: {current_status}) but no task ID found. Continuing with task creation.")
                        # Continue with task creation but update the chapter with the new task ID
                
                # Check if chapter is failed (allow retry)
                if current_status == "failed":
                    log.info(f"Chapter {chapter_id} (idx: {idx}) previously failed, creating retry task")
                    # Continue with task creation for retry
                    
            except Exception as db_error:
                log.error(f"Failed to check chapter status for {chapter_id}: {db_error}", exc_info=True)
                # Continue with task creation if we can't check status
                # This ensures the system is resilient to database issues
            
            # Prepare the task payload for individual chapter generation
            task_payload = {
                "chapter_id": chapter_id,
                "order_id": order_id,
                "book_id": book_id,
                "user_id": user_id,
                "module_index": module_index,
                "topic_index": topic_index,
                "idx": idx
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-single-chapter",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/generate-single-chapter"
            }
            
            # Add delay if specified
            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.now(timezone.utc).replace(
                        microsecond=0
                    ) + timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp
            
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            
            # Update the chapter with the generation task ID and status to prevent duplicates
            try:
                supabase.table("chapters").update({
                    "generation_task_id": task_name,
                    "status": "generating",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", chapter_id).execute()
                log.info(f"Updated chapter {chapter_id} (idx: {idx}) with generation task ID: {task_name}")
            except Exception as update_error:
                log.warning(f"Failed to update chapter {chapter_id} with generation task ID: {update_error}")
                # Don't fail the entire operation if we can't update the database
            
            log.info(f"Created Cloud Task for individual chapter generation: {task_name} for chapter_id: {chapter_id} (idx: {idx})")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create Cloud Task for chapter_id: {chapter_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create individual chapter generation task: {str(e)}")
    
    def create_completion_monitoring_task(
        self,
        order_id: str,
        delay_seconds: int = 60  # 1 minute default delay
    ) -> str:
        """
        Create a Cloud Task to monitor chapter completion and trigger final processing.
        Includes deduplication to prevent multiple monitoring tasks for the same order.
        
        Args:
            order_id: The order ID to monitor
            delay_seconds: Delay before checking completion (default: 1 minute)
            
        Returns:
            str: The task name/ID, or existing task name if already exists
        """
        try:
            # Get database connection
            supabase = get_supabase()
            
            # Check if a monitoring task already exists for this order
            existing_order = supabase.table("orders").select("monitoring_task_id").eq("id", order_id).single().execute()
            
            if existing_order.data and existing_order.data.get("monitoring_task_id"):
                existing_task_id = existing_order.data["monitoring_task_id"]
                log.info(f"Completion monitoring task already exists for order {order_id}: {existing_task_id}")
                return existing_task_id
            
            # Prepare the task payload for completion monitoring
            task_payload = {
                "order_id": order_id
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/monitor-completion",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/monitor-completion"
            }
            
            # Add delay
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(
                datetime.now(timezone.utc).replace(
                    microsecond=0
                ) + timedelta(seconds=delay_seconds)
            )
            task["schedule_time"] = timestamp
            
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            
            # Update the order with the monitoring task ID to prevent duplicates
            try:
                supabase.table("orders").update({"monitoring_task_id": task_name}).eq("id", order_id).execute()
                log.info(f"Updated order {order_id} with monitoring task ID: {task_name}")
            except Exception as update_error:
                log.warning(f"Failed to update order {order_id} with monitoring task ID: {update_error}")
                # Don't fail the entire operation if we can't update the database
            
            log.info(f"Created completion monitoring Cloud Task: {task_name} for order_id: {order_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create completion monitoring Cloud Task for order_id: {order_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create completion monitoring task: {str(e)}")
    
    def clear_monitoring_task_id(self, order_id: str) -> bool:
        """
        Clear the monitoring_task_id from an order to allow new monitoring tasks to be created.
        
        Args:
            order_id: The order ID to clear the monitoring task ID for
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            supabase = get_supabase()
            supabase.table("orders").update({"monitoring_task_id": None}).eq("id", order_id).execute()
            log.info(f"Cleared monitoring task ID for order {order_id}")
            return True
        except Exception as e:
            log.error(f"Failed to clear monitoring task ID for order {order_id}. Error: {e}", exc_info=True)
            return False
    
    def clear_generation_task_id(self, order_id: str) -> bool:
        """
        Clear the generation_task_id from an order when processing is complete.
        
        Args:
            order_id: The order ID to clear the generation task ID for
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            supabase = get_supabase()
            supabase.table("orders").update({"generation_task_id": None}).eq("id", order_id).execute()
            log.info(f"Cleared generation task ID for order {order_id}")
            return True
        except Exception as e:
            log.error(f"Failed to clear generation task ID for order {order_id}. Error: {e}", exc_info=True)
            return False
    
    def is_order_being_processed(self, order_id: str) -> bool:
        """
        Check if an order is currently being processed (has generation task in progress).
        
        Args:
            order_id: The order ID to check
            
        Returns:
            bool: True if order is being processed, False otherwise
        """
        try:
            supabase = get_supabase()
            order_response = supabase.table("orders").select("status, generation_task_id").eq("id", order_id).single().execute()
            
            if not order_response.data:
                return False
            
            order_data = order_response.data
            current_status = order_data.get("status")
            generation_task_id = order_data.get("generation_task_id")
            
            # Order is being processed if status is generating/pending and has a task ID
            return current_status in ["generating", "pending"] and generation_task_id is not None
            
        except Exception as e:
            log.error(f"Failed to check if order {order_id} is being processed: {e}", exc_info=True)
            return False
    
    def clear_chapter_generation_task_id(self, chapter_id: str) -> bool:
        """
        Clear the generation_task_id from a chapter when processing is complete.
        
        Args:
            chapter_id: The chapter ID to clear the generation task ID for
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            supabase = get_supabase()
            supabase.table("chapters").update({"generation_task_id": None}).eq("id", chapter_id).execute()
            log.info(f"Cleared generation task ID for chapter {chapter_id}")
            return True
        except Exception as e:
            log.error(f"Failed to clear generation task ID for chapter {chapter_id}. Error: {e}", exc_info=True)
            return False
    
    def create_pdf_generation_task(
        self,
        order_id: str,
        book_id: str,
        user_id: str,
        book_title: str,
        delay_seconds: int = 0
    ) -> str:
        """
        Create a Cloud Task for PDF generation.
        
        Args:
            order_id: The order ID this PDF belongs to
            book_id: The book ID to generate PDF for
            user_id: The user ID who owns this book
            book_title: The title of the book
            delay_seconds: Optional delay before task execution (default: 0)
            
        Returns:
            str: The task name/ID
        """
        try:
            # Prepare the task payload for PDF generation
            task_payload = {
                "order_id": order_id,
                "book_id": book_id,
                "user_id": user_id,
                "book_title": book_title
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-pdf",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/generate-pdf"
            }
            
            # Add delay if specified
            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.now(timezone.utc).replace(
                        microsecond=0
                    ) + timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp
            
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            log.info(f"Created Cloud Task for PDF generation: {task_name} for book_id: {book_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create Cloud Task for PDF generation for book_id: {book_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create PDF generation task: {str(e)}")
    
    def get_queue_info(self) -> Dict[str, Any]:
        """
        Get information about the Cloud Tasks queue.
        
        Returns:
            Dict containing queue information
        """
        try:
            queue = self.client.get_queue(name=self.parent)
            return {
                "name": queue.name,
                "state": queue.state.name,
                "tasks_count": getattr(queue, 'tasks_count', 'unknown'),
                "retry_config": {
                    "max_attempts": queue.retry_config.max_attempts,
                    "max_retry_duration": str(queue.retry_config.max_retry_duration),
                }
            }
        except Exception as e:
            log.error(f"Failed to get queue info: {e}", exc_info=True)
            return {"error": str(e)}
