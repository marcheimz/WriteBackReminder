# WriteBackReminder

Simple Quart-based web app for logging conversations and planning reminders.

## Setup

The project expects a Python 3.8 virtual environment named `.venv38`.

```bash
python3 -m venv .venv38
source .venv38/bin/activate
pip install -r requirements.txt
```

## Running the server

With the environment activated, start the development server with Quart:

```bash
quart --app main:app run --reload
```

Alternatively, you can launch it directly via Python:

```bash
python main.py
```

The app listens on `http://127.0.0.1:5000/` by default.

## Usage

1. Open `http://127.0.0.1:5000/` and pick a user from the landing page dropdown or click **Sign in with Google**.
2. After continuing, use the dashboard at `/conversations` to log summaries and review history.
3. Use the “Switch user” button to return to the landing page when you need to change identity.

## Google Sign-In

Google OAuth is optional and only enabled when client credentials are available.

1. Create an OAuth 2.0 Web Application credential in the [Google Cloud Console](https://console.cloud.google.com/).
2. Set the authorized redirect URI to `http://127.0.0.1:5000/auth/google` (match your deployment host/port).
3. Save the client credentials to `google_oauth.json` in the project root (or point `GOOGLE_CREDENTIALS_FILE` to a custom path). The file must look like:
   ```json
   {
     "client_id": "your-client-id.apps.googleusercontent.com",
     "client_secret": "your-client-secret"
   }
   ```
4. Restart the Quart app. When the file is present, the landing page will offer a **Sign in with Google** button that maps the authenticated email to an internal user defined in `GOOGLE_EMAIL_TO_USER`.

> Tip: If you prefer a different location for the credentials file, set `GOOGLE_CREDENTIALS_FILE=/absolute/path/to/file.json` before starting Quart.
