# BASELINE — httpload 当前状态记录

## 技术方案

方案 A：固定 Worker Pool（Python `concurrent.futures.ThreadPoolExecutor`）

| 选项 | 选择 |
|---|---|
| 语言 | Python 3.7+ |
| 并发模型 | `ThreadPoolExecutor(max_workers=C)` + `BoundedSemaphore(C)` |
| HTTP 客户端 | `requests.Session`（urllib3 连接池，HTTP keep-alive） |
| 响应体处理 | `stream=True` + `iter_content(chunk_size=8192)` 分块丢弃 |
| 超时机制 | `requests.get(timeout=...)` → `requests.exceptions.Timeout` |
| 统计并发安全 | `threading.Lock` 保护所有计数器和延迟切片 |
| 优雅退出 | `signal.SIGINT` → `threading.Event` → 停止提交 + 取消排队 |
| 测试 | 10 个自动化测试，本地 HTTP Server，不依赖公网 |

---

## 已完成功能

| 功能 | 说明 |
|---|---|
| CLI 长参数 + 短参数 (`-u -n -c -t`) | argparse |
| 参数校验（scheme=http/https、host缺失、≤0、timeout格式） | 含 scheme 白名单 |
| Worker Pool 并发控制 | `ThreadPoolExecutor` + `BoundedSemaphore` 防无界队列 |
| HTTP GET + 单请求超时 | `requests.Session.get(timeout=...)` |
| 响应体分块丢弃（不整包进内存） | `stream=True` + `iter_content(8192)` |
| HTTP 连接复用 | 共享 `requests.Session`，urllib3 连接池 keep-alive |
| 四分类统计（2xx/Non-2xx/Error/Timeout） | `Stats.exit_request(category, latency)` |
| 统计守恒（独立计数器 `_completed`） | 非恒等式 — 能捕获双计数/漏计 bug |
| in-flight 峰值追踪 | `Stats.max_in_flight` |
| 压测报告（Elapsed/RPS/Avg/Min/Max latency） | 与题目示例对齐 |
| 并发数自动调整 + 明确说明 | main 中打印 Note |
| Ctrl+C 优雅退出 | 停止调度、取消排队、输出部分结果、标记 Interrupted |
| Semaphore release 修复 | 取消的 Future 对应 release |
| 自动化测试 10 个 | 全部通过，覆盖成功/404/混合/超时/中断/网络错误/并发调整/Stats单元 |

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 无法取消 in-flight 请求 | Python 同步 socket 限制，Ctrl+C 后最多等 `--timeout` |
| 未测真实 SIGINT 路径 | 测试用 `ext_interrupt` Event（逻辑等价，非 OS 信号） |
| ResourceWarning（测试服务器侧） | `http.server` 在客户端超时断开时未完整关闭 socket，不影响功能 |
| 仅支持 GET | 题目要求仅 GET |

---

## 开发过程中发现并修复的问题（摘要）

1. Windows GBK emoji → ASCII
2. Concurrency 输出显示原始值 → 返回 adjusted 值
3. 测试服务器端口冲突 → 换端口
4. Non-2xx 误判为 Success → 服务器返回正确的 status code
5. Python descriptor 隐式传 self → 类名直接访问
6. Submit 太快无阻塞 → BoundedSemaphore
7. 统计守恒恒真 → 独立 `_completed` 计数器
8. 响应体整包 read → `stream=True` + 分块丢弃
9. cancel 与 semaphore 不一致 → drain 后释放 cancelled 的 sem
10. 无 in-flight 峰值测试 → `max_in_flight` + 测试
11. 弱断言 → 收紧 timeout/mixed/error 测试断言
12. scheme 校验不严 → http/https 白名单
