# httpload — HTTP Load Testing Tool

命令行 HTTP 接口压力测试工具。使用固定 Worker Pool（`ThreadPoolExecutor` + `BoundedSemaphore`）控制并发，通过 `requests.Session` 复用连接。

## 运行环境

- Python 3.7+
- 依赖：`requests`（见 `requirements.txt`）

```bash
pip install -r requirements.txt
```

## 使用方法

```bash
# 长参数
py httpload.py --url http://localhost:8080/ --requests 1000 --concurrency 20 --timeout 2s

# 短参数
py httpload.py -u http://localhost:8080/ -n 1000 -c 20 -t 2s
```

| 参数 | 短参 | 说明 |
|---|---|---|
| `--url` | `-u` | 被压测的 HTTP 地址 |
| `--requests` | `-n` | 总请求数（> 0） |
| `--concurrency` | `-c` | 最大并发请求数（> 0） |
| `--timeout` | `-t` | 单请求超时（如 `2s`、`500ms`） |

## 示例压测命令

```bash
# 先启动本地服务（可选）：npx serve -l 8080

# 正常压测
py httpload.py --url http://localhost:8080/ --requests 10000 --concurrency 50 --timeout 2s

# 非 2xx（404）
py httpload.py --url http://localhost:8080/not-found --requests 1000 --concurrency 20 --timeout 2s

# 网络错误
py httpload.py --url http://localhost:65530/ --requests 100 --concurrency 10 --timeout 500ms
```

## 运行自动化测试

```bash
cd httpload
py -m unittest test_httpload -v
```

## 行为说明

- 当 `concurrency > requests` 时，自动将并发数降为请求数，并打印 `Note: concurrency adjusted...`
- Ctrl+C 后停止调度新请求，输出已完成部分的统计，并标记 `Interrupted: true`
- 延迟统计以所有已完成请求（含超时和错误）为准

## 已完成 / 未完成

### 已完成

- CLI 长/短参数与参数校验（含 http/https scheme 白名单）
- 固定 Worker Pool 并发控制（同时在途请求不超过 concurrency）
- HTTP GET、单请求超时、`stream=True` 分块丢弃响应体、Session 连接复用
- 四类统计：Succeeded / Non-2xx / Errors / Timeouts（独立 `_completed` 守恒校验）
- in-flight 峰值追踪（`max_in_flight`）与相关自动化断言
- 压测报告：Elapsed、Requests/sec、Avg/Min/Max latency
- Ctrl+C：停止调度、输出部分结果、显示 Interrupted
- 本地自动化测试（不依赖公网）

### 未完成 / 已知限制

- 同步 `requests` 无法从外部强制取消已在执行的 HTTP 请求；中止后最多等待单请求 timeout
- 自动化测试用 `ext_interrupt` Event 模拟中止，未覆盖真实 SIGINT 路径
- 在部分环境下（如 Windows 连不上目标端口），`ConnectTimeout` 可能被归入 Timeouts 而非 Errors

### 若继续开发

1. 将 `ConnectTimeout` 明确归类为 Errors，与 ReadTimeout 区分
2. 若需要真正取消 in-flight，考虑迁移到 `aiohttp` 等可取消的异步客户端

## 输出示例

```text
Target:         http://localhost:8080/
Requests:       1000
Concurrency:    20

Completed:      1000
Succeeded:      970
Non-2xx:        15
Errors:         5
Timeouts:       10

Elapsed:        2.35s
Requests/sec:   425.53
Avg latency:    43.21ms
Min latency:    10.14ms
Max latency:    201.45ms
```
