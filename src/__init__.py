import os
from pathlib import Path
from . import paths
from .initializer import check_node

current_file_path = Path(__file__).resolve()
current_dir = current_file_path.parent
JS_SCRIPT_PATH = current_dir / 'javascript'

execute_dir = paths.slash(paths.project_root())
node_execute_dir = Path(execute_dir) / 'node'
current_env_path = os.environ.get('PATH')
os.environ['PATH'] = str(node_execute_dir) + os.pathsep + current_env_path
check_node()
