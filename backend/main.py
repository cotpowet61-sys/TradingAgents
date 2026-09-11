from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn
from config import settings
import asyncio

app = FastAPI(title="TradingAgents", version="1.0.0")

# Подключаем статические файлы (frontend)
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

@app.get("/")
async def root():
    with open("../frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/health")
async def health():
    return {"status": "ok", "port": settings.PORT}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Здесь будет логика агентов
            await websocket.send_json({
                "type": "log",
                "agent": "Scanner",
                "message": "Система запущена, ожидание подключения к Bybit..."
            })
            await asyncio.sleep(5)
    except Exception as e:
        print(f"WebSocket error: {e}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
