import uvicorn
from workmemory.config import get_settings

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run(
        "workmemory.main:create_app",
        factory=True,
        host=s.server.host,
        port=s.server.port,
        workers=1,
        access_log=False,
        # Persistent MCP/event streams must not keep managed shutdown waiting forever.
        timeout_graceful_shutdown=10,
    )
