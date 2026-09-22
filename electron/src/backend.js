/**
 * Python 后端进程的生命周期管理。
 *
 * 职责：
 * 1. 找到可用的 Python 解释器（优先项目内 .venv）；
 * 2. 用随机令牌 + 随机端口拉起 `python -m dylr`，读取它打印的就绪行；
 * 3. 把后端的 stdout/stderr 转发给渲染进程，用于「启动日志」面板；
 * 4. 退出时先请求优雅停止（/api/quit），再逐级降级到 SIGTERM / kill，
 *    避免上游那个「强杀 Python 留下孤儿 ffmpeg」的老问题。
 */
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const READY_PATTERN = /DYLR_READY\s+port=(\d+)\s+token=(\S+)/;
/** 后端启动进度：`DYLR_STAGE <文本>`，每个里程碑一行。 */
const STAGE_PATTERN = /^DYLR_STAGE\s+(.+)$/;
const START_TIMEOUT_MS = 60000;
const STOP_TIMEOUT_MS = 15000;

class BackendService {
  /**
   * @param {{root:string, onStatus:(status:object)=>void, onLog:(line:object)=>void,
   *          onStage?:(text:string)=>void}} options
   */
  constructor(options) {
    this.root = options.root;
    this.onStatus = options.onStatus || (() => {});
    this.onLog = options.onLog || (() => {});
    this.onStage = options.onStage || (() => {});
    this.process = null;
    this.baseUrl = '';
    this.token = '';
    this.state = 'idle';
    this.py = '';
    this.error = '';
    this.stages = [];
    this._stopping = false;
    this.logFile = this._openLogFile();
  }

  /**
   * 把后端原始输出落一份到 logs/backend-console.log。
   * 后端启动失败时，这份日志是唯一能直接定位原因的凭据（比如缺依赖、ffmpeg 异常）。
   */
  _openLogFile() {
    try {
      const logsDir = path.join(this.root, 'logs');
      if (!fs.existsSync(logsDir)) fs.mkdirSync(logsDir, { recursive: true });
      const stream = fs.createWriteStream(path.join(logsDir, 'backend-console.log'), { flags: 'a' });
      stream.on('error', () => {});
      stream.write(`\n===== ${new Date().toISOString()} 启动后端 =====\n`);
      return stream;
    } catch {
      return null;
    }
  }

  _trace(text) {
    if (process.argv.includes('--dev')) console.log(`[backend] ${text}`);
    try {
      this.logFile?.write(`${text}\n`);
    } catch {
      /* 日志写不进去也不影响主流程 */
    }
  }

