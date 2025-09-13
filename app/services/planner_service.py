# app/services/planner_service.py
from __future__ import annotations
from typing import Any, Dict
import json
import uuid
from datetime import datetime, timezone
from openai import OpenAI
from supabase import Client
from fastapi import HTTPException, status
from dotenv import load_dotenv

from app.core.config import settings
from app.core.logging import get_logger
from app.utils.llm import get_response
from app.prompts.prompts import LEARNING_PATH_PROMPT, REFINE_ROADMAP_PROMPT

# Load environment variables and initialize logger
load_dotenv()
log = get_logger(__name__)

# Constants for wish statuses to avoid magic strings
WISH_STATUS = {
    "QUEUED": "queued",
    "GENERATING": "generating",
    "COMPLETE": "complete",
    "FAILED": "failed"
}

class RoadmapPlanner:
    """
    Handles the creation and refinement of learning roadmaps using an LLM.
    """
    def __init__(self, supabase: Client, openai_client: OpenAI | None = None):
        """
        Initializes the planner with Supabase and OpenAI clients.

        Args:
            supabase: An initialized Supabase client.
            openai_client: An optional OpenAI client. If not provided, a new one is created.
        """
        self.sb = supabase
        self.client = openai_client or OpenAI()
        log.info("RoadmapPlanner initialized.")

    # --- Private Helper Methods ---

    def _update_wish_status(self, wish_id: str, status: str) -> None:
        """Updates the status of a given wish in the database."""
        log.info(f"Updating wish_id: {wish_id} to status: '{status}'")
        try:
            self.sb.table("wishes").update({"status": status}).eq("id", wish_id).execute()
            log.info(f"Successfully updated wish_id: {wish_id} status.")
        except Exception as e:
            log.error(f"Failed to update status for wish_id: {wish_id}. Error: {e}")
            # Depending on requirements, you might want to raise this exception
            # For now, we log it and continue.

    def _insert_llm_run(self, payload: Dict[str, Any]) -> None:
        """Logs a record of an LLM interaction to the database."""
        log.info(f"Logging LLM run for wish_id: {payload.get('wish_id')}")
        try:
            self.sb.table("llm_runs").insert(payload).execute()
            log.info("LLM run logged successfully.")
        except Exception as e:
            log.error(f"Failed to log LLM run. Error: {e}")

    def _upload_json(self, bucket: str, path: str, obj: dict) -> str:
        """
        Uploads a dictionary as a JSON file to Supabase storage, overwriting if it exists.
        """
        log.info(f"Uploading JSON to storage at path: {bucket}/{path}")
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            # First, try to upload. If it fails due to existing file, update it.
            self.sb.storage.from_(bucket).upload(path=path, file=data, file_options={"content-type": "application/json"})
        except Exception:
            log.warning(f"File at {path} already exists. Overwriting.")
            self.sb.storage.from_(bucket).update(path=path, file=data, file_options={"content-type": "application/json"})
        
        url = f"{bucket}/{path}"
        log.info(f"JSON successfully uploaded. URL: {url}")
        return url

    def _download_json(self, bucket: str, path: str) -> dict:
        """Downloads and parses a JSON file from Supabase storage."""
        log.info(f"Downloading JSON from storage at path: {bucket}/{path}")
        try:
            raw = self.sb.storage.from_(bucket).download(path)
            log.info("Download successful. Parsing JSON.")
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            log.error(f"Failed to download or parse JSON from {bucket}/{path}. Error: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not retrieve a required resource from storage."
            )

    # --- Public Methods ---

    def create_roadmap(self, user_id: str, topic: str) -> dict:
        """
        Orchestrates the creation of a new learning roadmap.

        Args:
            user_id: The ID of the user requesting the roadmap.
            topic: The topic for the learning roadmap.

        Returns:
            A dictionary containing the results of the operation.
        """
        log.info(f"Starting roadmap creation for user_id: {user_id} on topic: '{topic}'")

        # 1. Create initial wish record
        wish_payload = {
            "user_id": user_id,
            "topic": topic,
            "status": WISH_STATUS["QUEUED"],
            "model": settings.PLANNING_MODEL,
        }
        wish_row = self.sb.table("wishes").insert(wish_payload).execute().data[0]
        wish_id = wish_row["id"]
        log.info(f"Created initial wish record with wish_id: {wish_id}")

        self._update_wish_status(wish_id, WISH_STATUS["GENERATING"])

        # 2. Generate roadmap with LLM
        started_at = datetime.now(timezone.utc)
        error_message = None
        roadmap = {}

        log.info(f"Calling LLM to generate roadmap for wish_id: {wish_id}")
        try:
            raw_response = get_response(
                client=self.client,
                system_prompt=LEARNING_PATH_PROMPT,
                user_prompt=topic,
                model=settings.PLANNING_MODEL,
                web_search=settings.WEB_SEARCH
            )
            roadmap = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
            log.info(f"LLM roadmap generation successful for wish_id: {wish_id}")
        except Exception as e:
            log.error(f"LLM roadmap generation failed for wish_id: {wish_id}. Error: {e}")
            error_message = str(e)
            roadmap = {"error": "roadmap_generation_failed", "details": error_message}

        latency_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        log.info(f"LLM call latency: {latency_ms}ms")

        # 3. Store roadmap and create book entry if successful
        book_row = None
        content_url = None
        if not error_message:
            log.info(f"Storing successful roadmap for wish_id: {wish_id}")
            path = f"{user_id}/{wish_id}/roadmap.json"
            content_url = self._upload_json(settings.APP_BUCKET_BOOKS, path, roadmap)
            
            book_payload = {
                "wish_id": wish_id,
                "title": topic,
                "outline": roadmap.get("outline"),
                "summary": roadmap.get("summary"),
                "generated_title": roadmap.get("subject", topic),
                "content_url": content_url
            }
            book_row = self.sb.table("books").insert(book_payload).execute().data[0]
            log.info(f"Created book record with id: {book_row['id']} for wish_id: {wish_id}")

        # 4. Log the LLM run
        llm_run_payload = {
            "user_id": user_id, "wish_id": wish_id,
            "book_id": book_row["id"] if book_row else None,
            "prompt_name": "learning_path", "prompt_version": 1,
            "provider": "openai", "model": settings.PLANNING_MODEL,
            "latency_ms": latency_ms,
            "status_code": 200 if not error_message else 500,
            "error": error_message,
            "request_json": {"topic": topic},
            "response_json": roadmap,
        }
        self._insert_llm_run(llm_run_payload)

        # 5. Finalize wish status
        final_status = WISH_STATUS["COMPLETE"] if not error_message else WISH_STATUS["FAILED"]
        self._update_wish_status(wish_id, final_status)

        log.info(f"Roadmap creation process finished for wish_id: {wish_id} with status: {final_status}")
        return {
            "wish": {"id": wish_id, "status": final_status},
            "book": book_row,
            "book_id": book_row["id"] if book_row else None,
            "roadmap": roadmap
        }

    def refine_roadmap(self, user_id: str, wish_id: str, instructions: str) -> dict:
        """
        Orchestrates the refinement of an existing learning roadmap.

        Args:
            user_id: The ID of the user requesting the refinement.
            wish_id: The ID of the original wish to refine.
            instructions: The user's instructions for refinement.

        Returns:
            A dictionary containing the results of the refinement operation.
        """
        log.info(f"Starting roadmap refinement for user_id: {user_id} on original wish_id: {wish_id}")

        # 1. Fetch the original roadmap
        log.info(f"Fetching latest book record for original wish_id: {wish_id}")
        book_res = self.sb.table("books").select("content_url, topic:wishes(topic)").eq("wish_id", wish_id).order("created_at", desc=True).limit(1).maybe_single().execute()
        
        if not book_res.data or not book_res.data.get("content_url"):
            log.error(f"No book or content_url found for wish_id: {wish_id}. Cannot refine.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Original roadmap not found for this wish.")

        content_url = book_res.data["content_url"]
        original_topic = book_res.data.get("topic", {}).get("topic", "Untitled")
        bucket, *path_parts = content_url.split("/")
        original_roadmap = self._download_json(bucket, "/".join(path_parts))
        log.info(f"Successfully downloaded original roadmap for wish_id: {wish_id}")

        # 2. Create a new wish record for this refinement action
        refinement_topic = f"Refinement of '{original_topic}'"
        new_wish_payload = {
            "user_id": user_id, "topic": refinement_topic,
            "status": WISH_STATUS["QUEUED"], "model": settings.PLANNING_MODEL,
        }
        new_wish_row = self.sb.table("wishes").insert(new_wish_payload).execute().data[0]
        new_wish_id = new_wish_row["id"]
        log.info(f"Created new wish record with id: {new_wish_id} for refinement.")

        self._update_wish_status(new_wish_id, WISH_STATUS["GENERATING"])

        # 3. Generate refined roadmap with LLM
        started_at = datetime.now(timezone.utc)
        error_message = None
        refined_roadmap = {}
        
        log.info(f"Calling LLM to refine roadmap for new_wish_id: {new_wish_id}")
        try:
            prompt = f"Original Roadmap: {json.dumps(original_roadmap)}\n\nRefinement Instructions: {instructions}"
            raw_response = get_response(
                client=self.client,
                system_prompt=REFINE_ROADMAP_PROMPT,
                user_prompt=prompt,
                model=settings.PLANNING_MODEL,
                web_search=settings.WEB_SEARCH
            )
            refined_roadmap = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
            log.info(f"LLM refinement successful for new_wish_id: {new_wish_id}")
        except Exception as e:
            log.error(f"LLM refinement failed for new_wish_id: {new_wish_id}. Error: {e}")
            error_message = str(e)
            refined_roadmap = {"error": "refinement_failed", "details": error_message}
            
        latency_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        log.info(f"LLM call latency: {latency_ms}ms")
        
        # 4. Store refined roadmap and create new book entry if successful
        new_book_row = None
        if not error_message:
            log.info(f"Storing refined roadmap for new_wish_id: {new_wish_id}")
            new_path = f"{user_id}/{new_wish_id}/roadmap.refined.{uuid.uuid4().hex[:8]}.json"
            new_content_url = self._upload_json(settings.APP_BUCKET_BOOKS, new_path, refined_roadmap)
            
            new_book_payload = {
                "wish_id": new_wish_id, "title": refinement_topic,
                "outline": refined_roadmap.get("outline"),
                "summary": refined_roadmap.get("summary"),
                "generated_title": refined_roadmap.get("subject", original_topic),
                "content_url": new_content_url
            }
            new_book_row = self.sb.table("books").insert(new_book_payload).execute().data[0]
            log.info(f"Created new book record with id: {new_book_row['id']} for refined wish.")

        # 5. Log the LLM run
        llm_run_payload = {
            "user_id": user_id, "wish_id": new_wish_id,
            "book_id": new_book_row["id"] if new_book_row else None,
            "prompt_name": "refine_learning_path", "prompt_version": 1,
            "provider": "openai", "model": settings.PLANNING_MODEL,
            "latency_ms": latency_ms,
            "status_code": 200 if not error_message else 500,
            "error": error_message,
            "request_json": {"original_wish_id": wish_id, "instructions": instructions},
            "response_json": refined_roadmap,
        }
        self._insert_llm_run(llm_run_payload)

        # 6. Finalize wish status
        final_status = WISH_STATUS["COMPLETE"] if not error_message else WISH_STATUS["FAILED"]
        self._update_wish_status(new_wish_id, final_status)
        
        log.info(f"Roadmap refinement process finished for new_wish_id: {new_wish_id} with status: {final_status}")
        return {
            "wish": {"id": new_wish_id, "status": final_status},
            "book": new_book_row,
            "book_id": new_book_row["id"] if new_book_row else None,
            "roadmap": refined_roadmap
        }

