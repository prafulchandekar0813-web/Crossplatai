"""Entry point:  python server.py   (then open http://127.0.0.1:8080)

Environment: HOST (default 127.0.0.1, or 0.0.0.0 when PORT is set), PORT (default 8080), CONFIG_PATH, plus the settings listed in .env.example.
"""
import logging
import os
import sys

import uvicorn

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from llm_agent.api import create_app  # noqa: E402


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Hosting platforms (Render, Railway, Fly...) set PORT and require listening on all interfaces.
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    app = create_app(base_dir=os.path.dirname(os.path.abspath(__file__)))
    settings = app.state.settings
    cfg = settings.effective

    print("\n  LLM Agent")
    print(f"  Open:    http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    if settings.generated_admin_password:
        print(f"  Admin:   username '{cfg.admin.admin_username}'   password '{settings.generated_admin_password}'")
        print("           (generated now and shown only once; change it under Admin → Security)")
    else:
        print(f"  Admin:   username '{cfg.admin.admin_username}'")
    if not (cfg.openai.base_url and cfg.openai.model):
        print("  Model:   not configured yet - sign in as admin and open Admin → Model (or set OPENAI_BASE_URL / OPENAI_MODEL)")
    if host == "0.0.0.0":
        print("  Note:    listening on all interfaces; put it behind HTTPS if it is reachable from the internet")
    print()
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*" if host == "0.0.0.0" else "127.0.0.1"))


if __name__ == "__main__":
    main()
