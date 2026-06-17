from __future__ import annotations

import sys
from contextlib import closing

from monitoring_board import app_factory as _app_factory
from monitoring_board.app_factory import *  # noqa: F401,F403 - compatibility re-exports


app = _app_factory.app


if __name__ == "__main__":
    args = _app_factory.parse_cli_args()
    if _app_factory.DEFAULT_EXCEL_PATH and not _app_factory.DB_PATH.exists():
        with closing(_app_factory.get_db(str(_app_factory.DB_PATH))) as conn:
            _app_factory.import_excel_data(conn, _app_factory.DEFAULT_EXCEL_PATH)
    elif _app_factory.DEFAULT_EXCEL_PATH:
        with closing(_app_factory.get_db(str(_app_factory.DB_PATH))) as conn:
            if _app_factory.query_scalar(conn, "SELECT COUNT(*) FROM assets") == 0:
                _app_factory.import_excel_data(conn, _app_factory.DEFAULT_EXCEL_PATH)
    app.run(host=args.host, port=args.port, debug=args.debug)
else:
    sys.modules[__name__] = _app_factory
