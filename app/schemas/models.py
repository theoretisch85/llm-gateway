from pydantic import BaseModel


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "llm-gateway"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelCard]
