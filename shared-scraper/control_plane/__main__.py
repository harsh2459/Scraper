import logging
from waitress import serve
from .app import create_app, start_dispatcher
from .config import Settings

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    app = create_app(settings)
    start_dispatcher(app)
    serve(app, host='127.0.0.1', port=settings.port, threads=8)