  // ------------------------------------------------------------ 解释器
  /**
   * 按优先级找 Python：
   * 1. 环境变量 DYLR_PYTHON
   * 2. 项目内 .venv（Windows: Scripts/python.exe，其它: bin/python）
   * 3. PATH 里的 python3 / python
   */
  resolvePython() {
    const fromEnv = process.env.DYLR_PYTHON;
    if (fromEnv && fs.existsSync(fromEnv)) return fromEnv;

    const candidates = process.platform === 'win32'
      ? [
        path.join(this.root, '.venv', 'Scripts', 'python.exe'),
        path.join(this.root, '.venv.py310-legacy', 'Scripts', 'python.exe'),
      ]
      : [
        path.join(this.root, '.venv', 'bin', 'python3'),
        path.join(this.root, '.venv', 'bin', 'python'),
      ];
    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) return candidate;
    }
    return process.platform === 'win32' ? 'python' : 'python3';
  }

  hasProjectVenv() {
    const marker = process.platform === 'win32'
      ? path.join(this.root, '.venv', 'Scripts', 'python.exe')
      : path.join(this.root, '.venv', 'bin', 'python3');
    return fs.existsSync(marker);
  }

  // ------------------------------------------------------------ 启动
  async start() {
    if (this.process) await this.stop();
    this.stages = [];   // 重启时清空，避免两轮里程碑叠在一起

    // 开发模式：直接连已经在跑的后端
    if (process.env.DYLR_API) {
      const url = new URL(process.env.DYLR_API);
      this.baseUrl = `${url.protocol}//${url.host}`;
      this.token = process.env.DYLR_TOKEN || '';
      this._setState('ready', { baseUrl: this.baseUrl, token: this.token, external: true });
      return { baseUrl: this.baseUrl, token: this.token };
    }

    this.py = this.resolvePython();
    this.token = crypto.randomBytes(24).toString('hex');
    this.error = '';
    this._stopping = false;
    this._setState('starting', { python: this.py });

    const args = [
      '-m', 'dylr',
      '--host', '127.0.0.1',
      '--port', '0',
      '--token', this.token,
      '--no-console',
      '--print-port',
    ];

    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        this.error = '后端启动超时（60 秒）。请检查依赖是否安装完整：运行「安装依赖.bat」。';
        this._setState('error', { error: this.error });
        reject(new Error(this.error));
      }, START_TIMEOUT_MS);

      this._trace(`$ ${this.py} ${args.join(' ')}`);
      const child = spawn(this.py, args, {
        cwd: this.root,
        env: {
          ...process.env,
          // 清掉父进程注入的 NODE_OPTIONS（Electron/Node 不认其中部分参数），
          // 同时强制 UTF-8，避免 Windows 管道下中文日志乱码。
          NODE_OPTIONS: '',
          DYLR_HOME: this.root,
          PYTHONUNBUFFERED: '1',
          PYTHONIOENCODING: 'utf-8',
          PYTHONUTF8: '1',
        },
        windowsHide: true,
      });
      this.process = child;

      const handleLine = (stream, line) => {
        const text = String(line).replace(/\r$/, '');
        if (!text.trim()) return;

        const stage = STAGE_PATTERN.exec(text.trim());
        if (stage) {
          // 里程碑单独走一条通道：它是「启动到哪一步」的结构化信息，
          // 不是日志，混进启动日志面板只会让用户多读几行噪音
          this.stages.push(stage[1]);
          this._trace(`[stage] ${stage[1]}`);
          this.onStage(stage[1]);
          return;
        }

        const ready = READY_PATTERN.exec(text);
        if (ready) {
          this._trace(`[ready] port=${ready[1]}`);
          this.baseUrl = `http://127.0.0.1:${ready[1]}`;
          this.token = ready[2];
          this._setState('ready', { baseUrl: this.baseUrl, token: this.token, python: this.py });
          if (!settled) {
            settled = true;
            clearTimeout(timer);
            resolve({ baseUrl: this.baseUrl, token: this.token });
          }
          return;
        }
        this._trace(`[${stream}] ${text}`);
        this.onLog({ stream, text });
      };

      lineReader(child.stdout, (line) => handleLine('stdout', line));
      lineReader(child.stderr, (line) => handleLine('stderr', line));

      child.on('error', (error) => {
        this.error = `无法启动 Python（${this.py}）：${error.message}`;
        this._setState('error', { error: this.error });
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(this.error));
        }
      });

      child.on('exit', (code, signal) => {
        this.process = null;
        if (this._stopping) {
          this._setState('stopped', { code, signal });
          return;
        }
        this.error = `后端进程意外退出（code=${code} signal=${signal}）`;
        this._setState('exited', { code, signal, error: this.error });
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(this.error));
        }
      });
    });
  }

  // ------------------------------------------------------------ 停止
  async stop() {
    const child = this.process;
    if (!child) return;
    this._stopping = true;

    // 1) 先请后端自己收尾（会让 ffmpeg 正常结束）
    if (this.baseUrl && this.token) {
      try {
        await fetch(`${this.baseUrl}/api/quit`, {
          method: 'POST',
          headers: { 'X-DYLR-Token': this.token },
          signal: AbortSignal.timeout(4000),
        });
      } catch {
        /* 后端可能已经不可达，继续走信号 */
      }
    }

    const exited = await waitForExit(child, STOP_TIMEOUT_MS);
    if (!exited) {
      child.kill('SIGTERM');
      const graceful = await waitForExit(child, 4000);
      if (!graceful && process.platform === 'win32') {
        try {
          spawn('taskkill', ['/pid', String(child.pid), '/f', '/t'], { windowsHide: true });
        } catch {
          child.kill('SIGKILL');
        }
      } else if (!graceful) {
        child.kill('SIGKILL');
      }
    }
    this.process = null;
    this.baseUrl = '';
  }

  _setState(state, detail = {}) {
    this.state = state;
    // 记下最后一次状态：渲染层可能在事件发出之后才注册监听，
    // 需要能主动补拉一次，否则会永远停在「正在启动」。
    this.lastStatus = {
      state,
      ...detail,
      baseUrl: detail.baseUrl || this.baseUrl,
      token: detail.token || this.token,
      python: detail.python || this.py,
      error: detail.error || this.error,
    };
    this.onStatus(this.lastStatus);
  }

  /**
   * 供 IPC 主动查询当前状态。
   *
   * 带上已收到的启动里程碑：渲染层可能在某个阶段之后才注册监听，
   * 补拉一次就能把已经走过的步骤补齐，不会永远停在第一步。
   */
  snapshot() {
    return {
      ...(this.lastStatus || { state: this.state, baseUrl: this.baseUrl, token: this.token }),
      stages: [...this.stages],
    };
  }
}

function lineReader(stream, onLine) {
  if (!stream) return;
  const decoder = new (require('node:string_decoder').StringDecoder)('utf8');
  let buffer = '';
  stream.on('data', (chunk) => {
    buffer += decoder.write(chunk);
    const parts = buffer.split('\n');
    buffer = parts.pop() || '';
    parts.forEach(onLine);
  });
  stream.on('end', () => {
    buffer += decoder.end();
    if (buffer.trim()) onLine(buffer);
  });
}

function waitForExit(child, timeout) {
  if (child.exitCode !== null || child.signalCode) return Promise.resolve(true);
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.removeListener('exit', onExit);
      resolve(false);
    }, timeout);
    const onExit = () => {
      clearTimeout(timer);
      resolve(true);
    };
    child.once('exit', onExit);
  });
}

module.exports = { BackendService };
