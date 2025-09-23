# app/services/pdf_service.py
from __future__ import annotations
import uuid
import os
from typing import List, Optional
from io import BytesIO

from fastapi import HTTPException, status
from supabase import Client
from xhtml2pdf import pisa

from app.core.logging import get_logger
from app.services.html_conversion_service import convert_markdown_to_styled_html

log = get_logger(__name__)

class PDFService:
    """
    Service for combining book chapters into a single styled PDF using xhtml2pdf.
    """
    
    def __init__(self, supabase: Client):
        self.sb = supabase
        self.chapters_bucket = "chapters"
        self.pdf_bucket = "ebooks"
        
    def _get_chapters_by_book_id(self, book_id: str) -> List[dict]:
        """Fetches and sorts all chapters for a given book_id."""
        try:
            response = self.sb.table("chapters").select("id, book_id, idx").eq("book_id", book_id).order("idx").execute()
            log.info(f"Found {len(response.data)} chapters for book_id: {book_id}")
            return response.data
        except Exception as e:
            log.error(f"Failed to fetch chapters for book_id {book_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                detail="Failed to fetch chapters"
            )

    def _download_markdown_content(self, path_in_bucket: str) -> str:
        """Downloads markdown content from the chapters bucket."""
        try:
            log.info(f"Downloading markdown from: {self.chapters_bucket}/{path_in_bucket}")
            raw_content = self.sb.storage.from_(self.chapters_bucket).download(path_in_bucket)
            return raw_content.decode("utf-8")
        except Exception as e:
            log.error(f"Failed to download markdown from {path_in_bucket}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                detail=f"Failed to download markdown file: {path_in_bucket}"
            )
    
    def _download_chapters_parallel(self, chapters: List[dict]) -> List[dict]:
        """
        Downloads all chapter markdown content in parallel for better performance.
        
        Args:
            chapters: List of chapter dictionaries with idx and book_id
            
        Returns:
            List[dict]: List of chapters with downloaded content
        """
        import concurrent.futures
        from typing import Tuple
        
        def download_single_chapter(chapter: dict) -> dict:
            """Download a single chapter's content."""
            try:
                path_in_bucket = f"books/{chapter['book_id']}/chapters/{chapter['idx']}.md"
                content = self._download_markdown_content(path_in_bucket)
                return {
                    **chapter,
                    "content": content,
                    "download_success": True
                }
            except Exception as e:
                log.error(f"Failed to download chapter {chapter['idx']}: {e}")
                return {
                    **chapter,
                    "content": "",
                    "download_success": False,
                    "error": str(e)
                }
        
        try:
            log.info(f"Starting parallel download of {len(chapters)} chapters")
            
            # Use ThreadPoolExecutor for parallel downloads
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                # Submit all download tasks
                future_to_chapter = {
                    executor.submit(download_single_chapter, chapter): chapter 
                    for chapter in chapters
                }
                
                # Collect results as they complete
                downloaded_chapters = []
                for future in concurrent.futures.as_completed(future_to_chapter):
                    try:
                        result = future.result()
                        downloaded_chapters.append(result)
                    except Exception as e:
                        chapter = future_to_chapter[future]
                        log.error(f"Failed to download chapter {chapter['idx']}: {e}")
                        downloaded_chapters.append({
                            **chapter,
                            "content": "",
                            "download_success": False,
                            "error": str(e)
                        })
            
            # Sort by chapter index to maintain order
            downloaded_chapters.sort(key=lambda x: x['idx'])
            
            successful_downloads = sum(1 for ch in downloaded_chapters if ch.get('download_success', False))
            log.info(f"Parallel download completed: {successful_downloads}/{len(chapters)} chapters downloaded successfully")
            
            return downloaded_chapters
            
        except Exception as e:
            log.error(f"Error during parallel chapter download: {e}", exc_info=True)
            # Fallback to sequential download
            log.info("Falling back to sequential download")
            return [download_single_chapter(chapter) for chapter in chapters]

    def _convert_html_to_pdf(self, html_string: str) -> bytes:
        """
        Converts a string of HTML into PDF bytes using xhtml2pdf.

        Args:
            html_string: The complete HTML content to convert.

        Returns:
            The generated PDF as raw bytes.
        """
        log.info("Converting combined HTML to PDF with xhtml2pdf...")
        
        result_file = BytesIO()
        
        pisa_status = pisa.CreatePDF(
            html_string.encode('utf-8'),
            dest=result_file,
            encoding='utf-8'
        )
        
        if pisa_status.err:
            log.error(f"xhtml2pdf error: {pisa_status.err}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                detail="PDF generation failed"
            )
        
        pdf_bytes = result_file.getvalue()
        log.info(f"PDF generated successfully ({len(pdf_bytes)} bytes).")
        return pdf_bytes

    def _upload_pdf_to_storage(self, book_id: str, pdf_bytes: bytes) -> str:
        """
        Uploads PDF bytes to the Supabase storage bucket.

        Args:
            book_id: The ID of the book, used for the file path.
            pdf_bytes: The raw bytes of the PDF file to upload.

        Returns:
            The public URL of the uploaded file.
        """
        pdf_path = f"books/{book_id}/{uuid.uuid4()}.pdf"
        try:
            log.info(f"Uploading PDF to {self.pdf_bucket}/{pdf_path}")
            self.sb.storage.from_(self.pdf_bucket).upload(
                path=pdf_path,
                file=pdf_bytes,
                file_options={"content-type": "application/pdf", "cache-control": "3600", "upsert": "true"}
            )
            storage_url = f"{self.pdf_bucket}/{pdf_path}"
            log.info(f"Successfully uploaded PDF to {storage_url}")
            return storage_url
        except Exception as e:
            log.error(f"Failed to upload PDF for book {book_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                detail="Failed to upload PDF"
            )

    def _save_pdf_locally(self, pdf_bytes: bytes, filename: str = "local_book.pdf") -> str:
        """
        Saves PDF bytes to a local file for testing.

        Args:
            pdf_bytes: The raw bytes of the PDF file to save.
            filename: The local filename for the PDF.

        Returns:
            The path to the saved file.
        """
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            
            log.info(f"Saving PDF locally to {filename}")
            with open(filename, "wb") as f:
                f.write(pdf_bytes)
            log.info(f"Successfully saved PDF to {filename}")
            return filename
        except Exception as e:
            log.error(f"Failed to save PDF locally: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                detail="Failed to save PDF locally"
            )

    def _build_book_html(self, book_id: str, book_title: str, as_fragment: bool) -> str:
        """
        Fetches chapters, combines them, and builds the final HTML string for the book.
        """
        chapters = self._get_chapters_by_book_id(book_id)
        if not chapters:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, 
                detail="No chapters found for this book"
            )

        # Use parallel downloads for better performance
        try:
            downloaded_chapters = self._download_chapters_parallel(chapters)
        except Exception as e:
            log.error(f"Parallel download failed, falling back to sequential: {e}")
            # Fallback to sequential download
            downloaded_chapters = []
            for chapter in chapters:
                chapter_index = chapter.get("idx")
                if chapter_index is None:
                    log.warning(f"Skipping chapter {chapter.get('id')} due to missing index ('idx').")
                    continue
                
                markdown_path = f"books/{book_id}/chapters/{chapter_index}.md"
                try:
                    markdown_content = self._download_markdown_content(markdown_path)
                    downloaded_chapters.append({
                        **chapter,
                        "content": markdown_content,
                        "download_success": True
                    })
                except HTTPException as e:
                    log.error(f"Could not download chapter {chapter.get('id')} from path {markdown_path}: {e.detail}")
                    downloaded_chapters.append({
                        **chapter,
                        "content": "",
                        "download_success": False,
                        "error": str(e)
                    })

        # Process downloaded chapters into HTML fragments
        html_fragments = []
        for chapter in downloaded_chapters:
            if not chapter.get("download_success", False):
                log.warning(f"Skipping chapter {chapter.get('id')} due to download failure: {chapter.get('error', 'Unknown error')}")
                continue
                
            try:
                html_fragment = convert_markdown_to_styled_html(chapter["content"], as_fragment=as_fragment)
                html_fragments.append(html_fragment)
            except Exception as e:
                log.error(f"Could not process chapter {chapter.get('id')} content: {e}")

        if not html_fragments:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, 
                detail="No valid chapter content could be processed to generate a PDF"
            )

        log.info(f"Generated HTML fragments for {len(html_fragments)} chapters.")
        
        combined_body_html = '<div style="page-break-after: always;"></div>'.join(html_fragments)
        
        final_html_for_pdf = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>{book_title}</title>
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Merriweather:wght@400;700&family=Playfair+Display:wght@700&display=swap');
                
                @page {{
                    margin: 1in;
                    background-color: #fdf6e3; /* Antique background color */
                }}

                body {{
                    font-family: 'Merriweather', serif;
                    line-height: 1.7;
                    color: #3a3a3a; /* Dark text for readability */
                }}
            </style>
        </head>
        <body>
            {combined_body_html}
        </body>
        </html>
        """
        return final_html_for_pdf

    def create_book_pdf(self, book_id: str, book_title: str = "My Book", subscription: bool = True) -> dict:
        """
        Orchestrates the creation of a book PDF from markdown chapters and uploads to storage.
        Includes timeout handling and error recovery.

        Args:
            book_id: The ID of the book to generate PDF for
            book_title: The title of the book (optional, defaults to "My Book")
            subscription: Whether this is a subscription book

        Returns:
            Dict containing the storage URL and metadata
        """
        import signal
        import time
        
        log.info(f"Starting PDF generation for book_id: {book_id}")
        start_time = time.time()
        
        def timeout_handler(signum, frame):
            raise TimeoutError("PDF generation timed out after 300 seconds")
        
        try:
            # Set up timeout handler (5 minutes)
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(300)  # 5 minutes timeout
            
            # Step 1: Build HTML from chapters (with parallel downloads)
            log.info(f"Building HTML for book_id: {book_id}")
            final_html_for_pdf = self._build_book_html(book_id, book_title, not subscription)
            
            # Step 2: Convert HTML to PDF bytes
            log.info(f"Converting HTML to PDF for book_id: {book_id}")
            pdf_bytes = self._convert_html_to_pdf(final_html_for_pdf)
            
            # Step 3: Upload PDF to storage
            log.info(f"Uploading PDF to storage for book_id: {book_id}")
            storage_url = self._upload_pdf_to_storage(book_id, pdf_bytes)
            
            # Cancel timeout
            signal.alarm(0)
            
            generation_time = time.time() - start_time
            log.info(f"PDF generation completed for book_id: {book_id} in {generation_time:.2f} seconds")
            
            return {
                "status": "success",
                "message": "PDF generated and uploaded successfully",
                "book_id": book_id,
                "book_title": book_title,
                "storage_url": storage_url,
                "file_size_bytes": len(pdf_bytes),
                "generation_time_seconds": generation_time
            }
            
        except TimeoutError as e:
            signal.alarm(0)  # Cancel timeout
            log.error(f"PDF generation timed out for book_id: {book_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
                detail=f"PDF generation timed out after 5 minutes: {str(e)}"
            )
        except HTTPException:
            signal.alarm(0)  # Cancel timeout
            raise
        except Exception as e:
            signal.alarm(0)  # Cancel timeout
            log.error(f"Unexpected error while creating PDF for book {book_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error while creating PDF: {str(e)}"
            )

    def create_book_pdf_locally(self, book_id: str, book_title: str = "My Book", output_path: Optional[str] = None, subscription: bool = True) -> dict:
        """
        Orchestrates the creation of a book PDF and saves it to a local file.

        Args:
            book_id: The ID of the book to generate PDF for
            book_title: The title of the book (optional, defaults to "My Book")
            output_path: Optional custom path for the output file

        Returns:
            Dict containing the local file path and metadata
        """
        log.info(f"Starting LOCAL PDF generation for book_id: {book_id}")

        try:
            final_html_for_pdf = self._build_book_html(book_id, book_title, not subscription)
            
            # Step 1: Convert the assembled HTML to PDF bytes
            pdf_bytes = self._convert_html_to_pdf(final_html_for_pdf)
            
            # Step 2: Save the generated PDF bytes locally
            if output_path is None:
                output_path = f"data/testing/{book_id}/{book_id}.pdf"
            
            local_path = self._save_pdf_locally(pdf_bytes, filename=output_path)
            
            return {
                "status": "success",
                "message": "PDF generated and saved locally successfully",
                "book_id": book_id,
                "book_title": book_title,
                "local_path": local_path,
                "file_size_bytes": len(pdf_bytes)
            }
            
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Unexpected error while creating local PDF for book {book_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error while creating local PDF: {str(e)}"
            )

    def get_pdf_info(self, book_id: str) -> dict:
        """
        Gets information about a book's chapters without generating PDF.

        Args:
            book_id: The ID of the book to get info for

        Returns:
            Dict containing book and chapter information
        """
        try:
            chapters = self._get_chapters_by_book_id(book_id)
            
            return {
                "book_id": book_id,
                "chapter_count": len(chapters),
                "chapters": [
                    {
                        "id": chapter.get("id"),
                        "index": chapter.get("idx"),
                        "book_id": chapter.get("book_id")
                    }
                    for chapter in chapters
                ]
            }
            
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Unexpected error while getting PDF info for book {book_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error while getting PDF info: {str(e)}"
            )

if __name__ == "__main__":
    from app.core.config import settings
    from supabase import create_client

    supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    pdf_service = PDFService(supabase_client)
    
    # Example usage: Generate and save a local PDF for testing
    test_book_id = "c57452f4-b0fa-47fc-8670-c879de8dc9bd"
    result = pdf_service.create_book_pdf_locally(book_id=test_book_id, book_title="Test Book", output_path=f"data/testing/{test_book_id}/test_book.pdf", subscription=True)
    print(result)