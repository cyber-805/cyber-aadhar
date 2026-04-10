from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import requests
from urllib.parse import quote

app = FastAPI()

@app.get("/")
async def home(request: Request):
    try:
        # Input check (id_family)
        _id = request.query_params.get("id_family")
        if not _id:
            return JSONResponse({"error": "ID Not Provided"})

        # urlencode same as PHP
        _id = quote(_id)

        # Hex decode (same logic)
        part1 = bytes.fromhex("68747470733a2f2f736273616b69622e65752e63632f706169642f3f747970653d69645f66616d696c79267465726d3d").decode()
        part2 = bytes.fromhex("266b65793d44656d6f31").decode()

        # Final URL (same)
        _u = f"{part1}{_id}{part2}"

        # Request
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0"
        }

        response = requests.get(_u, headers=headers, timeout=25, verify=False)

        if response.status_code == 200:
            _d = response.json()

            if _d.get("success") == True:
                return JSONResponse({
                    "status": "success",
                    "result": _d.get("result")
                })
            else:
                return JSONResponse({
                    "status": "error",
                    "message": "Invalid Response"
                })
        else:
            return JSONResponse({
                "status": "error",
                "message": "Connection Failed"
            })

    except Exception:
        return JSONResponse({
            "status": "error",
            "message": "Internal System Error"
        })
