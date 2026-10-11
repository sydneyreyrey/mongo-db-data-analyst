"""MongoDB Atlas connection (workspaces, harness versions, runs, experiments)."""

from functools import lru_cache

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from common.settings import get_settings


@lru_cache
def get_client() -> AsyncMongoClient:
    uri = get_settings().mongodb_uri
    if not uri:
        raise RuntimeError("MONGODB_URI is not set. Copy .env.example to .env and fill it in.")
    return AsyncMongoClient(uri, appname="recursive-analyst", serverSelectionTimeoutMS=10_000)


def get_db() -> AsyncDatabase:
    return get_client()[get_settings().mongodb_db]


async def ping() -> bool:
    result = await get_client().admin.command("ping")
    return result.get("ok") == 1
