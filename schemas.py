from pydantic import BaseModel
from typing import List, Any, Optional

class CurrentTask(BaseModel):
    task_id: str = ""
    prediction: str = ""
    region: str = ""
    audio_url_path: str = ""
    audio_data: Any = None

class SubmitTask(BaseModel):
    transcript: Optional[str] = ""
    gender: Optional[str] = ""
    topic: Optional[str] = ""
    audio_issues: List[str] = []

class AnnotationResponse(BaseModel):
    transcript: str
    gender: str
    topic: str
    mc: str
    error_alert: str

class TaskState(BaseModel):
    task: Optional[CurrentTask] = None
    gemini_response: Optional[AnnotationResponse] = None