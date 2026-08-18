from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.charting.chart_selector import ChartSelector, ChartSpec
from app.conversation.memory import ConversationMemory, ConversationTurn
from app.db.connection import ConnectionNotFoundError
from app.db.introspection import SchemaIntrospectionError
from app.db.query_executor import (
    QueryExecutionError,
    QueryExecutor,
    QueryResult,
    QueryTimeoutError,
    UnsafeQueryError,
)
from app.db.sql_safety import SQLSafetyValidator
from app.llm.ollama_client import (
    OllamaAPIError,
    OllamaError,
    OllamaTimeoutError,
    OllamaUnreachableError,
)
from app.llm.result_interpreter import ResultInterpretationError, ResultInterpreter
from app.llm.sql_generator import (
    SQLGenerationError,
    SQLGenerationParseError,
    SQLGenerationResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["query"])


class SQLGenerationRequest(BaseModel):
    question: str = Field(min_length=1)


class SQLExecutionRequest(BaseModel):
    sql: str = Field(min_length=1)


class AskQueryRequest(BaseModel):
    question: str = Field(min_length=1)
    interpret: bool = True
    chart: bool = True
    # Optional conversation thread ID.  If omitted, a new one is generated
    # server-side and returned in the response so callers can pass it on
    # subsequent requests to continue the same context thread.
    conversation_id: str | None = None


class AskQueryResponse(BaseModel):
    generated_sql: str
    query_result: QueryResult
    interpretation: str | None = None
    warning: str | None = None
    chart: ChartSpec | None = None
    conversation_id: str


def _get_schema_introspector(request: Request):
    introspector = getattr(request.app.state, "schema_introspector", None)
    if introspector is None:
        raise RuntimeError("Schema introspector is not configured on the application.")
    return introspector


def _get_sql_generator(request: Request):
    generator = getattr(request.app.state, "sql_generator", None)
    if generator is None:
        raise RuntimeError("SQL generator is not configured on the application.")
    return generator


def _get_query_executor(request: Request) -> QueryExecutor:
    executor = getattr(request.app.state, "query_executor", None)
    if executor is None:
        raise RuntimeError("Query executor is not configured on the application.")
    return executor


def _get_result_interpreter(request: Request) -> ResultInterpreter:
    interpreter = getattr(request.app.state, "result_interpreter", None)
    if interpreter is None:
        raise RuntimeError("Result interpreter is not configured on the application.")
    return interpreter


def _get_chart_selector(request: Request) -> ChartSelector:
    selector = getattr(request.app.state, "chart_selector", None)
    if selector is None:
        selector = ChartSelector()
    return selector


def _get_conversation_memory(request: Request) -> ConversationMemory:
    memory = getattr(request.app.state, "conversation_memory", None)
    if memory is None:
        # Fallback: construct a default instance rather than hard-failing,
        # so endpoints that predate conversation support keep working.
        return ConversationMemory()
    return memory


def _get_semantic_glossary(request: Request):
    return getattr(request.app.state, "semantic_glossary", None)


def _get_feedback_store(request: Request):
    return getattr(request.app.state, "feedback_store", None)


@router.post(
    "/{connection_id}/query/generate-sql",
    response_model=SQLGenerationResult,
    description=(
        "Generate a candidate SQL SELECT query only. "
        "This endpoint intentionally does not execute the SQL yet; "
        "execution is deferred to the safety layer in the next step."
    ),
)
def generate_sql(
    connection_id: str,
    payload: SQLGenerationRequest,
    request: Request,
) -> SQLGenerationResult:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    introspector = _get_schema_introspector(request)
    generator = _get_sql_generator(request)
    glossary_store = _get_semantic_glossary(request)
    feedback_store = _get_feedback_store(request)

    try:
        if connection_manager is None:
            raise RuntimeError("Connection manager is not configured on the application.")
        engine = connection_manager.get_engine(connection_id)
        schema_info = introspector.get_schema(connection_id)
        semantic_context = (
            glossary_store.to_llm_context(connection_id) if glossary_store is not None else ""
        )
        positive_entries = (
            feedback_store.get_positive_examples(connection_id) if feedback_store is not None else []
        )
        few_shot_examples = (
            [{"question": e.question, "sql": e.generated_sql} for e in positive_entries]
            if positive_entries
            else None
        )
        import inspect
        kwargs: dict = {
            "dialect": engine.dialect.name,
        }
        try:
            sig = inspect.signature(generator.generate_sql)
            if "few_shot_examples" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["few_shot_examples"] = few_shot_examples
            if "semantic_context" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["semantic_context"] = semantic_context or None
        except (ValueError, TypeError):
            pass

        return generator.generate_sql(
            payload.question,
            schema_info,
            **kwargs,
        )
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except OllamaUnreachableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Ollama is not reachable at {exc.base_url}. Is it running?",
        ) from None
    except OllamaTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from None
    except OllamaAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from None
    except SQLGenerationParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from None
    except SQLGenerationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from None
    except SchemaIntrospectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from None


