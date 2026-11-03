"""OpenAI-compatible chat/embedding client. Provider secrets stay server-side."""

import json
import math
import time
from app.observability import record
import asyncio

import httpx

from app.config import settings


def require_llm(embedding: bool = False):
    if not settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("LLM_BASE_URL 或 LLM_API_KEY 未配置")
    if embedding and not settings.embedding_model:
        raise RuntimeError("EMBEDDING_MODEL 未配置")


def _headers():
    return {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}


def _chat_payload(messages, model, temperature=None, stream=False):
    payload = {"model": model, "messages": messages, "stream": stream,
               "max_tokens": settings.chat_max_output_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


async def _chat_chunks(messages: list[dict], model: str, temperature: float | None = None):
    """Yields only user-visible content deltas, not reasoning fields."""
    require_llm()
    timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{settings.llm_base_url}/chat/completions",
            headers=_headers(), json=_chat_payload(messages, model, temperature, True),
        ) as response:
            response.raise_for_status()
            data_lines = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line == "" and data_lines:
                    data = "\n".join(data_lines)
                    data_lines.clear()
                    if data == "[DONE]":
                        return
                    delta = extract_delta(data)
                    if delta:
                        yield delta
            if data_lines and "\n".join(data_lines) != "[DONE]":
                delta = extract_delta("\n".join(data_lines))
                if delta:
                    yield delta


def extract_delta(data: str) -> str:
    payload = json.loads(data)
    delta = payload.get("choices", [{}])[0].get("delta", {})
    value = delta.get("content", "")
    return value if isinstance(value, str) else ""


def _validate_vector(vector):
    if not isinstance(vector, list) or len(vector) != settings.embedding_dimensions:
        raise ValueError(f"向量维度不符合配置的 {settings.embedding_dimensions}")
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("向量包含无效数值")
    return values


def _embedding_payload(texts: list[str]):
    return {
        "model": settings.embedding_model,
        "input": texts,
        "dimensions": settings.embedding_dimensions,
    }


def _read_embeddings(response: httpx.Response, expected: int):
    response.raise_for_status()
    rows = sorted(response.json()["data"], key=lambda row: row["index"])
    if len(rows) != expected:
        raise ValueError("模型返回的向量数量与输入数量不一致")
    return [_validate_vector(row["embedding"]) for row in rows]


async def _embed_texts(texts: list[str]) -> list[list[float]]:
    require_llm(embedding=True)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{settings.llm_base_url}/embeddings", headers=_headers(), json=_embedding_payload(texts),
        )
    return _read_embeddings(response, len(texts))


def _embed_texts_sync(texts: list[str]) -> list[list[float]]:
    require_llm(embedding=True)
    with httpx.Client(timeout=60) as client:
        response = client.post(
            f"{settings.llm_base_url}/embeddings", headers=_headers(), json=_embedding_payload(texts),
        )
    return _read_embeddings(response, len(texts))


async def chat_chunks(messages,model,temperature=None):
    started=time.perf_counter()
    outcome="success"
    try:
        async for chunk in _chat_chunks(messages,model,temperature): yield chunk
    except (asyncio.CancelledError,GeneratorExit):
        outcome="cancelled"
        raise
    except Exception:
        outcome="error"
        raise
    finally:
        await asyncio.shield(asyncio.to_thread(record,"chat",outcome,time.perf_counter()-started))


async def embed_texts(texts):
    started=time.perf_counter()
    outcome="success"
    try: return await _embed_texts(texts)
    except asyncio.CancelledError:
        outcome="cancelled"
        raise
    except Exception:
        outcome="error"
        raise
    finally:
        await asyncio.shield(asyncio.to_thread(record,"embedding",outcome,time.perf_counter()-started,units=len(texts)))


def embed_texts_sync(texts):
    started=time.perf_counter()
    outcome="success"
    try: return _embed_texts_sync(texts)
    except Exception:
        outcome="error"
        raise
    finally: record("embedding",outcome,time.perf_counter()-started,units=len(texts))


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in _validate_vector(values)) + "]"
