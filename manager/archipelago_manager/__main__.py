"""`python -m archipelago_manager` starts the web server."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("archipelago_manager.main:app", host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8080")), log_level="info", access_log=False)
