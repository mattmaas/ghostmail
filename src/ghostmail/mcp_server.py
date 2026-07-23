"""GhostMail MCP Server - Research emails via MCP protocol."""

import asyncio
import json
import logging
from typing import Any, Optional

from .config import get_settings
from .database import get_database
from .gmail_gateway import get_gateway
from .modules.research import ResearchModule

logger = logging.getLogger(__name__)

# MCP Server implementation
# This provides a simple HTTP server that exposes GhostMail capabilities via JSON-RPC


class GhostMailMCPServer:
    """MCP server for GhostMail research capabilities."""

    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.settings = get_settings()
        self._gateway = None
        self._router = None
        self._db = None
        self._research = None

    @property
    def gateway(self):
        if self._gateway is None:
            self._gateway = get_gateway()
        return self._gateway

    @property
    def router(self):
        if self._router is None:
            from .ai_engine import get_router

            self._router = get_router()
        return self._router

    @property
    def db(self):
        if self._db is None:
            self._db = get_database()
        return self._db

    @property
    def research(self):
        if self._research is None:
            self._research = ResearchModule(
                self.gateway,
                self.router,
                self.db,
            )
        return self._research

    async def handle_request(self, request: dict) -> dict:
        """Handle incoming JSON-RPC request."""
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")

        try:
            if method == "research":
                result = await self.research.research(
                    query=params.get("query", ""),
                    max_emails=params.get("max_emails", 50),
                    use_local=params.get("use_local", False),
                )
            elif method == "search":
                result = await self.research.quick_search(
                    query=params.get("query", ""),
                    max_results=params.get("max_results", 10),
                )
            elif method == "get_inbox":
                result = self._get_inbox(params.get("max_results", 20))
            elif method == "get_email":
                result = self._get_email(params.get("email_id"))
            else:
                raise ValueError(f"Unknown method: {method}")

            return {
                "jsonrpc": "2.0",
                "result": result,
                "id": request_id,
            }

        except Exception as e:
            logger.error(f"MCP error: {e}")
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": str(e),
                },
                "id": request_id,
            }

    def _get_inbox(self, max_results: int = 20) -> list:
        """Get recent inbox emails."""
        messages, _ = self.gateway.list_messages(
            query="in:inbox",
            max_results=max_results,
        )

        results = []
        for msg in messages:
            try:
                full = self.gateway.get_message(msg["id"], format="metadata")
                headers = {
                    h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
                }

                results.append(
                    {
                        "id": full["id"],
                        "from": headers.get("From", ""),
                        "subject": headers.get("Subject", ""),
                        "date": headers.get("Date", ""),
                        "snippet": full.get("snippet", ""),
                    }
                )
            except Exception:
                continue

        return results

    def _get_email(self, email_id: str) -> Optional[dict]:
        """Get a specific email by ID."""
        try:
            full = self.gateway.get_message(email_id, format="full")
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}

            # Get body
            body = ""
            payload = full.get("payload", {})
            if "parts" in payload:
                for part in payload["parts"]:
                    if part.get("mimeType") == "text/plain":
                        if "data" in part.get("body", {}):
                            import base64

                            try:
                                body = base64.urlsafe_b64decode(part["body"]["data"]).decode(
                                    "utf-8", errors="ignore"
                                )
                            except Exception:
                                pass
                        break

            return {
                "id": full["id"],
                "thread_id": full.get("threadId", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "body": body[:5000],
                "snippet": full.get("snippet", ""),
                "labels": full.get("labelIds", []),
            }
        except Exception as e:
            return {"error": str(e)}


async def run_server(host: str = "localhost", port: int = 8765):
    """Run the MCP server."""
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="GhostMail MCP Server")
    mcp_server = GhostMailMCPServer(host=host, port=port)

    @app.post("/mcp")
    async def handle_mcp(request: Request):
        """Handle MCP JSON-RPC requests."""
        body = await request.json()
        result = await mcp_server.handle_request(body)
        return JSONResponse(result)

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok", "service": "ghostmail-mcp"}

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def start_server(host: str = "localhost", port: int = 8765):
    """Start the MCP server (blocking)."""
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="GhostMail MCP Server")
    mcp_server = GhostMailMCPServer(host=host, port=port)

    @app.post("/mcp")
    async def handle_mcp(request: Request):
        body = await request.json()
        result = await mcp_server.handle_request(body)
        return JSONResponse(result)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "ghostmail-mcp"}

    uvicorn.run(app, host=host, port=port)


# CLI command to start server
def add_mcp_commands(app):
    """Add MCP server commands to the CLI."""
    import typer

    @app.command("mcp-server")
    def mcp_server(
        host: str = typer.Option("localhost", "--host", help="Host to bind to"),
        port: int = typer.Option(8765, "--port", help="Port to bind to"),
    ):
        """Start the MCP server for email research."""
        from rich.console import Console

        console = Console()
        console.print(f"[bold]Starting GhostMail MCP Server...[/bold]")
        console.print(f"  Host: {host}")
        console.print(f"  Port: {port}")
        console.print(f"\n  MCP endpoint: http://{host}:{port}/mcp")
        console.print(f"  Health: http://{host}:{port}/health")
        console.print("\n  Example JSON-RPC call:")
        console.print("""  {
  "jsonrpc": "2.0",
  "method": "research",
  "params": {"query": "job applications", "max_emails": 20},
  "id": 1
}""")

        start_server(host=host, port=port)


if __name__ == "__main__":
    start_server()
