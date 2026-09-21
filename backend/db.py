"""Shared FalkorDB connection configured by the project .env."""

import os
from pathlib import Path

from dotenv import load_dotenv
from falkordb import FalkorDB, Graph


def get_graph() -> Graph:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    url = os.getenv("FALKORDB_URL") or "redis://localhost:6379"
    name = os.getenv("FALKORDB_GRAPH") or "bos"
    return FalkorDB.from_url(url).select_graph(name)
