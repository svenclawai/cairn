from fastapi import FastAPI

from routers import sessions, queries, pages, coverage

app = FastAPI(
    title="Cairn",
    description="Stateful research session service for LLM agents",
    version="0.1.0",
)

app.include_router(sessions.router)
app.include_router(queries.router)
app.include_router(pages.router)
app.include_router(coverage.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