# # app/services/planner_service.py
# from __future__ import annotations
# from typing import Any, Dict
# import json, uuid
# from datetime import datetime, timezone
# from openai import OpenAI
# from supabase import Client
# from app.core.config import settings
# from app.core.logging import get_logger; log = get_logger(__name__)
# from app.utils.llm import get_response
# from app.prompts.prompts import LEARNING_PATH_PROMPT, REFINE_ROADMAP_PROMPT
# from dotenv import load_dotenv; load_dotenv()
# from fastapi import HTTPException, status

# WISH_STATUS = {"QUEUED":"queued","GENERATING":"generating","COMPLETE":"complete","FAILED":"failed"}

# class RoadmapPlanner:
#     def __init__(self, supabase: Client, openai_client: OpenAI | None = None):
#         self.sb = supabase
#         self.client = openai_client or OpenAI()

#     def _insert_llm_run(self, payload: Dict[str, Any]) -> None:
#         self.sb.table("llm_runs").insert(payload).execute()

#     def _upload_json(self, bucket: str, path: str, obj: dict) -> str:
#         data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
#         try:
#             self.sb.storage.from_(bucket).upload(path=path, file=data, file_options={"content-type":"application/json"})
#         except Exception:
#             # if exists, overwrite
#             self.sb.storage.from_(bucket).update(path=path, file=data, file_options={"content-type":"application/json"})
#         return f"{bucket}/{path}"
    
