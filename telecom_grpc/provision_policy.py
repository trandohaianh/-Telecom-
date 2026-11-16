"""One-time explicit creation of an empty policy namespace; never run on server boot."""
import asyncio
import os
from urllib.parse import quote, urlsplit, urlunsplit
from ram_online.core import PolicyStore, make_client


async def main():
    url = os.getenv("POLICY_REDIS_URL", "redis://redis-policy:6379/0")
    password_file = os.getenv("POLICY_REDIS_PASSWORD_FILE")
    if password_file:
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, ":" + quote(open(password_file, encoding="utf-8").read().strip(), safe="") + "@" + parts.netloc, parts.path, parts.query, parts.fragment))
    client = make_client(url, max_connections=1, io_timeout=1.0)
    try:
        await PolicyStore(client, os.getenv("POLICY_NAMESPACE", "telecom-policy")).initialize_empty_policy()
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
