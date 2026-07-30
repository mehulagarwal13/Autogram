import numpy as np


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """
    Computes cosine similarity between two vectors.
    Since our embeddings are already normalized (Node 6), this reduces
    to a plain dot product — but we compute it properly here anyway,
    in case a future embedding source isn't pre-normalized.
    """
    a = np.array(vec_a)
    b = np.array(vec_b)

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return float(np.dot(a, b) / (norm_a * norm_b))