#     def _download_json(self, bucket: str, path: str) -> dict:
#         """Helper to download and parse a JSON file from storage."""
#         raw = self.sb.storage.from_(bucket).download(path)
#         return json.loads(raw.decode("utf-8"))
    
#     def create_roadmap(self, user_id: str, topic: str) -> dict:
#         wish = {
#             "user_id": user_id,
#             "topic": topic,
#             "status": WISH_STATUS["QUEUED"],
#             "model": settings.PLANNING_MODEL,
#         }
#         wish_row = self.sb.table("wishes").insert(wish).execute().data[0]
#         wish_id = wish_row["id"]

#         self.sb.table("wishes").update({"status": WISH_STATUS["GENERATING"]}).eq("id", wish_id).execute()

#         started = datetime.now(timezone.utc)
#         error_txt = None
#         response_payload = None

#         try:
#             raw = get_response(
#                 client=self.client,
#                 system_prompt=LEARNING_PATH_PROMPT,
#                 user_prompt=topic,
#                 model=settings.PLANNING_MODEL,
#                 verbosity="low",
#                 effort="low",
#                 web_search=settings.WEB_SEARCH
#             )
#             roadmap = json.loads(raw) if isinstance(raw, str) else raw
#             response_payload = roadmap
#         except Exception as e:
#             error_txt = str(e)
#             roadmap = {"error": "roadmap_generation_failed", "details": error_txt}

