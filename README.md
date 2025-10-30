# Bilibili Downloader API (Render Ready)

Minimal FastAPI service for downloading Bilibili videos with optional Google Drive delivery. This directory is a clean copy of the API server prepared for deployment on Render via Docker/GitHub.

## Local Development
- Install Python 3.11+.
- Create a virtual environment and install requirements:
  ```bash
  python -m venv .venv
  .\.venv\Scripts\activate
  pip install -r requirements.txt
  ```
- Copy `.env.example` to `.env` and fill the values you need for local runs. You can set either base64 encoded credentials or file paths.
- Start the API locally:
  ```bash
  uvicorn main:create_api_app --factory --host 0.0.0.0 --port 8000
  ```
- Health check: `http://localhost:8000/health`

## Deploying to Render
1. Push this directory to a new GitHub repository.
2. In Render, create a *Web Service* and choose **Deploy from GitHub**.
3. Render will detect `render.yaml`; select it as the blueprint when prompted.
4. Add the following Render environment variables (mark the sensitive ones as *Secret*):
   - `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`
   - `GOOGLE_OAUTH_TOKEN_JSON_BASE64` *(optional if you only use service accounts)*
   - `GOOGLE_DRIVE_FOLDER_ID`
   - `GOOGLE_DRIVE_SHARE_PUBLIC` (`true` or `false`)
5. Deploy. Render will build the Docker image using `Dockerfile` and expose the service on `https://<your-service>.onrender.com`.

### Persistent Storage (optional)
If you need downloaded files to survive restarts, create a Render persistent disk and mount it at `/app/downloads`. Otherwise the container filesystem is ephemeral and files will be cleaned on each redeploy.

## Project Structure
- `main.py` – FastAPI application and download logic.
- `Dockerfile` – Container image for Render/other clouds.
- `render.yaml` – Render blueprint defining the web service.
- `requirements.txt` – Python dependencies.
- `.dockerignore` / `.gitignore` – Ignore files for Docker and Git.
- `.env.example` – Template for local environment variables.
- `credentials/.gitkeep` – Placeholder; actual credential files are not stored in Git.
