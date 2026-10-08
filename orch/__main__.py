import sys

if sys.version_info < (3, 11):
    sys.stderr.write("MAO orch 需要 Python 3.11 或更新版本（当前 %s）\n" % sys.version.split()[0])
    sys.exit(2)

from .cli import main  # noqa: E402

sys.exit(main())