#         latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

#         book_row = None
#         content_url = None
#         if error_txt is None:
#             path = f"{user_id}/{wish_id}/roadmap.json"
#             content_url = self._upload_json(settings.APP_BUCKET_BOOKS, path, roadmap)
#             book_row = self.sb.table("books").insert({
#                 "wish_id": wish_id,
#                 "title": f"{topic}",
#                 "outline": roadmap.get("outline") if isinstance(roadmap, dict) else None,
#                 "summary": roadmap.get("summary") if isinstance(roadmap, dict) else None,
#                 "content_url": content_url
#             }).execute().data[0]

#         self._insert_llm_run({
#             "id": str(uuid.uuid4()),
#             "user_id": user_id,
#             "wish_id": wish_id,
#             "book_id": book_row["id"] if book_row else None,
#             "prompt_name": "learning_path",
#             "prompt_version": 1,
#             "provider": "openai",
#             "model": settings.PLANNING_MODEL,
#             "latency_ms": latency_ms,
#             "status_code": 200 if error_txt is None else 500,
#             "error": error_txt,
#             "request_json": {"topic": topic},
#             "response_json": response_payload if error_txt is None else {"error": error_txt},
#         })

#         self.sb.table("wishes").update({
#             "status": WISH_STATUS["COMPLETE"] if error_txt is None else WISH_STATUS["FAILED"]
#         }).eq("id", wish_id).execute()

#         return {
#             "wish": {"id": wish_id, "status": WISH_STATUS["COMPLETE"] if error_txt is None else WISH_STATUS["FAILED"]},
#             "book": book_row,
#             "artifact": content_url,
#             "roadmap": roadmap
#         }

#     def refine_roadmap(self, user_id: str, wish_id: str, instructions: str) -> dict:
#         """
#         Refines a roadmap by fetching the latest version from storage and applying new instructions.
#         """
#         original_topic = self.sb.table("wishes").select("topic").eq("id", wish_id).maybe_single().execute().data.get("topic", "Untitled")
        
