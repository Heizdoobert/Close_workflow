import json
import sys
import time
from pathlib import Path

# Importable from any cwd: sibling workspace holds scripts/tri_modal_router/agent_system
_WS = Path(__file__).resolve().parents[2] / "Agent_update_smart_working"
if _WS.is_dir() and str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

# Core components from the agent-system framework
from scripts.tenant_vector_store import TenantVectorStore
from tri_modal_router.router import TaskDomain, route_execution_tier
from agent_system.core.truncator import StructuralTruncator
from agent_system.storage.sqlite_store import SQLiteStore

router = APIRouter()

# Simple Pydantic models for request validation
class DocumentAddPayload(BaseModel):
    tenant_id: str = Field(..., description="Identifier of the tenant")
    texts: list[str] = Field(..., description="List of raw documents to index")

# -------------------------------------------------------------------
# Middleware: request audit & token‑budget logging
# -------------------------------------------------------------------

# APIRouter has no .middleware decorator – keep this as a plain dispatcher and
# mount it on the FastAPI app via:
#   app.add_middleware(BaseHTTPMiddleware, dispatch=audit_middleware)
async def audit_middleware(request: Request, call_next):
    # Generate a simple request id (could be UUID in production)
    request_id = str(time.time()).replace(".", "")
    # Capture basic request info
    method = request.method
    path = request.url.path
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload_size = len(json.dumps(body).encode("utf-8"))
    # Record start time for later duration (optional)
    start = time.time()

    # Proceed with route handling
    response = await call_next(request)

    # After response, log to SQLiteStore (sessions table)
    try:
        from agent_system.storage.sqlite_store import SQLiteStore
        store = SQLiteStore()
        def _log(cur):
            cur.execute(
                "INSERT INTO sessions (session_id, data_json, created_at, updated_at) VALUES (?,?,?,?)",
                (
                    request_id,
                    json.dumps({
                        "method": method,
                        "path": path,
                        "payload_size": payload_size,
                        "status_code": response.status_code,
                        "duration_ms": int((time.time() - start) * 1000),
                    }),
                    time.time(),
                    time.time(),
                ),
            )
        store.execute_with_retry(_log)
    except Exception as e:
        # Fail‑soft: never block the API because of logging errors
        print("[audit] logging failed:", e)

    return response

class QueryPayload(BaseModel):
    tenant_id: str = Field(..., description="Identifier of the tenant")
    query: str = Field(..., description="Search query string")
    top_k: int = Field(5, ge=1, le=20, description="Number of results to return")

# Token‑budget helper – very lightweight example
class TokenEngine:
    def __init__(self, budget_bytes: int = 3072):
        self.budget_bytes = budget_bytes
        self.truncator = StructuralTruncator()

    def check(self, payload: dict) -> bool:
        """Return True if the JSON‑encoded payload fits inside the token budget.
        The truncator uses UTF‑8 byte size, which approximates token usage for the
        framework's context‑budget policy (<3KB)."""
        encoded = json.dumps(payload).encode("utf-8")
        return len(encoded) <= self.budget_bytes

# Initialize shared services (singletons for the process)
sqlite_store = SQLiteStore()
token_engine = TokenEngine()

@router.post("/add_documents")
async def add_documents(payload: DocumentAddPayload):
    """Index raw texts for a tenant.
    The function respects the token‑budget before any heavy work is performed.
    """
    if not token_engine.check(payload.dict()):
        raise HTTPException(status_code=422, detail="Payload exceeds token budget (3KB)")

    store = TenantVectorStore(tenant_id=payload.tenant_id)
    try:
        store.add_documents(payload.texts)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "SUCCESS", "added": len(payload.texts)}

@router.post("/query")
async def query(payload: QueryPayload):
    """Similarity search limited to the tenant.
    The request is routed through TriModalRouter so that the framework can
    choose an appropriate execution tier based on estimated token cost.
    """
    if not token_engine.check(payload.dict()):
        raise HTTPException(status_code=422, detail="Payload exceeds token budget (3KB)")

    # Estimate cost (very rough – count characters as proxy for tokens)
    estimated_tokens = len(payload.query)
    routing = route_execution_tier(
        TaskDomain.INTERACTIVE_AGENT_LOOP,
        estimated_tokens=estimated_tokens,
    )
    tier = routing["tier"]

    # In this simple service we don't spawn a sub‑agent – we just note the tier.
    # Real deployments could dispatch a flash or pro model here.
    store = TenantVectorStore(tenant_id=payload.tenant_id)
    try:
        hits = store.query(payload.query, top_k=payload.top_k)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "status": "SUCCESS",
        "tier": tier.name if hasattr(tier, "name") else "default",
        "results": hits,
    }

@router.get("/status")
async def health_check():
    """Basic health endpoint – now also reports token‑budget status."""
    return {
        "status": "ok",
        "token_budget_bytes": token_engine.budget_bytes,
    }

# Optional: expose a simple endpoint to inspect memory statistics
# Endpoint to retrieve workflow status by run_id
@router.get("/budget")
async def get_budget():
    """Placeholder Budget Management endpoint for each tenant.
    Returns the token‑budget configured for the service.
    In a real implementation this would track per‑tenant spending limits.
    """
    return {"tenant_budget": "unlimited", "token_budget_bytes": token_engine.budget_bytes}

# Endpoint to retrieve workflow status by run_id
@router.get("/workflow_status/{run_id}")
async def workflow_status(run_id: str):
    """Return the stored status and result (if any) of a workflow run.
    The data is persisted in the ``workflow_runs`` SQLite table.
    """
    from agent_system.storage.sqlite_store import SQLiteStore
    store = SQLiteStore()
    def _select(cur):
        cur.execute(
            "SELECT tenant_id, goal, status, started_at, finished_at, result_json FROM workflow_runs WHERE run_id = ?",
            (run_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow run not found")
        result_json = row[5]
        result = json.loads(result_json) if result_json else None
        return {
            "run_id": run_id,
            "tenant_id": row[0],
            "goal": row[1],
            "status": row[2],
            "started_at": row[3],
            "finished_at": row[4],
            "result": result,
        }
    return store.execute_with_retry(_select)

# Endpoint to launch the 12‑step Close‑Workflow
@router.post("/run_workflow")
async def run_workflow(payload: DocumentAddPayload):
    """Trigger the canonical 12‑step workflow for a tenant.
    The payload re‑uses ``DocumentAddPayload`` (tenant_id + dummy texts).
    Only ``tenant_id`` and ``goal`` (first text) are required.
    """
    if not payload.texts:
        raise HTTPException(status_code=400, detail="Goal text required in `texts` array.")
    # The first text element is interpreted as the high‑level goal/feature.
    goal = payload.texts[0]
    from workflow.close_workflow_engine import launch_workflow
    try:
        result = launch_workflow(payload.tenant_id, goal)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result


