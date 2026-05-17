from fastapi import FastAPI

app = FastAPI(title="Autognosys API")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "autognosys-api"}
