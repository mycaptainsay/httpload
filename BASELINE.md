# BASELINE — httpload 最小可运行版记录

## 开发日期

2026-07-25

## 技术方案

方案 A：固定 Worker Pool（Python `concurrent.futures.ThreadPoolExecutor`）

| 选项 | 选择 |
|---|---|
| 语言 | Python 3.9 |
| 并发模型 | `ThreadPoolExecutor(max_workers=C)` + `BoundedSemaphore(C)` 控制队列深度 |
| HTTP 客户端 | `requests.Session`（urllib3 连接池，HTTP keep-alive 自动复用） |
| 超时机制 | `requests.get(timeout=timeout_sec)` → `requests.exceptions.Timeout` |
| 统计并发安全 | `threading.Lock` 保护计数器和延迟切片 |
| 优雅退出 | `signal.SIGINT` → `threading.Event` → 停止提交新任务 + 取消排队任务 |

---

## 已完成功能 ✅

| 功能 | 状态 | 验证 |
|---|---|---|
| CLI 参数解析（长参数 `--url` `--requests` `--concurrency` `--timeout`） | ✅ | `--help` 正常输出 |
| CLI 短参数（`-u` `-n` `-c` `-t`） | ✅ | 手动测试通过 |
| 参数校验（URL 缺失/非法、整数 ≤0、timeout 格式） | ✅ | 5 种非法输入均报错退出 |
| 固定 Worker Pool 并发控制 | ✅ | `ThreadPoolExecutor(max_workers=C)` + `BoundedSemaphore` |
| Semaphore 控制队列深度（防止内部队列无界膨胀） | ✅ | 中断测试验证 submit 循环正确阻塞和中断检测 |
| HTTP GET 请求 + 单请求超时 | ✅ | 正常场景 100/100 成功 |
| HTTP 连接复用（requests.Session 连接池） | ✅ | 单一 Session 实例在所有 worker 间共享 |
| 响应体正确关闭（释放连接回池） | ✅ | `resp.close()` 释放回 urllib3 连接池 |
| 状态码四分类（2xx / Non-2xx / Error / Timeout） | ✅ | 404→Non-2xx, 500→Non-2xx, 无服务→Error 均正确 |
| 统计输出（Completed、Succeeded、Non-2xx、Errors、Timeouts） | ✅ | 所有场景均正确 |
| 延迟统计（Avg/Min/Max） | ✅ | 以 ms 为单位 |
| 吞吐量（Requests/sec） | ✅ | 基于 elapsed 计算 |
| 统计守恒（Completed = Sum of 4 categories） | ✅ | 所有手动测试场景 + 9 个自动化测试均成立 |
| 并发数 > 请求数时自动调整 + 明确说明 | ✅ | 输出 Note 提示 |
| Ctrl+C 优雅退出（停止调度、取消排队、输出已统计、不挂起） | ✅ | 中断测试验证 |
| 自动化测试（9 个） | ✅ | 全部通过，覆盖：成功/非200/混合/超时/中断/网络错误/并发调整/统计守恒 |

---

## 自动化测试覆盖

| 测试 | 涵盖点 |
|---|---|
| `TestBasicSuccess.test_all_success` | 全部 200，请求总数、分类、统计守恒、延迟记录 |
| `TestNon2xx.test_all_404` | 全部 404，Non-2xx 分类正确 |
| `TestNon2xx.test_mixed_200_404` | 混合 200+404，分类计数准确 |
| `TestTimeout.test_timeout` | 慢服务器触发超时统计 |
| `TestInterrupt.test_interrupt_stops_cleanly` | 中断后不挂起、正确退出、统计完整 |
| `TestNetworkError.test_error_counting_no_deadlock` | 端口不可达不死锁、Error 统计正确 |
| `TestConcurrencyAdjust.test_auto_adjust` | 并发数 > 请求数时自动调整 |
| `TestStats.test_conservation_empty` | Stats 空状态守恒 |
| `TestStats.test_conservation_after_records` | Stats 记录后守恒 |

---

## 暂未完成 / 已知限制

| 项目 | 说明 |
|---|---|
| Python 无法中断 in-flight HTTP 请求 | `requests`（同 `urllib`）基于同步 socket，无法从外部取消已发出的连接。Ctrl+C 后最多等待 `--timeout` 秒。此限制已在 README 中说明 |
| ResourceWarning in tests | 测试服务器（`http.server`）在客户端超时断开时有 socket 未关闭的 warning，属于 Python stdlib 已知行为，不影响功能 |
| POST/PUT 等方法 | 暂不支持，题目只要求 GET |

---

## 开发过程中发现并修复的问题 🐛

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | Windows GBK 终端无法打印 emoji | Windows 中文终端默认 GBK 编码 | 替换为纯 ASCII |
| 2 | Concurrency 输出显示原始值 | `run_load_test` 内部调整后未返回 | 返回值增加字段 |
| 3-4 | 测试服务器遗留进程 | 端口被旧进程占用 | 换端口 + 自定义 Handler |
| 5 | Python descriptor 隐式传 self | 实例访问类属性函数 | 改为类名直接访问 |
| 6 | 中断测试失败（submit too fast） | `executor.submit` 不阻塞，队列无界 | 添加 `BoundedSemaphore` + 外部中断参数 |
