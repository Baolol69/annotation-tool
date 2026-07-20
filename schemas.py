from pydantic import BaseModel
from typing import List, Any, Optional

class CurrentTask(BaseModel):
    task_id: str = ""
    prediction: str = ""
    region: str = ""
    audio_url_path: str = ""
    audio_data: Any = None
    project_id: str = ""
    parent_prediction_id: Optional[int] = None
    original_result: List[Any] = []
    task_info: dict = {}

class SubmitTask(BaseModel):
    transcript: Optional[str] = ""
    gender: Optional[str] = ""
    topic: Optional[str] = ""
    audio_issues: Optional[List[str]] = None

class AnnotationResponse(BaseModel):
    transcript: str
    gender: str
    topic: str
    mc: str
    error_alert: str

class TaskState(BaseModel):
    task: Optional[CurrentTask] = None
    gemini_response: Optional[AnnotationResponse] = None