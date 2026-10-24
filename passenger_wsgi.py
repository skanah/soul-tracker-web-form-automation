"""cPanel / Passenger WSGI entrypoint.

In cPanel's "Setup Python App":
  - Application root : this project folder
  - Startup file     : passenger_wsgi.py   (WSGI entry: `application`)
  - Environment      : AUTO_RESUME=1, FLASK_DEBUG=0
                       SOCKETIO_ASYNC_MODE=threading
                       (set automatically below when not already set)

With AUTO_RESUME=1 the worker starts with the process: uploads keep being
added/tracked even when every browser is closed.
"""
import os

os.environ.setdefault("FLASK_DEBUG", "0")
os.environ.setdefault("SOCKETIO_ASYNC_MODE", "threading")

# data/job.db and data/*.csv are relative paths - pin the working directory
# to the app folder no matter where the host starts the process from.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import app  # noqa: E402  (must follow the env defaults above)

application = app
