# 问题清单与修复记录

## 问题 A：HTTP Client 连接不复用 🔴 → ✅ 已修复

**发现时间**：2026-07-25 第二轮审查
**来源**：第四节第2款 — "尽可能复用 HTTP Client 和底层连接"

**现象**：
`urllib.request.urlopen()` 每次调用创建新的 TCP 连接，使用 HTTP/1.0，不支持 keep-alive。
100 个请求 = 100 次 TCP 握手 + 100 次挥手（产生大量 TIME_WAIT）。

**修复方法**：
将 HTTP 层从 `urllib.request` 完全替换为 `requests` 库（2.32.5）。
- 在 `run_load_test()` 中创建单一 `requests.Session` 实例
- 所有 worker 通过同一个 Session 发出 GET 请求
- `requests.Session` 底层使用 `urllib3` 连接池，自动复用 TCP 连接（HTTP keep-alive）
- `run_load_test()` 结束时调用 `session.close()` 释放所有连接

**验证**：
- 全部 9 个自动化测试通过（包括成功、非200、超时、中断、网络错误、统计守恒等场景）
- 手动测试 200/404/500 三种场景均正确，统计守恒成立
- 不再出现 urllib 相关的 socket ResourceWarning（连接池由 Session 统一管理）

---

## 问题 B：缺少"网络错误不造成死锁"测试 🔴 → ✅ 已修复

**发现时间**：2026-07-25 第二轮审查
**来源**：第六节建议覆盖 — "网络错误不会造成死锁"

**现象**：
当前 7 个测试中，没有验证：当目标端口不可达时，压测工具能否正确统计为 Error 并正常结束。

**修复方法**：
新增 `TestNetworkError` 测试类，包含 `test_error_counting_no_deadlock` 测试：
- 向 `http://127.0.0.1:19999/`（无服务监听）发送 20 个请求
- 验证：`interrupted=False`、`scheduled=20`、`completed=20`
- 验证：`errors + timeouts > 0`（网络错误被正确捕获）
- 验证：`conservation_ok()` 成立
- 验证：测试在合理时间内完成（不挂起/不死锁）

**验证**：
- `TestNetworkError.test_error_counting_no_deadlock` — PASS
- 连同其他 8 个测试，全部通过

---

## 问题 C：缺少"中止后正常退出"测试 🔴 → ✅ 已修复

**发现时间**：2026-07-25 第二轮审查
**来源**：第六节建议覆盖 — "中止后可以正常退出"

**现象**：
当前没有自动化测试验证中断后：不再调度新请求、不挂起、正确输出 Interrupted 和已完成的统计数据。

**修复方法**：
1. 重构 `run_load_test()`：添加可选参数 `ext_interrupt: threading.Event`。当外部传入 Event 时，跳过 signal 处理器安装，由调用方控制中断时机。这使得测试可以模拟 Ctrl+C 行为。

2. 添加 `BoundedSemaphore(concurrency)` 控制 submit 队列深度：submit 循环在 semaphore 计数为 0 时阻塞，确保"同时执行不超过 C"且队列不会无界膨胀。中断时 submit 循环在下一个 semaphore release 后检测到 `interrupted.is_set()` 并跳出。

3. 新增 `TestInterrupt` 测试类：
   - 在后台线程运行 `run_load_test()`（传入外部 Event，目标为慢速服务器）
   - 主线程等待 0.8 秒后设置 Event（模拟 Ctrl+C）
   - 验证：线程在 5 秒内完成（不挂起）
   - 验证：`was_interrupted=True`
   - 验证：`completed > 0`（已完成的请求被正确统计）
   - 验证：`conservation_ok()` 成立

**验证**：
- `TestInterrupt.test_interrupt_stops_cleanly` — PASS
- 连同其他 8 个测试，全部通过

---

## 问题 D：并发数 > 请求数时未明确说明行为 🟡 → ✅ 已修复

**发现时间**：2026-07-25 第二轮审查
**来源**：第四节第3款 — "必须明确说明行为"

**现象**：
当 `--concurrency 20 --requests 5` 时，工具静默将并发数降为 5。

**修复方法**：
在 `main()` 中的 `run_load_test()` 返回后、`print_results()` 之前，增加判断：
```python
if adjusted_concurrency != args.concurrency:
    print(f"Note: concurrency adjusted from {args.concurrency} "
          f"to {adjusted_concurrency} (concurrency > requests, using requests count)")
```
输出示例：
```
Note: concurrency adjusted from 20 to 5 (concurrency > requests, using requests count)
```

**验证**：
- 手动测试：`--requests 5 --concurrency 20` → 输出中正确显示提示
- 自动化测试：`TestConcurrencyAdjust.test_auto_adjust` — PASS

---

## 问题 E：urllib 无法真正取消 in-flight 请求 🟡 → 📝 已记录

**发现时间**：2026-07-25 第二轮审查
**来源**：第四节第5款 — "通知正在执行的请求结束或取消"

**问题本质**：
所有基于 socket 的同步 HTTP 客户端（包括 `urllib` 和 `requests`）都有此限制：
正在进行的 TCP 连接无法被外部线程"杀死"——只能等待其自然完成或超时。
这与 Go 的 context 取消（`ctx.Done()` 传播到 socket 层）有本质差距。

**当前行为**：
- Ctrl+C 后，submit 循环停止（不再提交新任务到队列）
- 已在执行的请求继续运行直到完成或超时
- 最长等待时间 = 单请求超时（`--timeout`）
- 满足题目"不长时间挂起"的要求

**记录决议**：Python 语言层面无法完美解决。此限制已在 README.md 中说明。
此问题是 Python 生态系统的已知限制，不属于 httpload 的实现缺陷。

---

## 问题 F：短参数 -u -n -c -t 未实现 🟢 → ✅ 已修复

**发现时间**：2026-07-25 第二轮审查
**来源**：第三节 — "允许增加短参数"

**修复方法**：
在 `argparse` 参数定义中为每个长参数增加短参数别名：
```python
"-u", "--url"
"-n", "--requests"
"-c", "--concurrency"
"-t", "--timeout"
```

**验证**：
```bash
py httpload.py -u http://localhost:9879/ -n 10 -c 3 -t 2s
```
输出正常，长参数和短参数均可使用。

---

## 问题 G：ResourceWarning — socket 未显式关闭 🟢 → 📝 已知限制

**发现时间**：2026-07-25 第一轮测试
**来源**：第四节第1款 — "请求结束后不能遗留后台执行单元"

**现象**：
自动化测试中每次都会出现 `ResourceWarning: unclosed <socket.socket>`。

**分析**：
- 这些警告来自测试服务器端（`http.server.HTTPServer`），而非 httpload 客户端
- 测试中 `server.shutdown()` 会等待请求处理完成，但 `ConnectionAbortedError`（客户端超时断开）导致服务端 socket 未完全关闭
- 迁移到 `requests.Session` 后，客户端连接由 `session.close()` 统一释放，不再有客户端侧泄漏
- 这是测试辅助服务器的已知行为，不影响 httpload 功能

**记录决议**：测试服务器的 ResourceWarning 是 Python `http.server` 模块的已知行为，可通过 `-W ignore::ResourceWarning` 抑制。不影响功能正确性。
