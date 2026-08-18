from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes_connections import router as connections_router
from app.api.routes_conversation import router as conversation_router
from app.api.routes_feedback import router as feedback_router
from app.api.routes_query import router as query_router
from app.api.routes_schema import router as schema_router
from app.api.routes_semantic import router as semantic_router
from app.charting.chart_selector import ChartSelector
from app.config import get_settings
from app.conversation.memory import ConversationMemory
from app.db.connection import ConnectionManager
from app.db.introspection import SchemaIntrospector
from app.db.query_executor import QueryExecutor
from app.feedback.feedback_store import FeedbackStore
from app.llm.ollama_client import OllamaClient
from app.llm.result_interpreter import ResultInterpreter
from app.llm.sql_generator import SQLGenerator
from app.semantic.glossary import SemanticGlossary


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AI Business Analyst", version="0.1.0")
    app.state.settings = settings
    app.state.connection_manager = ConnectionManager()
    app.state.schema_introspector = SchemaIntrospector(
        app.state.connection_manager,
        cache_ttl_seconds=settings.schema_cache_ttl_seconds,
        row_count_sample_limit=settings.schema_row_count_sample_limit,
    )
    app.state.ollama_client = OllamaClient(
        base_url=settings.ollama_base_url,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
    app.state.sql_generator = SQLGenerator(
        app.state.ollama_client,
        model=settings.ollama_model,
    )
    app.state.result_interpreter = ResultInterpreter(
        app.state.ollama_client,
        model=settings.ollama_model,
    )
    app.state.query_executor = QueryExecutor(
        app.state.connection_manager,
        timeout_seconds=settings.query_timeout_seconds,
        max_rows=settings.query_max_rows,
    )
    app.state.chart_selector = ChartSelector()
    app.state.conversation_memory = ConversationMemory(
        max_turns=settings.conversation_max_turns,
        ttl_minutes=settings.conversation_ttl_minutes,
    )
    app.state.semantic_glossary = SemanticGlossary(
        app.state.ollama_client,
        model=settings.ollama_model,
    )
    app.state.feedback_store = FeedbackStore(
        few_shot_limit=settings.feedback_few_shot_limit,
    )
    app.include_router(connections_router)
    app.include_router(schema_router)
    app.include_router(query_router)
    app.include_router(conversation_router)
    app.include_router(semantic_router)
    app.include_router(feedback_router)

    static_dir = Path(__file__).resolve().parents[1] / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/", include_in_schema=False)
        def serve_root():
            index_path = static_dir / "index.html"
            if index_path.exists():
                return FileResponse(index_path)
            return {"message": "AI Business Analyst API"}

    return app


app = create_app()
