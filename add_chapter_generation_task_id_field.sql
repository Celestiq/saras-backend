-- Add generation_task_id field to chapters table to prevent duplicate chapter generation
-- This field will store the Cloud Task ID for the individual chapter generation task

ALTER TABLE chapters 
ADD COLUMN generation_task_id TEXT;

-- Add an index for better query performance
CREATE INDEX idx_chapters_generation_task_id ON chapters(generation_task_id);

-- Add a comment to document the field's purpose
COMMENT ON COLUMN chapters.generation_task_id IS 'Cloud Task ID for individual chapter generation task. Used to prevent duplicate chapter generation.';

-- Update existing chapters to have a default status if they don't have one
UPDATE chapters 
SET status = CASE 
    WHEN content_path IS NOT NULL THEN 'completed'
    ELSE 'pending'
END
WHERE status IS NULL;
