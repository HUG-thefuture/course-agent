"""素材接入子包。"""

from .loader import IngestError, chunk_material, fetch_url, load_source

__all__ = ["IngestError", "chunk_material", "fetch_url", "load_source"]
