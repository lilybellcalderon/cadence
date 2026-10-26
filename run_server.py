"""Start the operator console from any working directory:  python run_server.py [--port 8000] [--seed N]"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from app.server import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