@router.post(
    "/{connection_id}/query/execute",
    response_model=QueryResult,
    description="Validate and execute a caller-supplied SQL SELECT query.",
)
def execute_sql(
    connection_id: str,
    payload: SQLExecutionRequest,
    request: Request,
) -> QueryResult:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    executor = _get_query_executor(request)

    try:
        if connection_manager is None:
            raise RuntimeError("Connection manager is not configured on the application.")
        connection_manager.get_engine(connection_id)
        return executor.execute(connection_id, payload.sql)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except UnsafeQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.reason) from None
    except QueryTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from None
    except QueryExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post(
    "/{connection_id}/query/ask",
    response_model=AskQueryResponse,
    description="Generate SQL for a question, validate it, execute it, and return the results.",
)
def ask_and_execute_sql(
    connection_id: str,
    payload: AskQueryRequest,
    request: Request,
) -> AskQueryResponse:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    introspector = _get_schema_introspector(request)
    generator = _get_sql_generator(request)
    executor = _get_query_executor(request)
    interpreter = _get_result_interpreter(request)
    chart_selector = _get_chart_selector(request)
    memory = _get_conversation_memory(request)

    if connection_manager is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Connection manager is not configured on the application.",
        )

    # Resolve / validate conversation_id before touching the LLM.
    conversation_id = payload.conversation_id or str(uuid4())
    stored_conn = memory.get_connection_id(conversation_id)
    if stored_conn is not None and stored_conn != connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Conversation '{conversation_id}' was created against connection "
                f"'{stored_conn}', not '{connection_id}'. "
                "Pass a new conversation_id to start a fresh thread."
            ),
        )

    # Fetch history (empty list for a brand-new conversation).
    history = memory.get_history(conversation_id, connection_id=connection_id) if stored_conn else []

    # Fetch semantic context (empty string if no glossary generated yet).
    glossary_store = _get_semantic_glossary(request)
    semantic_context = (
        glossary_store.to_llm_context(connection_id) if glossary_store is not None else ""
    )

    # Fetch positive feedback examples for few-shot prompting.
    feedback_store = _get_feedback_store(request)
    positive_entries = (
        feedback_store.get_positive_examples(connection_id) if feedback_store is not None else []
    )
    few_shot_examples = (
        [{"question": e.question, "sql": e.generated_sql} for e in positive_entries]
        if positive_entries
        else None
    )

    try:
        engine = connection_manager.get_engine(connection_id)
        schema_info = introspector.get_schema(connection_id)
        validator = getattr(executor, "_validator", None) or SQLSafetyValidator()

        if hasattr(generator, "generate_valid_sql"):
            target_fn = generator.generate_valid_sql
            kwargs: dict = {
                "validator": validator,
                "dialect": engine.dialect.name,
            }
        else:
            target_fn = generator.generate_sql
            kwargs = {
                "dialect": engine.dialect.name,
            }

        import inspect
        try:
            sig = inspect.signature(target_fn)
            accepts_few_shot = "few_shot_examples" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if accepts_few_shot:
                kwargs["few_shot_examples"] = few_shot_examples

            accepts_history = "conversation_history" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if accepts_history:
                kwargs["conversation_history"] = history or None

            accepts_semantic = "semantic_context" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if accepts_semantic:
                kwargs["semantic_context"] = semantic_context or None
        except (ValueError, TypeError):
            pass

        generation = target_fn(payload.question, schema_info, **kwargs)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except OllamaUnreachableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Ollama is not reachable at {exc.base_url}. Is it running?",
        ) from None
    except OllamaTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from None
    except OllamaAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from None
    except SQLGenerationParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from None
    except SQLGenerationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from None
    except SchemaIntrospectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from None

    if not generation.validation_passed:
        rejection_reason = generation.rejection_reason or "Generated SQL failed safety validation."
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Generated SQL was rejected: {rejection_reason}",
        )

    try:
        query_result = executor.execute(connection_id, generation.sql)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except UnsafeQueryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Generated SQL was rejected: {exc.reason}",
        ) from None
    except QueryTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from None
    except QueryExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    interpretation_text: str | None = None
    warning_text: str | None = None
    if payload.interpret:
        try:
            interp_kwargs: dict = {}
            import inspect
            try:
                sig = inspect.signature(interpreter.interpret)
                if "semantic_context" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    interp_kwargs["semantic_context"] = semantic_context or None
            except (ValueError, TypeError):
                pass

            interpretation = interpreter.interpret(
                payload.question,
                generation.sql,
                query_result,
                **interp_kwargs,
            )
            interpretation_text = interpretation.answer
        except (ResultInterpretationError, OllamaError) as exc:
            warning_text = f"Interpretation unavailable: {exc}"
            logger.warning(
                "Interpretation unavailable for connection %s: %s",
                connection_id,
                exc,
            )

    chart_spec: ChartSpec | None = None
    if payload.chart:
        try:
            chart_spec = chart_selector.select_chart(query_result)
        except Exception as exc:
            logger.warning(
                "Chart generation failed for connection %s: %s",
                connection_id,
                exc,
            )
            chart_spec = None

    # Record this turn in conversation memory.
    result_summary: dict = {
        "columns": query_result.columns,
        "row_count": query_result.row_count,
        "sample_rows": query_result.rows[:3] if query_result.rows else [],
    }
    try:
        memory.add_turn(
            conversation_id,
            ConversationTurn(
                question=payload.question,
                generated_sql=generation.sql,
                dialect=engine.dialect.name,
                query_result_summary=result_summary,
            ),
            connection_id=connection_id,
        )
    except Exception as exc:
        logger.warning(
            "Failed to record conversation turn for conversation %s: %s",
            conversation_id,
            exc,
        )

    return AskQueryResponse(
        generated_sql=generation.sql,
        query_result=query_result,
        interpretation=interpretation_text,
        warning=warning_text,
        chart=chart_spec,
        conversation_id=conversation_id,
    )
