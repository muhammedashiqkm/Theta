"""Question writing: a model drafts questions for a topic, checked against the bank's rules.

Stateless. OnlineCampus decides who may generate (the school's AI entitlement, the
teacher's Teaching Scope, daily limits), keeps the log, and saves the drafts a
teacher picks; Theta only writes and checks.

WHICH MODEL: the request may name one, because the choice belongs to the SCHOOL —
OnlineCampus keeps it per institution and sends it. The name must be one of this server's
`generation_models`, so an unlisted or unknown model never reaches a provider. `GET
/v1/generate/models` publishes that list, and OnlineCampus builds its picker from it rather
than keeping a second copy.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.security import Caller, require_staff
from app.generation import service
from app.generation.schemas import GenerateIn, GenerateOut
from app.generation.writer import WriterFactory, allowed_models, get_writer_factory, provider_of

router = APIRouter(prefix="/v1/generate", tags=["generate"])


class ModelChoice(BaseModel):
    name: str
    provider: str
    # This server can actually call it: the provider's key is configured.
    ready: bool
    is_default: bool


class ModelsOut(BaseModel):
    models: list[ModelChoice]
    default: str


@router.get("/models", response_model=ModelsOut)
async def list_models(
    caller: Annotated[Caller, Depends(require_staff)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ModelsOut:
    keys = {"anthropic": bool(settings.anthropic_api_key), "google": bool(settings.google_api_key)}
    choices = []
    for name in allowed_models(settings):
        try:
            provider = provider_of(name)
        except Exception:  # noqa: BLE001  a mis-typed name in config must not break the list
            continue
        choices.append(
            ModelChoice(
                name=name,
                provider=provider,
                ready=keys.get(provider, False),
                is_default=name == settings.generation_model,
            )
        )
    return ModelsOut(models=choices, default=settings.generation_model)


@router.post("/questions", response_model=GenerateOut)
async def generate_questions(
    body: GenerateIn,
    caller: Annotated[Caller, Depends(require_staff)],
    make_writer: Annotated[WriterFactory, Depends(get_writer_factory)],
) -> GenerateOut:
    # The body decides the provider, so the writer is built after the body is read.
    return await service.generate(body, make_writer(body.model), caller)
