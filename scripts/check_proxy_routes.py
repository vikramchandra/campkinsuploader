"""Exercise the proxy settings routes against the real FastAPI app.

Run from the project root:  .venv\\Scripts\\python.exe scripts\\check_proxy_routes.py

Uses an in-process test client, so no server or browser window is needed.
The "none" check makes one request to api.ipify.org, nothing else leaves
the machine. Settings are restored afterwards.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import SETTINGS, save_settings  # noqa: E402
from app.main import app  # noqa: E402


def main() -> int:
    saved = {key: getattr(SETTINGS, key) for key in
             ("proxy_provider", "proxy_username", "proxy_password")}
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
        if not ok:
            failures += 1

    with TestClient(app) as client:
        settings = client.get("/api/settings").json()
        keys = [p["key"] for p in settings.get("proxy_providers", [])]
        check("GET /api/settings lists providers", keys == ["none", "dataimpulse"],
              str(keys))
        check("GET /api/settings has proxy fields",
              all(k in settings for k in saved), "")

        r = client.post("/api/settings", json={"proxy_provider": "bogus"})
        check("POST unknown provider is a 400", r.status_code == 400,
              r.text[:80])

        r = client.post("/api/proxy/check", json={
            "proxy_provider": "dataimpulse", "proxy_username": "",
            "proxy_password": ""}).json()
        check("check with blank DataImpulse credentials fails cleanly",
              r["ok"] is False and "login" in r["error"], r["error"])

        r = client.post("/api/proxy/check", json={
            "proxy_provider": "dataimpulse", "proxy_username": "wronguser",
            "proxy_password": "wrongpass"}).json()
        check("check with wrong DataImpulse credentials is refused",
              r["ok"] is False and r["error"], r["error"])

        r = client.post("/api/proxy/check", json={"proxy_provider": "none"}).json()
        check("check with no proxy returns this machine's IP",
              r["ok"] is True and r["ip"], r.get("error") or r["ip"])

        r = client.post("/api/settings", json={
            "proxy_provider": "dataimpulse", "proxy_username": "u",
            "proxy_password": "p"}).json()
        check("POST saves proxy settings",
              r["proxy_provider"] == "dataimpulse" and r["proxy_username"] == "u",
              "")

    save_settings(saved)
    print("settings restored")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
