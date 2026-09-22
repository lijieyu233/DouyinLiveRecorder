import os
import sys
import gettext
import inspect
import builtins
from pathlib import Path

from src import paths


def init_gettext(locale_dir, locale_name):
    gettext.bindtextdomain('zh_CN', locale_dir)
    gettext.textdomain('zh_CN')
    os.environ['LANG'] = f'{locale_name}.utf8'
    return gettext.gettext


#: 项目根目录。上游用 ``sys.argv[0]`` 推导，模块方式启动时会把
#: ``i18n/`` 找错地方，这里统一走 src.paths。
execute_dir = paths.slash(paths.project_root())
if os.path.exists(Path(execute_dir) / '_internal/i18n'):
    locale_path = Path(execute_dir) / '_internal/i18n'
else:
    locale_path = Path(execute_dir) / 'i18n'
_tr = init_gettext(locale_path, 'zh_CN')
original_print = builtins.print
package_name = 'src'


def translated_print(*args, **kwargs):
    for arg in args:
        if package_name in inspect.stack()[1].filename:
            translated_arg = _tr(str(arg))
        else:
            translated_arg = str(arg)
        original_print(translated_arg, **kwargs)
