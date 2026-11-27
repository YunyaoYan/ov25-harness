"""The command line.

    ov25-harness serve --config FILE [--host HOST] [--port PORT]
    ov25-harness check URL IMAGE [--voice] [--skip-recording] [--language en|zh]
                       [--live-idle-s SECONDS]
    ov25-harness version
"""
import argparse
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import sys

from . import __version__


def setup_logging(server):
    """The package's logs to stderr and, with server.log_dir, to a file rotated daily."""
    handlers = [logging.StreamHandler()]
    if server.log_dir:
        directory = Path(server.log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # Only this service's own rotated files are ever deleted, after log_keep_days.
        handlers.append(TimedRotatingFileHandler(directory / "ov25-harness.log", when="midnight",
                                                 backupCount=server.log_keep_days, encoding="utf-8"))
    logger = logging.getLogger("ov25_harness")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def serve(args):
    from pydantic import ValidationError
    import uvicorn

    from . import config
    from .app import build_app
    from .backends import make_backends

    try:
        settings = config.load(args.config)
        overrides = {key: value for key, value in (("host", args.host), ("port", args.port)) if value is not None}
        if overrides:
            data = settings.model_dump()
            data["server"].update(overrides)
            settings = config.Settings.model_validate(data)
    except (OSError, ValueError, ValidationError) as error:
        # ValidationError names the field at fault.
        sys.exit(f"{args.config}: {error}")
    server = settings.server
    setup_logging(server)
    chat, streaming, transcriber, speaker = make_backends(settings)
    app = build_app(settings, chat=chat, streaming=streaming, transcriber=transcriber, speaker=speaker)
    uvicorn.run(app, host=server.host, port=server.port)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "check":
        # The check has its own arguments; it is a client of a running service.
        from .check import main as check

        return check(argv[1:])
    parser = argparse.ArgumentParser(prog="ov25-harness", description="A live video question-answering service.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve", help="run the service")
    run.add_argument("--config", required=True, help="the TOML settings file")
    run.add_argument("--host", help="override [server] host")
    run.add_argument("--port", type=int, help="override [server] port")
    commands.add_parser("check", help="check a running service end to end (see check --help)")
    commands.add_parser("version", help="print the version")
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
