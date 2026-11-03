"""Make one small real request to each configured model before staging ingestion."""

import json

import httpx

from app.config import settings
from app.llm import _chat_payload, _embedding_payload, _headers, _read_embeddings, require_llm


def check_response(name: str, response: httpx.Response):
    if response.is_success:
        return
    try:
        body = response.json()
        code = body.get("code") or body.get("error", {}).get("code")
    except (ValueError, AttributeError):
        code = None
    raise RuntimeError(f"{name} 接口返回 HTTP {response.status_code}"
                       + (f"，供应商错误码 {code}" if code else ""))


def main():
    require_llm(embedding=True)
    with httpx.Client(timeout=45) as client:
        chat = client.post(f"{settings.llm_base_url}/chat/completions",
                           headers=_headers(), json=_chat_payload(
                               [{"role": "user", "content": "请回答：收到"}],
                               settings.customer_model, 0, False))
        check_response("聊天模型", chat)
        if not chat.json().get("choices"):
            raise RuntimeError("聊天模型未返回 choices")
        embedding = client.post(f"{settings.llm_base_url}/embeddings",
                                headers=_headers(), json=_embedding_payload(["预发布模型连通性检查"]))
        check_response("Embedding 模型", embedding)
        vector = _read_embeddings(embedding, 1)[0]
    print(json.dumps({"chatModel": settings.customer_model,
                      "embeddingModel": settings.embedding_model,
                      "embeddingDimensions": len(vector), "passed": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
