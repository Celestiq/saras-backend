# app/services/cloud_tasks_service.py
import json
from typing import Dict, Any, Optional
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

class CloudTasksService:
    """
    Service for managing Google Cloud Tasks for background job processing.
    """
    
    def __init__(self):
        """Initialize the Cloud Tasks client."""
        self.client = tasks_v2.CloudTasksClient()
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
        Create a Cloud Task for chapter generation.
        
        Args:
            order_id: The order ID to process
            delay_seconds: Optional delay before task execution (default: 0)
            
        Returns:
            str: The task name/ID
        """
        try:
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
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {settings.JOB_RUNNER_SECRET}"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add service account email if configured (for authentication)
            if settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL:
                task["http_request"]["oidc_token"] = {
                    "service_account_email": settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL,
                    "audience": f"{settings.BACKEND_URL}/tasks/generate-chapters"
                }
            
            # Add delay if specified
            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.now(timezone.utc).replace(
                        microsecond=0
                    ) + timezone.timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp
            
            log.debug(f"Creating Cloud Task with payload: {task_payload} and delay: {delay_seconds} seconds")
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            log.debug(f"Cloud Task creation response: {response}")
            task_name = response.name
            log.info(f"Created Cloud Task for chapter generation: {task_name} for order_id: {order_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create Cloud Task for order_id: {order_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create chapter generation task: {str(e)}")
    
    def create_single_chapter_generation_task(
        self,
        order_id: str,
        book_id: str,
        user_id: str,
        chapter_idx: int,
        module_index: int,
        topic_index: int,
        chapter_title: str,
        delay_seconds: int = 0
    ) -> str:
        """
        Create a Cloud Task for generating a single chapter.
        
        Args:
            order_id: The order ID this chapter belongs to
            book_id: The book ID this chapter belongs to
            user_id: The user ID who owns this order
            chapter_idx: The chapter index within the book
            module_index: The module index in the roadmap
            topic_index: The topic index within the module
            chapter_title: The title of the chapter
            delay_seconds: Optional delay before task execution (default: 0)
            
        Returns:
            str: The task name/ID
        """
        try:
            # Prepare the task payload for single chapter generation
            task_payload = {
                "order_id": order_id,
                "book_id": book_id,
                "user_id": user_id,
                "chapter_idx": chapter_idx,
                "module_index": module_index,
                "topic_index": topic_index,
                "chapter_title": chapter_title
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-single-chapter",
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {settings.JOB_RUNNER_SECRET}"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add service account email if configured (for authentication)
            if settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL:
                task["http_request"]["oidc_token"] = {
                    "service_account_email": settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL,
                    "audience": f"{settings.BACKEND_URL}/tasks/generate-single-chapter"
                }
            
            # Add delay if specified
            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.now(timezone.utc).replace(
                        microsecond=0
                    ) + timezone.timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp
            
            log.debug(f"Creating single chapter Cloud Task with payload: {task_payload} and delay: {delay_seconds} seconds")
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            log.debug(f"Single chapter Cloud Task creation response: {response}")
            task_name = response.name
            log.info(f"Created Cloud Task for single chapter generation: {task_name} for book_id: {book_id}, chapter_idx: {chapter_idx}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create single chapter Cloud Task for book_id: {book_id}, chapter_idx: {chapter_idx}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create single chapter generation task: {str(e)}")
    
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
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {settings.JOB_RUNNER_SECRET}"
                    },
                    "body": json.dumps(retry_payload).encode("utf-8"),
                },
            }
            
            # Add service account email if configured (for authentication)
            if settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL:
                task["http_request"]["oidc_token"] = {
                    "service_account_email": settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL,
                    "audience": f"{settings.BACKEND_URL}/tasks/generate-chapters"
                }
            
            # Add delay
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(
                datetime.now(timezone.utc).replace(
                    microsecond=0
                ) + timezone.timedelta(seconds=delay_seconds)
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