#         log.info(f"Starting refinement process for wish_id: {wish_id} with instructions: {instructions[:50]}...\nOriginal topic: {original_topic[:50]}")

#         new_topic = f"Refine Instructions: {instructions[:50]}__Original wish_id: {wish_id[:5]}"
#         new_wish = {
#             "user_id": user_id,
#             "topic": new_topic,
#             "status": WISH_STATUS["QUEUED"],
#             "model": settings.PLANNING_MODEL,
#         }
#         new_wish_row = self.sb.table("wishes").insert(new_wish).execute().data[0]
#         new_wish_id = new_wish_row["id"]
#         log.info(f"Created new wish entry with id: {new_wish_id} for refinement process.")

#         self.sb.table("wishes").update({"status": WISH_STATUS["GENERATING"]}).eq("id", new_wish_id).execute()

#         log.info(f"Fetching latest roadmap for wish_id: {wish_id} to apply refinements.")

#         book_res = self.sb.table("books").select("content_url").eq("wish_id", wish_id).order("created_at", desc=True).limit(1).maybe_single().execute()
#         log.info(f"Book fetch response: {book_res}")

#         if not book_res:
#             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incorrect wish ID or no roadmap found for this wish.")
        
#         book = book_res.data
#         if not book or not book.get("content_url"):
#             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Original roadmap not found for this wish.")

#         content_url = book["content_url"]
#         bucket, *path_parts = content_url.split("/")
#         path = "/".join(path_parts)
#         original_roadmap = self._download_json(bucket, path)

#         # Step 3: Proceed with the refinement process using the fetched roadmap
#         started = datetime.now(timezone.utc)
#         error_txt = None
#         response_payload = None

#         try:
#             raw = get_response(
#                 client=self.client,
#                 system_prompt=REFINE_ROADMAP_PROMPT,
#                 user_prompt=f"Roadmap: {json.dumps(original_roadmap)}\nInstructions: {instructions}",
#                 model=settings.PLANNING_MODEL,
#                 verbosity="low",
#                 effort="low",
#                 web_search=settings.WEB_SEARCH
#             )
#             refined_roadmap = json.loads(raw) if isinstance(raw, str) else raw
#             response_payload = refined_roadmap
#         except Exception as e:
#             error_txt = str(e)
#             refined_roadmap = {"error": "refine_failed", "details": error_txt}

#         latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

#         # Step 4: Save the new refined roadmap and log the run
#         new_content_url = None
#         if error_txt is None:
#             # Save the new version with a ".refined" suffix to distinguish it
#             new_path = f"{user_id}/{wish_id}/roadmap.refined.{uuid.uuid4()}.json"
#             new_content_url = self._upload_json(settings.APP_BUCKET_BOOKS, new_path, refined_roadmap)
#             book_row = self.sb.table("books").insert({
#                 "wish_id": new_wish_id,
#                 "title": new_topic[:20],
#                 "outline": refined_roadmap.get("outline") if isinstance(refined_roadmap, dict) else None,
#                 "summary": refined_roadmap.get("summary") if isinstance(refined_roadmap, dict) else None,
#                 "content_url": new_content_url
#             }).execute()

#         self._insert_llm_run({
#             "id": str(uuid.uuid4()),
#             "user_id": user_id,
#             "wish_id": new_wish_id,
#             "prompt_name": "refine_learning_path",
#             "prompt_version": 1,
#             "provider": "openai",
#             "model": settings.PLANNING_MODEL,
#             "latency_ms": latency_ms,
#             "status_code": 200 if error_txt is None else 500,
#             "error": error_txt,
#             "request_json": {"instructions": instructions},
#             "response_json": response_payload if error_txt is None else {"error": error_txt},
#         })

#         self.sb.table("wishes").update({
#             "status": WISH_STATUS["COMPLETE"] if error_txt is None else WISH_STATUS["FAILED"]
#         }).eq("id", new_wish_id).execute()

#         return {"wish": {"id": new_wish_id, "status": WISH_STATUS["COMPLETE"] if error_txt is None else WISH_STATUS["FAILED"]}, "book": book_row, "artifact": new_content_url, "roadmap": refined_roadmap}