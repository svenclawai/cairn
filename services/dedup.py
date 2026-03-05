from database import get_supabase
from services.embeddings import embed


async def is_semantically_duplicate(
    session_id: str,
    candidate_phrase: str,
    threshold: float = 0.92,
) -> tuple[bool, list[dict]]:
    embedding = await embed(candidate_phrase)
    sb = get_supabase()
    result = sb.rpc(
        "similar_queries",
        {
            "p_session_id": session_id,
            "p_embedding": embedding,
            "p_threshold": threshold,
            "p_limit": 5,
        },
    ).execute()
    similar = result.data or []
    return len(similar) > 0, similar, embedding
