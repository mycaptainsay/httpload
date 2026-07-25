# 问题清单与修复记录（终版）

> 对照审核清单 `审核问题清单.md` 的 ID 逐一处理。

---

## 审核问题 ID → 状态总览

| ID | 问题 | 本次状态 | 说明 |
|---|---|---|---|
| A | HTTP 连接/Client 未复用 | ✅ 已在上一轮修复 | `requests.Session` + urllib3 连接池 |
| B | `resp.read()` 整包读 body | ✅ 本次修复 | `stream=True` + `iter_content(8192)` 分块丢弃 |
| C | 无法取消 in-flight 请求 | 📝 已知限制 | Python 同步 socket 无法外部取消，文档已说明 |
| D | cancel 与 semaphore 不一致 | ✅ 本次修复 | drain 后统计 cancelled futures 并释放对应 sem |
| E | `conservation_ok` 恒真（假测试） | ✅ 本次修复 | `_completed` 改为独立计数器；新增 `test_in_flight_peak_tracking` |
| F | 未测最大 in-flight 并发 | ✅ 本次修复 | Stats 新增 `max_in_flight` 追踪；每个测试都断言 `≤ concurrency` |
| G | timeout/error/mixed 断言过松 | ✅ 本次修复 | timeout: ≥18/20; mixed: ≈50±10; error: errors+timeouts=20 |
| H | 未测真实 SIGINT | 📝 已知限制 | 测试用 `ext_interrupt` Event（逻辑等价路径），已在测试注释中诚实说明 |
| I | ResourceWarning | 📝 已知限制 | 警告来自测试服务器 `http.server`，客户端 `Session.close()` 已正确释放 |
| J | scheme 校验不严 | ✅ 本次修复 | 添加 http/https 白名单检查 |
| K | 短参数未做 | ✅ 已在上一轮修复 | `-u -n -c -t` |
| L | 文档过时/夸大 | ✅ 本次修复 | BASELINE 重写，ISSUES 更新，README 已由 linter 自动更新 |

---

## 本次修复的关键代码变更

### 1. Stats 独立计数器（Fix E）

```python
class Stats:
    def __init__(self):
        self._completed = 0       # 独立计数器（非四分类之和推导）
        self.max_in_flight = 0    # in-flight 峰值
        self._in_flight = 0

    def enter_request(self):       # 每次请求前调用
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)

    def exit_request(self, category, latency):  # 每次请求后调用
        self._completed += 1       # 独立递增
        self._in_flight -= 1
        # 分类递增...
```

### 2. 响应体分块丢弃（Fix B）

```python
resp = session.get(url, timeout=timeout_sec, stream=True)
for _ in resp.iter_content(chunk_size=8192):  # 不整包进内存
    pass
resp.close()
```

### 3. Semaphore 一致性修复（Fix D）

```python
# Drain 结束后：
cancelled = sum(1 for f in futures if f.cancelled())
for _ in range(cancelled):
    sem.release()   # 被 cancel 的 Future 未执行 _worker，补释放
```

### 4. 收紧的测试断言（Fix F + G）

| 测试 | 旧断言 | 新断言 |
|---|---|---|
| `test_timeout` | `timeouts > 0` | `timeouts >= 18` (20 个请求) |
| `test_mixed_200_404` | `succeeded > 0, non2xx > 0` | `succeeded ≈ 50 ± 10, non2xx ≈ 50 ± 10` |
| `test_error_counting` | `errors + timeouts > 0` | `errors + timeouts == 20, succeeded == 0, non2xx == 0` |
| 所有集成测试 | 无 in-flight 检查 | `stats.max_in_flight <= concurrency` |

---

## 最终测试结果

```
py -m unittest test_httpload -v

Ran 10 tests in ~15s
OK (10/10)
```
