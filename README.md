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

1. Open `http://127.0.0.1:5000/` and pick a user from the landing page dropdown.
2. After continuing, use the dashboard at `/conversations` to log summaries and review history.
3. Use the “Switch user” button to return to the landing page when you need to change identity.
