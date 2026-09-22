"""Pure research computations; no network, persistence, signing or execution."""

from fastapi import APIRouter, HTTPException

from ..launch.batch_scoring import BatchRequest, BatchResult, score_batch
from ..launch.models import StrictModel
from ..launch.scoring import Candidate, ScorePolicy, ScoreResult, score_candidate
from .deps import State

router = APIRouter(prefix="/api/v1/research", tags=["research"])


class ScoreRequest(StrictModel):
    candidate: Candidate
    policy: ScorePolicy


@router.post("/token-score", response_model=ScoreResult)
def token_score(request: ScoreRequest, _state: State) -> ScoreResult:
    return score_candidate(request.candidate, request.policy)


@router.post("/batch-score", response_model=BatchResult)
def batch_score(request: BatchRequest, _state: State) -> BatchResult:
    try:
        return score_batch(request)
    except ValueError:
        raise HTTPException(status_code=422, detail="Batch evidence binding failed") from None
