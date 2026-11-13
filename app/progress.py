# In-memory state shared by the web API and the worker threads.
# PROGRESS[episode_id] = {"phase": "downloading" or "scanning", "done": bytes, "total": bytes, "fraction": 0..1}
# ERRORS[episode_id] = short error text for the queue page (cleared on restart)
# PLAYING[episode_id] = time the page last asked for the audio or saved progress, so cleanup leaves it alone
PROGRESS = {}
ERRORS = {}
PLAYING = {}
